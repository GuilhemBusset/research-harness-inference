"""No image is built on a base pulled from Docker Hub, but the two that are published nowhere else
(system design, sections 8.2, step 4, and 9).

Docker Hub limits anonymous pulls by address, and the build hosts of the pipeline's asset step
share theirs: a base pulled from there fails the step on a day of several merges. So every image a
Dockerfile makes the builder pull, the image of a FROM line and the one of a `# syntax=` directive,
comes from another registry, or is in `ALLOWED` with the reason it cannot. And every FROM line
names its image by its sha256 digest, whichever registry serves it.

The Dockerfiles are those git lists, tracked or untracked but not ignored: the pipeline's synth
step runs the tests in a work tree with no commit, where every file is untracked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The names Docker Hub's registry answers to. An image reference with no host names it too.
DOCKER_HUB = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
# The repositories of Docker Hub a FROM line may name, each with the reason it stays there.
ALLOWED = {
    "grafana/grafana-oss": (
        "Grafana publishes its image on Docker Hub and has no official repository on Amazon ECR"
        " Public or on GHCR (checked 2026-11-25)."
    ),
    "vllm/vllm-openai": (
        "vLLM publishes its image on Docker Hub and has no official repository on Amazon ECR"
        " Public or on GHCR (checked 2026-11-25)."
    ),
}

ON_DOCKER_HUB = "an image on Docker Hub, outside the allowed list"
NO_DIGEST = "an image without its sha256 digest"

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
FROM = re.compile(r"^\s*FROM\s+(?P<arguments>.+)$", re.IGNORECASE)
# The directive is one only at the top of a file. A line written like it lower down is refused all
# the same: nothing is lost, and nobody has to know where a builder stops looking.
SYNTAX = re.compile(r"^\s*#\s*syntax\s*=\s*(?P<image>\S+)", re.IGNORECASE)


def docker_hub_repository(image: str) -> str | None:
    """The repository of Docker Hub an image reference names, without tag or digest, and None when
    it names another registry. The reference names a host when its first path component, of
    several, has a `.` or a `:` or is `localhost`; with no host, or one of Docker Hub's, it names
    Docker Hub, where `python` is `library/python`."""
    name = image.split("@", 1)[0]
    first, slash, rest = name.partition("/")
    if slash and first.lower() in DOCKER_HUB:
        name = rest
    elif slash and ("." in first or ":" in first or first == "localhost"):
        return None
    folder, _, last = name.rpartition("/")
    repository = f"{folder}/{last.split(':', 1)[0]}" if folder else last.split(":", 1)[0]
    return repository.removeprefix("library/")


def findings(dockerfile: str) -> list[tuple[str, str]]:
    """What a Dockerfile's text names that it may not, as (image, what is wrong with it): the image
    of a FROM line or of a syntax directive on Docker Hub and not allowed, and the image of a FROM
    line without its digest. `FROM scratch` pulls nothing, and neither does a FROM that names an
    earlier stage of the same file."""
    found = []
    stages: set[str] = set()
    for line in dockerfile.replace("\\\n", " ").splitlines():
        syntax, base = SYNTAX.match(line), FROM.match(line)
        if syntax and docker_hub_repository(syntax["image"]) is not None:
            found.append((syntax["image"], ON_DOCKER_HUB))
        if not base:
            continue
        words = [word for word in base["arguments"].split() if not word.startswith("--")]
        image = words[0]
        if image.lower() != "scratch" and image.lower() not in stages:
            repository = docker_hub_repository(image)
            if repository is not None and repository not in ALLOWED:
                found.append((image, ON_DOCKER_HUB))
            if not DIGEST.search(image):
                found.append((image, NO_DIGEST))
        if len(words) == 3 and words[1].upper() == "AS":
            stages.add(words[2].lower())
    return found


def listed_dockerfiles() -> list[str]:
    """The Dockerfiles git lists, tracked or untracked but not ignored, as paths from the root."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    names = [name for name in listed.decode().split("\0") if name and (REPO / name).is_file()]
    return sorted(
        name
        for name in names
        if (base := name.rsplit("/", 1)[-1].lower()) == "dockerfile"
        or base.startswith("dockerfile.")
        or base.endswith(".dockerfile")
    )


