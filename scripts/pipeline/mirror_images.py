"""Copies the images the pipeline's builds pull into the account's private mirror (system design,
section 8.2).

**What it mirrors**, read from the source it runs on: the image of each FROM line of the files
named `Dockerfile` under `services/`, but `scratch` and a stage of the same file, and the emulator
and BuildKit images that `src/rhi/pipeline/stack.py` pins, `EMULATOR_IMAGE` and `BUILDKIT_IMAGE`,
read from that file as text. Each is an image of Docker Hub, pinned by its sha256 digest: an image
named without one, or from another registry, fails the job before anything is copied. An official
image's path on Docker Hub starts with `library/`.

**Where.** Each image to the repository `rhi-mirror/` followed by its path on Docker Hub, in the
registry of the account and region the job runs in: the host is made from the caller's account,
which STS gives, and the region CodeBuild sets in `AWS_REGION`, so nothing names the account.

**What it reads.** For each image, ECR first: when the repository holds the pinned digest, the job
reads nothing else for it, and Docker Hub not at all. When it lacks the digest, `crane copy`, from
the path `RHI_MIRROR_CRANE` names, copies the image from Docker Hub, read anonymously, by its
digest: its index, every manifest the index lists and their layers, so the repository then holds
the pinned digest itself. The copy is tagged with the pin's tag when it has one. Before its first
copy the job logs crane in to the registry with an ECR authorization token of its own role.

**A repository that does not exist yet**, an image a change adds before the self-update has
created its repository, is left for a later execution: the job says so and goes on, and it does
not fail for it.

**When a copy fails**, the job goes on to the next image, and once it has tried every image exits
1 if any was not copied: what was copied stays, so a retried execution copies what is left.

It prints its own lines, never a message of AWS. Run by the pipeline's step `Mirror`
(`src/rhi/pipeline/mirror.py`), from the root of the source:

    uv run --locked python scripts/pipeline/mirror_images.py
"""

from __future__ import annotations

import ast
import base64
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
# The folder whose Dockerfiles are read, and the file that pins the emulator and BuildKit.
SERVICES = "services"
PIPELINE_STACK = "src/rhi/pipeline/stack.py"
PINNED_IN_THE_STACK = ("EMULATOR_IMAGE", "BUILDKIT_IMAGE")
# Every repository of the mirror is named under this path.
MIRROR = "rhi-mirror"
# Where an image is copied from, and the names Docker Hub is also known by.
DOCKER_HUB = "docker.io"
DOCKER_HUB_NAMES = ("docker.io", "index.docker.io", "registry-1.docker.io")
# The variable that names crane's path to the job.
CRANE_VARIABLE = "RHI_MIRROR_CRANE"

FROM_LINE = re.compile(r"^\s*FROM\s+(?P<rest>.+?)\s*$", re.IGNORECASE)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ListingError(Exception):
    """The source names an image the job cannot mirror: without its digest, or not on Docker Hub."""


@dataclass(frozen=True)
class Image:
    """An image of Docker Hub, by its path there, the tag of its pin if any, and its digest."""

    path: str
    tag: str | None
    digest: str

    @property
    def repository(self) -> str:
        return f"{MIRROR}/{self.path}"

    @property
    def source(self) -> str:
        return f"{DOCKER_HUB}/{self.path}@{self.digest}"

    def destination(self, host: str) -> str:
        reference = f":{self.tag}" if self.tag else f"@{self.digest}"
        return f"{host}/{self.repository}{reference}"


def parse(reference: str) -> Image:
    """The image `reference` names, as a FROM line or a pin writes it."""
    name, at, digest = reference.partition("@")
    if not at or not DIGEST.fullmatch(digest):
        raise ListingError(f"{reference}: not pinned by its sha256 digest")
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        if parts[0] not in DOCKER_HUB_NAMES:
            raise ListingError(f"{reference}: not an image of Docker Hub")
        parts = parts[1:]
    last, colon, tag = parts[-1].partition(":")
    parts[-1] = last
    if len(parts) == 1:
        parts.insert(0, "library")
    return Image(path="/".join(parts), tag=tag if colon else None, digest=digest)


def from_images(dockerfile: str) -> list[str]:
    """The image of each FROM line of `dockerfile`, but `scratch` and a stage of the same file."""
    stages: set[str] = set()
    images = []
    for line in dockerfile.splitlines():
        match = FROM_LINE.match(line)
        if match is None:
            continue
        words = [word for word in match["rest"].split() if not word.startswith("--")]
        if not words:
            continue
        image = words[0]
        # A stage is named by an earlier FROM line of the file.
        if image.lower() != "scratch" and image.lower() not in stages:
            images.append(image)
        if len(words) >= 3 and words[1].lower() == "as":
            stages.add(words[2].lower())
    return images


