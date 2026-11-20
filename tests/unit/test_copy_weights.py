"""The copy job (scripts/pipeline/copy_weights.py): each GPU pool's files, verified against the
manifest of its model, under the pool's prefix at its revision in the model bucket.

Stand-ins answer in the place of Hugging Face, of S3 and of SSM, so no request leaves the process.
The bucket's stand-in has a listing, an upload and a deletion, and nothing that reads an object.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from typing import TYPE_CHECKING, Any

import botocore.exceptions
import httpx
import pytest

import copy_weights
import weights
from rhi import config

if TYPE_CHECKING:
    from pathlib import Path

REPOSITORY = "example/model"
# Commit hashes made when the tests run: each is 40 hexadecimal characters that name no real commit.
REVISION = hashlib.sha1(b"the revision the pool pins", usedforsecurity=False).hexdigest()
OTHER_REVISION = hashlib.sha1(b"another revision", usedforsecurity=False).hexdigest()
BUCKET = "the-model-bucket"
POOL = "coursework"
PREFIX = f"{POOL}/{REVISION}/"
# The repository's files that the manifest lists, and two that no manifest may list.
FILES = {
    "chat_template.jinja": b"{{ messages }}",
    "config.json": b'{"architectures": ["Example"]}',
    "model.safetensors": b"the weights, as the manifest was written from them",
}
NOT_ADMITTED = {"pytorch_model.bin": b"a pickle file", "modeling_example.py": b"import os"}

EXAMPLE = tomllib.loads((config.REPO_ROOT / config.EXAMPLE_FILE).read_text(encoding="utf-8"))
MOCK = EXAMPLE["pools"]["mock"]


def gpu(revision: str = REVISION, model: str = REPOSITORY) -> dict[str, object]:
    """The entry of a GPU pool that pins a revision of a model."""
    return {
        "engine": "vllm",
        "model": model,
        "revision": revision,
        "instance_types": ["g7e.2xlarge"],
        "prices": {"g7e.2xlarge": 3.36},
        "max_instances": 1,
        "warm_instances": 0,
        "wake": "on-demand",
        "target_concurrency": 8,
        "gpu_hours_per_day": 16,
        "context_length": 262144,
        "largest_output": 32000,
    }


class Parameters:
    """An SSM client as far as the configuration's loader and its writer use one."""

    def __init__(self, pools: dict[str, dict[str, object]]) -> None:
        self.parameters: dict[str, dict[str, Any]] = {}
        data = EXAMPLE | {"pools": pools, "allowed_instance_types": ["t4g.small", "g7e.2xlarge"]}
        # The default pool is one of the pools, when there is one.
        del data["default_pool"]
        if pools:
            data["default_pool"] = next(iter(pools))
        config.write_ssm(config.parse(data, "test"), client=self)

    def put_parameter(self, **request: Any) -> None:
        self.parameters[request["Name"]] = {"Name": request["Name"], "Type": request["Type"], "Value": request["Value"]}

    def get_parameters_by_path(self, **request: Any) -> dict[str, Any]:
        assert request["Path"] == config.SSM_PREFIX
        return {"Parameters": list(self.parameters.values())}


