"""The rule for model weights (system design, section 4.4): which files of a model repository are
copied into the model bucket, and the SHA-256 manifest every copied file is verified against.

**The allow-list.** A file is copied when its name says it is a safetensors file, a JSON file, or
one of the tokenizer's files `merges.txt`, `tokenizer.model` and `chat_template.jinja`. Anything
else is refused, pickle formats and Python files among it: a pickle file runs code when it is
loaded, and the engine runs no code from a model's repository.

**The manifest.** One JSON file per model repository, `models/manifests/<name>.json`, where the
name is the repository's id in lower case with its slash replaced by two hyphens. It holds exactly
three keys:

    {
      "repository": "Qwen/Qwen3.8-27B-FP8",
      "revision": "<the commit, 40 lower-case hexadecimal characters>",
      "files": [{"path": "config.json", "size": 51350, "sha256": "<64 of the same>"}]
    }

Every path is one the allow-list admits, and none is listed twice. Moving a model to another
revision is a change of its manifest.

**The program.** With a repository and a revision, it writes that repository's manifest from
Hugging Face's API, which it reads anonymously, over HTTPS:

    uv run --locked python scripts/pipeline/weights.py Qwen/Qwen3.8-27B-FP8 <revision>

Hugging Face publishes a SHA-256 only for a file stored in LFS (the weights). For any other file it
gives a git blob id, which is not a SHA-256 of the content. So a file's digest is taken from its
LFS metadata when the file is in LFS, and is otherwise computed from the file, downloaded at that
revision.

The job that copies the weights is another script: it uses `select`, `load` and `verify`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

NAME = "weights"
# Where the manifests are committed: `models/manifests/` of the repository this script is in.
MANIFESTS = Path(__file__).resolve().parents[2] / "models" / "manifests"
HUB = "https://huggingface.co"

# The allow-list: the endings of a safetensors file and of a JSON file, and the tokenizer's files
# that are neither (its merges file, its model file for some models, and the chat template).
COPIED_ENDINGS = (".safetensors", ".json")
TOKENIZER_FILES = frozenset({"merges.txt", "tokenizer.model", "chat_template.jinja"})

MANIFEST_KEYS = ("repository", "revision", "files")
FILE_KEYS = ("path", "size", "sha256")
# An id as Hugging Face writes one: an owner and a name, with one slash between them.
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
REVISION = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """A file, a manifest or an answer of Hugging Face that the rule does not accept."""


@dataclass(frozen=True)
class Selection:
    """A repository's files, each by its name: those that are copied, and those that are not."""

    copied: tuple[str, ...]
    refused: tuple[str, ...]


@dataclass(frozen=True)
class File:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    repository: str
    revision: str
    files: tuple[File, ...]


def admitted(path: str) -> bool:
    """Whether the allow-list admits a file of a model repository, which its name alone decides."""
    segments = path.split("/")
    if "\\" in path or any(segment in {"", ".", ".."} for segment in segments):
        return False  # Not a path inside the repository.
    return segments[-1] in TOKENIZER_FILES or segments[-1].endswith(COPIED_ENDINGS)


def select(paths: Iterable[str]) -> Selection:
    """Which of a repository's files are copied, and which are refused, in the order given."""
    copied, refused = [], []
    for path in paths:
        (copied if admitted(path) else refused).append(path)
    return Selection(tuple(copied), tuple(refused))


def manifest_name(repository: str) -> str:
    """The name of a repository's manifest file: Qwen/Qwen3.8-27B-FP8 gives qwen--qwen3.8-27b-fp8.json."""
    if not REPOSITORY.fullmatch(repository):
        raise Refused(f"{repository!r} is not a repository id, an owner and a name around one slash")
    return repository.lower().replace("/", "--") + ".json"


def _keyed(value: object, keys: tuple[str, ...], what: str) -> dict[str, object]:
    if not isinstance(value, dict) or sorted(value) != sorted(keys):
        raise Refused(f"{what} must hold exactly the keys {', '.join(keys)}")
    return value


def parse(document: object) -> Manifest:
    """The manifest a parsed JSON document holds. Refuses one that is not in the format."""
    manifest = _keyed(document, MANIFEST_KEYS, "a manifest")
    repository, revision, listed = (manifest[key] for key in MANIFEST_KEYS)
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise Refused("repository must be a repository id, an owner and a name around one slash")
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise Refused("revision must be a full commit hash, 40 lower-case hexadecimal characters")
    if not isinstance(listed, list):
        raise Refused("files must be a list")
    files: dict[str, File] = {}
    for entry in listed:
        path, size, sha256 = (_keyed(entry, FILE_KEYS, "a file")[key] for key in FILE_KEYS)
        if not isinstance(path, str) or not admitted(path):
            raise Refused(f"{path!r} is not a path the allow-list admits")
        if path in files:
            raise Refused(f"{path} is listed twice")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise Refused(f"{path}: size must be a number of bytes")
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            raise Refused(f"{path}: sha256 must be 64 lower-case hexadecimal characters")
        files[path] = File(path, size, sha256)
    return Manifest(repository, revision, tuple(files.values()))


def read(path: Path) -> Manifest:
    """The manifest in a file. Refuses one that is not in the format, or not under its name."""
    try:
        document = json.loads(path.read_bytes())
    except ValueError:  # Not UTF-8, or not JSON.
        raise Refused("not a JSON file") from None
    manifest = parse(document)
    if path.name != manifest_name(manifest.repository):
        raise Refused(f"the manifest of {manifest.repository} must be named {manifest_name(manifest.repository)}")
    return manifest