def stack_pins(stack: str) -> list[str]:
    """The values of the pins `PINNED_IN_THE_STACK` in the module text `stack`."""
    values = {}
    for node in ast.parse(stack).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in PINNED_IN_THE_STACK
        ):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    missing = [name for name in PINNED_IN_THE_STACK if not isinstance(values.get(name), str)]
    if missing:
        raise ListingError(f"{PIPELINE_STACK}: no pin {', '.join(missing)}")
    return [values[name] for name in PINNED_IN_THE_STACK]


def listed(root: Path = REPO) -> list[Image]:
    """The images to mirror, read from the tree at `root`, each once."""
    references = [
        image
        for dockerfile in sorted((root / SERVICES).rglob("Dockerfile"))
        if dockerfile.is_file()
        for image in from_images(dockerfile.read_text(encoding="utf-8"))
    ]
    references += stack_pins((root / PIPELINE_STACK).read_text(encoding="utf-8"))
    images: dict[tuple[str, str], Image] = {}
    for reference in references:
        image = parse(reference)
        images.setdefault((image.path, image.digest), image)
    return list(images.values())


class Crane(Protocol):
    """Runs crane with `args`, `stdin` on its standard input, and gives its exit status."""

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> int: ...


class NoRepository(Exception):
    """The image's repository does not exist yet."""


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def holds(ecr: Any, image: Image) -> bool:
    """Whether the image's repository holds its digest. Raises `NoRepository` when there is no such
    repository, and what ECR raises for any other error."""
    try:
        ecr.describe_images(repositoryName=image.repository, imageIds=[{"imageDigest": image.digest}])
    except ClientError as error:
        if _code(error) == "ImageNotFoundException":
            return False
        if _code(error) == "RepositoryNotFoundException":
            raise NoRepository from error
        raise
    return True


def log_in(ecr: Any, crane: Crane, host: str) -> bool:
    """Logs crane in to the registry at `host`, with an ECR authorization token."""
    try:
        token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    except (BotoCoreError, ClientError, KeyError, IndexError):
        return False
    user, _, password = base64.b64decode(token).decode("utf-8").partition(":")
    return crane(["auth", "login", host, "--username", user, "--password-stdin"], stdin=password) == 0


def mirror(images: Iterable[Image], *, ecr: Any, crane: Crane, host: str) -> int:
    """Copies into the mirror at `host` each of `images` whose digest its repository lacks. Exits
    1 once every image was tried if one could not be copied, else 0."""
    logged_in = False
    failed = []
    for image in images:
        name = f"{image.repository}@{image.digest}"
        try:
            if holds(ecr, image):
                print(f"{name}: already mirrored")
                continue
        except NoRepository:
            print(f"{name}: no repository yet, left for a later execution")
            continue
        except (BotoCoreError, ClientError):
            print(f"{name}: could not be looked up in the mirror")
            failed.append(name)
            continue
        if not logged_in:
            logged_in = log_in(ecr, crane, host)
            if not logged_in:
                print(f"{name}: could not log in to the mirror")
                failed.append(name)
                continue
        if crane(["copy", image.source, image.destination(host)]) != 0:
            print(f"{name}: could not be copied from {DOCKER_HUB}")
            failed.append(name)
            continue
        print(f"{name}: copied from {DOCKER_HUB}")
    if failed:
        print(f"{len(failed)} image(s) not mirrored; a retried execution copies what is left")
        return 1
    return 0


def run_crane(path: str) -> Crane:
    """Crane at `path`, as a subprocess."""

    def crane(args: Sequence[str], stdin: str | None = None) -> int:
        return subprocess.run([path, *args], input=stdin, text=True, check=False).returncode

    return crane


def main(environ: Mapping[str, str] = os.environ, root: Path = REPO) -> int:
    try:
        images = listed(root)
    except ListingError as error:
        print(error)
        return 1
    import boto3

    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    host = f"{account}.dkr.ecr.{environ['AWS_REGION']}.amazonaws.com"
    return mirror(images, ecr=session.client("ecr"), crane=run_crane(environ[CRANE_VARIABLE]), host=host)


if __name__ == "__main__":
    sys.exit(main())