def test_no_dockerfile_takes_an_image_from_docker_hub_but_the_allowed_ones_and_each_base_has_its_digest() -> None:
    dockerfiles = listed_dockerfiles()
    under_services = {path.relative_to(REPO).as_posix() for path in (REPO / "services").rglob("Dockerfile")}
    assert under_services, "the Dockerfiles were not found"
    assert under_services <= set(dockerfiles)

    texts = {name: (REPO / name).read_text(encoding="utf-8") for name in dockerfiles}
    assert {name: found for name, text in texts.items() if (found := findings(text))} == {}
    # An allowance is for an image a Dockerfile still takes from Docker Hub.
    for repository in ALLOWED:
        assert any(re.search(rf"^FROM {re.escape(repository)}[:@]", text, re.MULTILINE) for text in texts.values())


SHA = "@sha256:" + "0" * 64
ECR_PUBLIC = f"public.ecr.aws/docker/library/python:3.13{SHA}"


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        pytest.param(f"FROM python{SHA}\n", [(f"python{SHA}", ON_DOCKER_HUB)], id="python by digest, with no host"),
        pytest.param(
            f"FROM docker.io/library/python{SHA}\n",
            [(f"docker.io/library/python{SHA}", ON_DOCKER_HUB)],
            id="docker.io/library/python by digest",
        ),
        pytest.param(
            "FROM public.ecr.aws/docker/library/python:3.13\n",
            [("public.ecr.aws/docker/library/python:3.13", NO_DIGEST)],
            id="a tag and no digest",
        ),
        pytest.param(
            f"# syntax=docker/dockerfile:1\nFROM {ECR_PUBLIC}\n",
            [("docker/dockerfile:1", ON_DOCKER_HUB)],
            id="a syntax directive naming docker/dockerfile",
        ),
        pytest.param(
            f"FROM --platform=linux/arm64 registry-1.docker.io/grafana/grafana:13{SHA} AS base\n",
            [(f"registry-1.docker.io/grafana/grafana:13{SHA}", ON_DOCKER_HUB)],
            id="another name of Docker Hub, and a repository beside an allowed one",
        ),
        pytest.param(
            "FROM vllm/vllm-openai:v0.33.0\n", [("vllm/vllm-openai:v0.33.0", NO_DIGEST)], id="an allowed image by tag"
        ),
        pytest.param(
            f"FROM {ECR_PUBLIC} AS build\nFROM builder\n",
            [("builder", ON_DOCKER_HUB), ("builder", NO_DIGEST)],
            id="a name that is no earlier stage",
        ),
    ],
)
def test_an_image_on_docker_hub_or_without_its_digest_is_refused(
    dockerfile: str, expected: list[tuple[str, str]]
) -> None:
    assert findings(dockerfile) == expected


@pytest.mark.parametrize(
    "dockerfile",
    [
        pytest.param(f"# The base.\nFROM {ECR_PUBLIC}\n", id="another registry, by digest"),
        pytest.param(f"FROM grafana/grafana-oss:13.0.2{SHA}\n", id="an allowed image, by digest"),
        pytest.param("FROM scratch\nCOPY . /srv\n", id="scratch"),
        pytest.param(
            f"FROM --platform=$BUILDPLATFORM {ECR_PUBLIC} AS build\nFROM build AS test\nFROM scratch\n",
            id="an earlier stage",
        ),
        pytest.param(f"# syntax=ghcr.io/example/dockerfile:1{SHA}\nFROM localhost/base{SHA}\n", id="other hosts"),
    ],
)
def test_what_pulls_nothing_from_docker_hub_is_accepted(dockerfile: str) -> None:
    assert findings(dockerfile) == []
