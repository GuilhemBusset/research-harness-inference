"""The rule for model weights (scripts/pipeline/weights.py): the allow-list of file types, the
verification of a downloaded file, the manifest's format, and the program that writes a manifest.

The program is run against recorded answers of Hugging Face, under `weights_recorded/`, through a
transport that answers in its place, so no request leaves the process. `model_info.json` is the
answer to `GET /api/models/Qwen/Qwen3.8-27B-FP8/revision/<commit>?blobs=true`, recorded on
2026-11-20 and cut down to the three keys the program reads and to seven of the repository's 81
files. `files/` holds the content of the two of them that are admitted and not in LFS, as
downloaded at that commit.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import weights

RECORDED = Path(__file__).with_name("weights_recorded")
INFO = json.loads((RECORDED / "model_info.json").read_text(encoding="utf-8"))
REPOSITORY, REVISION = INFO["id"], INFO["sha"]
NAME = "qwen--qwen3.8-27b-fp8.json"
CONTENT = b"the weights, as the manifest was written from them"
VALID = {
    "repository": REPOSITORY,
    "revision": REVISION,
    "files": [
        {"path": "model.safetensors", "size": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest()},
        {"path": "chat_template.jinja", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()},
    ],
}


def changed(**keys: Any) -> dict[str, Any]:
    """The valid manifest with some of its keys replaced."""
    return copy.deepcopy(VALID) | keys


def written(folder: Path, document: object, name: str = NAME) -> Path:
    path = folder / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def hub(requests: list[httpx.Request], info: dict[str, Any] = INFO) -> httpx.MockTransport:
    """Hugging Face, from the recorded answers. A download is answered as it was recorded: by a
    redirection to a path of the same host, which serves the content."""
    described = f"/api/models/{REPOSITORY}/revision/{REVISION}"
    resolved = f"/{REPOSITORY}/resolve/{REVISION}/"
    served = f"/api/resolve-cache/models/{REPOSITORY}/{REVISION}/"

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == described and request.url.params["blobs"] == "true":
            return httpx.Response(200, json=info)
        if path.startswith(resolved):
            return httpx.Response(307, headers={"location": served + path.removeprefix(resolved)})
        if path.startswith(served) and (RECORDED / "files" / path.removeprefix(served)).is_file():
            return httpx.Response(200, content=(RECORDED / "files" / path.removeprefix(served)).read_bytes())
        return httpx.Response(404)

    return httpx.MockTransport(answer)


# The allow-list.


def test_a_listing_copies_neither_its_pickle_file_nor_its_python_file() -> None:
    selection = weights.select(["config.json", "pytorch_model.bin", "modeling_qwen.py", "model.safetensors"])

    assert selection.copied == ("config.json", "model.safetensors")
    assert selection.refused == ("pytorch_model.bin", "modeling_qwen.py")


def test_the_chat_template_is_copied() -> None:
    assert weights.select(["chat_template.jinja"]).copied == ("chat_template.jinja",)


@pytest.mark.parametrize(
    "path",
    ["layers-0.safetensors", "model.safetensors.index.json", "merges.txt", "tokenizer.model", "onnx/config.json"],
)
def test_safetensors_json_and_tokenizer_files_are_admitted(path: str) -> None:
    assert weights.admitted(path)


@pytest.mark.parametrize(
    "path",
    [
        *("model.pt", "model.pth", "model.pkl", "model.pickle", "model.ckpt", "training_args.bin"),
        *("README.md", ".gitattributes", "crc32.txt", "template.jinja", "model.safetensors.py", "json"),
        *("../config.json", "/config.json", "onnx//config.json"),
    ],
)
def test_anything_else_is_refused(path: str) -> None:
    assert not weights.admitted(path)
    assert weights.select([path]).refused == (path,)


# The verification.


def test_a_file_that_differs_from_the_manifest_by_one_byte_is_refused(tmp_path: Path) -> None:
    manifest = weights.load(REPOSITORY, written(tmp_path, VALID).parent)
    downloaded = tmp_path / "download"

    downloaded.write_bytes(CONTENT)
    weights.verify(manifest, "model.safetensors", downloaded)

    downloaded.write_bytes(CONTENT[:-1] + b"n")
    with pytest.raises(weights.Refused, match="SHA-256"):
        weights.verify(manifest, "model.safetensors", downloaded)


def test_a_file_the_manifest_does_not_list_is_refused(tmp_path: Path) -> None:
    downloaded = tmp_path / "download"
    downloaded.write_bytes(CONTENT)

    with pytest.raises(weights.Refused, match="does not list"):
        weights.verify(weights.parse(VALID), "other.safetensors", downloaded)


def test_a_repository_that_has_no_manifest_is_refused(tmp_path: Path) -> None:
    written(tmp_path, VALID)

    assert weights.load(REPOSITORY, tmp_path) == weights.parse(VALID)
    with pytest.raises(weights.Refused, match="no manifest"):
        weights.load("Qwen/Qwen3.8-27B", tmp_path)
    with pytest.raises(weights.Refused, match="not of"):  # The same file name, another repository.
        weights.load(REPOSITORY.lower(), tmp_path)


# The format.


def test_a_branch_name_is_not_a_revision(tmp_path: Path) -> None:
    assert weights.read(written(tmp_path, VALID)).revision == REVISION

    for revision in ("main", REVISION[:7], REVISION[:39], REVISION.upper(), REVISION + "0"):
        with pytest.raises(weights.Refused, match="full commit hash"):
            weights.read(written(tmp_path, changed(revision=revision)))


@pytest.mark.parametrize(
    "document",
    [
        VALID | {"license": "apache-2.0"},
        {key: value for key, value in VALID.items() if key != "revision"},
        changed(files=[VALID["files"][0] | {"blob": "0" * 40}]),
        changed(files=[{"path": "model.safetensors", "sha256": "0" * 64}]),
        [VALID],
    ],
    ids=["one more key", "a key missing", "one more key of a file", "a key of a file missing", "not an object"],
)
def test_keys_other_than_the_formats_are_refused(tmp_path: Path, document: object) -> None:
    with pytest.raises(weights.Refused, match="exactly the keys"):
        weights.read(written(tmp_path, document))


@pytest.mark.parametrize(
    ("file", "reason"),
    [
        ({"path": "pytorch_model.bin", "size": 1, "sha256": "0" * 64}, "allow-list"),
        (VALID["files"][0], "twice"),
        ({"path": "config.json", "size": "1", "sha256": "0" * 64}, "size"),
        ({"path": "config.json", "size": 1, "sha256": "0" * 40}, "sha256"),
        ({"path": "config.json", "size": 1, "sha256": "A" * 64}, "sha256"),
    ],
    ids=["a pickle file", "a path twice", "a size that is no number", "a git blob id", "an upper-case digest"],
)
def test_a_file_of_a_manifest_is_held_to_the_format(tmp_path: Path, file: dict[str, Any], reason: str) -> None:
    with pytest.raises(weights.Refused, match=reason):
        weights.read(written(tmp_path, changed(files=[*VALID["files"], file])))


def test_committed_manifests_are_valid(tmp_path: Path) -> None:
    assert weights.invalid(weights.MANIFESTS) == []

    # The same check finds a manifest whose file name does not follow from its repository.
    written(tmp_path, VALID)
    assert weights.invalid(tmp_path) == []
    written(tmp_path, VALID, "qwen-qwen3.8-27b-fp8.json")
    written(tmp_path, VALID, "Qwen--Qwen3.8-27B-FP8.json")
    assert [problem.split(": ")[0] for problem in weights.invalid(tmp_path)] == [
        "Qwen--Qwen3.8-27B-FP8.json",
        "qwen-qwen3.8-27b-fp8.json",
    ]


# The program.


def test_the_program_writes_the_manifest_of_the_files_the_allow_list_admits(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    assert weights.main([REPOSITORY, REVISION], transport=hub(requests), manifests=tmp_path / "manifests") == 0

    assert [path.name for path in (tmp_path / "manifests").iterdir()] == [NAME]
    assert weights.invalid(tmp_path / "manifests") == []
    manifest = weights.load(REPOSITORY, tmp_path / "manifests")
    assert manifest.revision == REVISION
    # Of the seven files recorded, .gitattributes, README.md and crc32.txt are not copied.
    assert [(file.path, file.size) for file in manifest.files] == [
        ("generation_config.json", 202),
        ("layers-0.safetensors", 383865448),
        ("preprocessor_config.json", 390),
        ("tokenizer.json", 12809320),
    ]
    # Anonymously, over HTTPS, and from Hugging Face alone.
    assert all(request.url.scheme == "https" and request.url.host == "huggingface.co" for request in requests)
    assert not any("authorization" in request.headers or "cookie" in request.headers for request in requests)


def test_digests_come_from_lfs_or_the_file(tmp_path: Path) -> None:
    in_lfs, not_in_lfs = "layers-0.safetensors", "generation_config.json"
    listed = {sibling["rfilename"]: sibling for sibling in INFO["siblings"]}
    assert "lfs" in listed[in_lfs]
    assert "lfs" not in listed[not_in_lfs]
    requests: list[httpx.Request] = []
    info = INFO | {"siblings": [listed[in_lfs], listed[not_in_lfs]]}

    assert weights.main([REPOSITORY, REVISION], transport=hub(requests, info), manifests=tmp_path) == 0

    digests = {file.path: file.sha256 for file in weights.load(REPOSITORY, tmp_path).files}
    content = (RECORDED / "files" / not_in_lfs).read_bytes()
    assert digests == {
        in_lfs: listed[in_lfs]["lfs"]["sha256"],
        not_in_lfs: hashlib.sha256(content).hexdigest(),
    }
    # The file in LFS was not downloaded: its digest can only be the listing's.
    assert not any(request.url.path.endswith(in_lfs) for request in requests)
    assert any(request.url.path.endswith(not_in_lfs) for request in requests)


@pytest.mark.parametrize(
    ("revision", "info"),
    [
        ("main", INFO),
        (REVISION, INFO | {"sha": "0" * 40}),
        (REVISION, INFO | {"siblings": [{"rfilename": "generation_config.json", "size": 201}]}),
        (REVISION, INFO | {"siblings": [{"rfilename": "config.json", "size": 2}]}),
    ],
    ids=["a branch name", "another commit's listing", "a download of another size", "a file that is not served"],
)
def test_the_program_writes_no_manifest_it_cannot_stand_behind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], revision: str, info: dict[str, Any]
) -> None:
    assert weights.main([REPOSITORY, revision], transport=hub([], info), manifests=tmp_path) == 1

    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err.startswith("weights: ")


def test_a_redirection_away_from_https_is_not_followed(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/api/models/"):
            return httpx.Response(200, json=INFO)
        return httpx.Response(307, headers={"location": "http://huggingface.co/elsewhere"})

    assert weights.main([REPOSITORY, REVISION], transport=httpx.MockTransport(answer), manifests=tmp_path) == 1

    assert all(request.url.scheme == "https" for request in requests)
    assert list(tmp_path.iterdir()) == []