class Hub:
    """Hugging Face, as far as the job asks it: a file of the repository at the revision. It
    records the path of every file asked for."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.asked: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https" and "authorization" not in request.headers
        resolved = f"/{REPOSITORY}/resolve/{REVISION}/"
        path = request.url.path.removeprefix(resolved)
        self.asked.append(path)
        if request.url.path.startswith(resolved) and path in self.files:
            return httpx.Response(200, content=self.files[path])
        return httpx.Response(404)


class Bucket:
    """The model bucket, as far as the job may use it: a listing, two keys a page, an upload and
    a deletion. It records every operation asked of it; any other, a read of an object among
    them, is recorded and fails."""

    def __init__(self, objects: dict[str, bytes] | None = None, *, refuses: str | None = None) -> None:
        self.objects = dict(objects or {})
        self.refuses = refuses
        self.operations: list[tuple[str, str]] = []

    def _asked(self, operation: str, request: dict[str, Any], named: str) -> str:
        assert request["Bucket"] == BUCKET
        self.operations.append((operation, request[named]))
        if operation == self.refuses:
            error = {"Code": "AccessDenied", "Message": "a message of AWS, which names the caller"}
            raise botocore.exceptions.ClientError({"Error": error}, operation)
        return request[named]

    def list_objects_v2(self, **request: Any) -> dict[str, Any]:
        prefix = self._asked("list_objects_v2", request, "Prefix")
        after = request.get("ContinuationToken", "")
        keys = sorted(key for key in self.objects if key.startswith(prefix) and key > after)
        page = {"Contents": [{"Key": key, "Size": len(self.objects[key])} for key in keys[:2]]}
        return page | ({"IsTruncated": True, "NextContinuationToken": keys[1]} if len(keys) > 2 else {})

    def upload_file(self, **request: Any) -> None:
        key = self._asked("upload_file", request, "Key")
        with open(request["Filename"], "rb") as content:
            self.objects[key] = content.read()

    def delete_object(self, **request: Any) -> None:
        del self.objects[self._asked("delete_object", request, "Key")]

    def __getattr__(self, operation: str) -> Any:
        self.operations.append((operation, ""))
        raise AssertionError(f"the job asked the bucket for {operation}")

    def did(self, operation: str) -> list[str]:
        """The keys, or the prefixes, an operation was asked for, in order."""
        return [named for asked, named in self.operations if asked == operation]


def manifest_of(files: dict[str, bytes], revision: str = REVISION) -> dict[str, object]:
    listed = [
        {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in sorted(files.items())
    ]
    return {"repository": REPOSITORY, "revision": revision, "files": listed}


def committed(folder: Path, document: dict[str, object]) -> Path:
    """The folder of manifests, holding the repository's."""
    folder.mkdir(exist_ok=True)
    (folder / weights.manifest_name(REPOSITORY)).write_text(json.dumps(document), encoding="utf-8")
    return folder


def run(pools: dict[str, dict[str, object]], bucket: Bucket, hub: Hub, manifests: Path) -> int:
    transport = httpx.MockTransport(hub.answer)
    return copy_weights.main(
        {"RHI_MODEL_BUCKET": BUCKET}, ssm=Parameters(pools), s3=bucket, transport=transport, manifests=manifests
    )


def test_the_manifest_s_files_and_no_other_are_written_under_the_pool_s_prefix_at_its_revision(tmp_path: Path) -> None:
    bucket, hub = Bucket(), Hub(FILES | NOT_ADMITTED)

    assert run({"mock": MOCK, POOL: gpu()}, bucket, hub, committed(tmp_path, manifest_of(FILES))) == 0

    assert bucket.objects == {PREFIX + path: content for path, content in FILES.items()}
    assert sorted(hub.asked) == sorted(FILES)


def test_a_manifest_that_lists_a_file_the_allow_list_refuses_copies_nothing(tmp_path: Path) -> None:
    bucket, hub = Bucket(), Hub(FILES | NOT_ADMITTED)
    manifests = committed(tmp_path, manifest_of(FILES | NOT_ADMITTED))

    assert run({POOL: gpu()}, bucket, hub, manifests) != 0

    assert (bucket.operations, hub.asked) == ([], [])


@pytest.mark.parametrize(
    "served",
    [b"the weights, as the manifest was written from the_", b"other weights"],
    ids=["of the manifest's size", "of another size"],
)
def test_a_file_that_is_not_the_manifest_s_is_not_written(tmp_path: Path, served: bytes) -> None:
    bucket, hub = Bucket(), Hub(FILES | {"model.safetensors": served})

    assert run({POOL: gpu()}, bucket, hub, committed(tmp_path, manifest_of(FILES))) != 0

    assert "model.safetensors" in hub.asked
    assert PREFIX + "model.safetensors" not in bucket.objects


def test_a_file_the_prefix_already_holds_at_its_size_is_not_downloaded_again(tmp_path: Path) -> None:
    # The weights are there at the manifest's size; the configuration file is there at another.
    there = {PREFIX + "model.safetensors": b"x" * len(FILES["model.safetensors"]), PREFIX + "config.json": b"{}"}
    bucket, hub = Bucket(there), Hub(FILES)

    assert run({POOL: gpu()}, bucket, hub, committed(tmp_path, manifest_of(FILES))) == 0

    assert sorted(hub.asked) == ["chat_template.jinja", "config.json"]
    assert sorted(bucket.did("upload_file")) == [PREFIX + "chat_template.jinja", PREFIX + "config.json"]
    assert bucket.objects[PREFIX + "config.json"] == FILES["config.json"]


