"""The mirror of the images the pipeline's builds pull (scripts/pipeline/mirror_images.py): it lists
them from the source, each by its digest, and copies only the digests the mirror lacks. A missing
repository is left for later, and a refused copy fails the run only after every image was tried.

Stand-ins answer in the place of ECR and of crane.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

import mirror_images

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

HOST = "123456789012.dkr.ecr.eu-west-1.amazonaws.com"
PINNED = re.compile(r"sha256:[0-9a-f]{64}")
FIVE = {"library/python", "grafana/grafana-oss", "vllm/vllm-openai", "tonistiigi/binfmt", "moby/buildkit"}


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": ""}}, "DescribeImages")


class Ecr:
    """Holds the digests of `held`, by repository; `absent` repositories do not exist."""

    def __init__(self, held: dict[str, set[str]], absent: frozenset[str] = frozenset()) -> None:
        self.held = held
        self.absent = absent

    def describe_images(self, repositoryName: str, imageIds: list[dict[str, str]]) -> dict[str, Any]:
        if repositoryName in self.absent:
            raise error("RepositoryNotFoundException")
        if imageIds[0]["imageDigest"] not in self.held.get(repositoryName, set()):
            raise error("ImageNotFoundException")
        return {"imageDetails": [{}]}

    def get_authorization_token(self) -> dict[str, Any]:
        return {"authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:password").decode()}]}


class Crane:
    """Records every call; a copy from a source in `refused` fails."""

    def __init__(self, refused: frozenset[str] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.refused = refused

    def __call__(self, args: Sequence[str], stdin: str | None = None) -> int:
        self.calls.append(list(args))
        return 1 if args[0] == "copy" and args[1] in self.refused else 0

    def copies(self) -> list[tuple[str, str]]:
        return [(call[1], call[2]) for call in self.calls if call[0] == "copy"]


@pytest.fixture(scope="module")
def images() -> list[mirror_images.Image]:
    return mirror_images.listed()


def test_today_s_tree_lists_the_five_images_each_with_its_digest(images: list[mirror_images.Image]) -> None:
    assert sorted(image.path for image in images) == sorted(FIVE)
    # Each by the digest its Dockerfile or the pipeline stack pins it at.
    sources = [mirror_images.REPO / mirror_images.PIPELINE_STACK, *mirror_images.REPO.glob("services/**/Dockerfile")]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    for image in images:
        assert PINNED.fullmatch(image.digest)
        assert f"{image.path.removeprefix('library/')}:{image.tag}@{image.digest}" in text


def test_lists_from_lines_but_scratch_and_stages_and_refuses_an_unpinned_image(tmp_path: Path) -> None:
    pin = "sha256:" + "a" * 64
    (tmp_path / "src/rhi/pipeline").mkdir(parents=True)
    (tmp_path / mirror_images.PIPELINE_STACK).write_text(
        f'EMULATOR_IMAGE = "tonistiigi/binfmt@{pin}"\nBUILDKIT_IMAGE = "docker.io/moby/buildkit:v1@{pin}"\n'
    )
    service = tmp_path / "services/one"
    service.mkdir(parents=True)
    (service / "Dockerfile").write_text(
        f"FROM --platform=$BUILDPLATFORM python:3@{pin} \\\n    # a comment inside the instruction\n"
        f"    AS build\nFROM build AS test\nFROM scratch\nfrom owner/tool@{pin}\n"
    )
    assert [(i.path, i.tag) for i in mirror_images.listed(tmp_path)] == [
        ("library/python", "3"),
        ("owner/tool", None),
        ("tonistiigi/binfmt", None),
        ("moby/buildkit", "v1"),
    ]
    (service / "Dockerfile").write_text("FROM python:3\n")
    with pytest.raises(mirror_images.ListingError):
        mirror_images.listed(tmp_path)
    assert mirror_images.main({}, tmp_path) == 1


@pytest.mark.parametrize(
    "dockerfile",
    [
        "# escape=`\nFROM python@{pin}\n",
        "# syntax=docker/dockerfile:1\nFROM python@\\\n{pin} AS base\n",
        "# escape=`\nFROM python@`\n\n# a comment\n{pin}\n",
    ],
    ids=["after-a-directive", "within-the-reference", "escape-directive"],
)
def test_lists_a_pinned_base_after_a_directive_or_across_an_escaped_newline(
    tmp_path: Path, dockerfile: str
) -> None:
    pin = "sha256:" + "b" * 64
    (tmp_path / "src/rhi/pipeline").mkdir(parents=True)
    (tmp_path / mirror_images.PIPELINE_STACK).write_text(
        f'EMULATOR_IMAGE = "tonistiigi/binfmt@{pin}"\nBUILDKIT_IMAGE = "moby/buildkit@{pin}"\n'
    )
    service = tmp_path / "services/one"
    service.mkdir(parents=True)
    (service / "Dockerfile").write_text(dockerfile.format(pin=pin))
    assert ("library/python", pin) in {(i.path, i.digest) for i in mirror_images.listed(tmp_path)}


def test_a_run_copies_only_the_digests_the_mirror_lacks(images: list[mirror_images.Image]) -> None:
    held, lacking = images[:2], images[2:]
    crane = Crane()
    ecr = Ecr({image.repository: {image.digest} for image in held})
    assert mirror_images.mirror(images, ecr=ecr, crane=crane, host=HOST) == 0
    assert crane.copies() == [(f"docker.io/{i.path}@{i.digest}", i.destination(HOST)) for i in lacking]
    # Logged in once, to the private registry alone.
    assert [call[:3] for call in crane.calls if call[0] == "auth"] == [["auth", "login", HOST]]


def test_a_run_after_a_complete_copy_copies_nothing_and_reads_no_public_registry(
    images: list[mirror_images.Image],
) -> None:
    crane = Crane()
    ecr = Ecr({image.repository: {image.digest} for image in images})
    assert mirror_images.mirror(images, ecr=ecr, crane=crane, host=HOST) == 0
    assert crane.calls == []


def test_an_image_with_no_repository_yet_is_skipped_while_the_others_are_copied(
    images: list[mirror_images.Image],
) -> None:
    crane = Crane()
    new, *others = images
    ecr = Ecr({}, absent=frozenset({new.repository}))
    assert mirror_images.mirror(images, ecr=ecr, crane=crane, host=HOST) == 0
    assert [source for source, _ in crane.copies()] == [image.source for image in others]


def test_a_refused_copy_fails_the_run_only_after_the_other_images_were_tried(
    images: list[mirror_images.Image],
) -> None:
    crane = Crane(refused=frozenset({images[0].source}))
    assert mirror_images.mirror(images, ecr=Ecr({}), crane=crane, host=HOST) == 1
    assert [source for source, _ in crane.copies()] == [image.source for image in images]