def load(repository: str, manifests: Path = MANIFESTS) -> Manifest:
    """The manifest of a repository. Refuses a repository that has none."""
    path = manifests / manifest_name(repository)
    if not path.is_file():
        raise Refused(f"{repository} has no manifest: there is no {path.name}")
    try:
        manifest = read(path)
    except Refused as refusal:
        raise Refused(f"{path.name}: {refusal}") from None
    if manifest.repository != repository:
        raise Refused(f"{path.name} is the manifest of {manifest.repository}, not of {repository}")
    return manifest


def invalid(manifests: Path = MANIFESTS) -> list[str]:
    """What is wrong under the manifests' folder, one line per entry that is not a manifest in the
    format under its name. Empty when every file is one, and when the folder does not exist."""
    problems = []
    for path in sorted(manifests.iterdir()) if manifests.is_dir() else []:
        if path.is_dir():
            problems.append(f"{path.name}: a folder, and every manifest lies in the manifests' folder itself")
            continue
        try:
            read(path)
        except Refused as refusal:
            problems.append(f"{path.name}: {refusal}")
    return problems


def verify(manifest: Manifest, path: str, downloaded: Path) -> None:
    """Refuses a downloaded file the manifest does not list, or whose SHA-256 is not the manifest's."""
    listed = {file.path: file for file in manifest.files}.get(path)
    if listed is None:
        raise Refused(f"{path}: the manifest of {manifest.repository} does not list it")
    digest = hashlib.sha256()
    with downloaded.open("rb") as content:
        while chunk := content.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != listed.sha256:
        raise Refused(f"{path}: its SHA-256 is not the one in the manifest of {manifest.repository}")


def _https_only(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise Refused(f"a request to {request.url.host} would not go over HTTPS")


def _listing(client: httpx.Client, repository: str, revision: str) -> dict[str, dict[str, object]]:
    """The repository's files at the revision, by path, each as Hugging Face describes it: its
    size, and its LFS metadata when it is stored in LFS."""
    response = client.get(f"{HUB}/api/models/{repository}/revision/{revision}", params={"blobs": "true"})
    response.raise_for_status()
    try:
        answer = response.json()
        if answer["id"] != repository:
            raise Refused(f"Hugging Face writes this repository's id {answer['id']}")
        if answer["sha"] != revision:
            raise Refused(f"Hugging Face answered for the commit {answer['sha']}")
        return {str(sibling["rfilename"]): sibling for sibling in answer["siblings"]}
    except (KeyError, TypeError, ValueError):
        raise Refused("Hugging Face's answer is not a model's description") from None


def _downloaded(client: httpx.Client, repository: str, revision: str, path: str) -> File:
    """The file as downloaded at the revision: its size, and the SHA-256 of its content."""
    digest, size = hashlib.sha256(), 0
    with client.stream("GET", f"{HUB}/{repository}/resolve/{revision}/{quote(path)}") as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            digest.update(chunk)
            size += len(chunk)
    return File(path, size, digest.hexdigest())


def build(client: httpx.Client, repository: str, revision: str) -> tuple[Manifest, Selection]:
    """The manifest of the repository at the revision, from Hugging Face, and what was refused."""
    manifest_name(repository)
    if not REVISION.fullmatch(revision):
        raise Refused("the revision must be a full commit hash, 40 lower-case hexadecimal characters")
    listing = _listing(client, repository, revision)
    selection = select(sorted(listing))
    files = []
    for path in selection.copied:
        described = listing[path]
        lfs = described.get("lfs")
        if isinstance(lfs, dict):
            files.append({"path": path, "size": lfs.get("size"), "sha256": lfs.get("sha256")})
            continue
        file = _downloaded(client, repository, revision, path)
        if file.size != described.get("size"):
            raise Refused(f"{path}: the download has {file.size} bytes, not the size Hugging Face lists")
        files.append({"path": file.path, "size": file.size, "sha256": file.sha256})
    try:
        return parse({"repository": repository, "revision": revision, "files": files}), selection
    except Refused as refusal:
        raise Refused(f"Hugging Face's answer does not make a manifest: {refusal}") from None


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None, manifests: Path = MANIFESTS
) -> int:
    parser = argparse.ArgumentParser(prog=NAME, description="Write a model repository's SHA-256 manifest.")
    parser.add_argument("repository", help="the repository's id as Hugging Face writes it: Qwen/Qwen3.8-27B-FP8")
    parser.add_argument("revision", help="the commit: a full hash, 40 lower-case hexadecimal characters")
    arguments = parser.parse_args(argv)
    try:
        # No token and no credentials: the client sends none unless given one. `transport` is how
        # a test answers in Hugging Face's place.
        with httpx.Client(
            transport=transport, follow_redirects=True, timeout=60, event_hooks={"request": [_https_only]}
        ) as client:
            manifest, selection = build(client, arguments.repository, arguments.revision)
    except httpx.HTTPStatusError as error:
        print(f"{NAME}: Hugging Face answered {error.response.status_code}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"{NAME}: no answer from Hugging Face ({type(error).__name__})", file=sys.stderr)
        return 1
    except Refused as refusal:
        print(f"{NAME}: {refusal}", file=sys.stderr)
        return 1
    document = {
        "repository": manifest.repository,
        "revision": manifest.revision,
        "files": [{"path": file.path, "size": file.size, "sha256": file.sha256} for file in manifest.files],
    }
    manifests.mkdir(parents=True, exist_ok=True)
    name = manifest_name(manifest.repository)
    (manifests / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    for path in selection.refused:
        print(f"{NAME}: not copied: {path}")
    size = sum(file.size for file in manifest.files)
    print(f"{NAME}: wrote {name}: {len(manifest.files)} files, {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