def test_a_file_the_manifest_no_longer_lists_is_removed_and_other_prefixes_stay(tmp_path: Path) -> None:
    elsewhere = {
        f"{POOL}/{OTHER_REVISION}/model.safetensors": b"the revision in service",
        f"{POOL}/{OTHER_REVISION}/removed.safetensors": b"listed by that revision's manifest",
        f"another-pool/{REVISION}/removed.safetensors": b"another pool's",
    }
    bucket = Bucket(elsewhere | {PREFIX + "removed.safetensors": b"once listed", PREFIX + "tokenizer.json": b"{}"})

    assert run({POOL: gpu()}, bucket, Hub(FILES), committed(tmp_path, manifest_of(FILES))) == 0

    assert bucket.did("delete_object") == [PREFIX + "removed.safetensors", PREFIX + "tokenizer.json"]
    assert bucket.objects == elsewhere | {PREFIX + path: content for path, content in FILES.items()}
    # The job looked at nothing but the pool's prefix at its revision.
    assert set(bucket.did("list_objects_v2")) == {PREFIX}


@pytest.mark.parametrize(
    "out_of_order",
    [gpu(OTHER_REVISION), gpu(model="example/unlisted")],
    ids=["another revision than its manifest's", "a model with no manifest"],
)
def test_one_pool_out_of_order_stops_the_job_with_nothing_written_for_any_pool(
    tmp_path: Path, out_of_order: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    bucket, hub = Bucket(), Hub(FILES)
    pools = {POOL: gpu(), "second": out_of_order}

    assert run(pools, bucket, hub, committed(tmp_path, manifest_of(FILES))) != 0

    assert (bucket.operations, bucket.objects, hub.asked) == ([], {}, [])
    assert "pool second" in capsys.readouterr().err


@pytest.mark.parametrize("pools", [{"mock": MOCK}, {}], ids=["only the mock pool", "no pool"])
def test_a_configuration_with_no_gpu_pool_copies_nothing_and_passes(
    tmp_path: Path, pools: dict[str, dict[str, object]]
) -> None:
    bucket, hub = Bucket(), Hub(FILES)

    # With no committed manifest either: the folder does not exist.
    assert run(pools, bucket, hub, tmp_path / "manifests") == 0

    assert (bucket.operations, hub.asked) == ([], [])


def test_no_call_reads_an_object_of_the_bucket(tmp_path: Path) -> None:
    # A run that lists, skips, copies and removes: everything the job does to the bucket.
    there = {PREFIX + "model.safetensors": FILES["model.safetensors"], PREFIX + "removed.json": b"{}"}
    bucket = Bucket(there)

    assert run({POOL: gpu()}, bucket, Hub(FILES), committed(tmp_path, manifest_of(FILES))) == 0

    assert {operation for operation, _ in bucket.operations} == {"list_objects_v2", "upload_file", "delete_object"}


def test_a_write_the_bucket_refuses_fails_the_job_without_the_message_of_aws(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bucket = Bucket(refuses="upload_file")

    assert run({POOL: gpu()}, bucket, Hub(FILES), committed(tmp_path, manifest_of(FILES))) != 0

    printed = "".join(capsys.readouterr())
    assert "ClientError" in printed and "a message of AWS" not in printed
    assert bucket.objects == {}


def test_a_job_that_is_not_told_the_bucket_fails_before_it_reads_anything(tmp_path: Path) -> None:
    bucket = Bucket()

    assert copy_weights.main({}, ssm=Parameters({POOL: gpu()}), s3=bucket, manifests=tmp_path) != 0

    assert bucket.operations == []


def test_the_coursework_model_s_manifest_is_committed_at_a_pinned_revision() -> None:
    manifest = weights.load("Qwen/Qwen3.8-27B-FP8")

    # The publisher's 8-bit build, 30.9 GB, with its chat template in a file of its own (system
    # design, section 4.2), at the full commit hash the manifest's format asks for.
    assert weights.REVISION.fullmatch(manifest.revision)
    assert round(sum(file.size for file in manifest.files) / 1e9, 1) == 30.9
    paths = {file.path for file in manifest.files}
    assert "chat_template.jinja" in paths and any(path.endswith(".safetensors") for path in paths)
