"""The fallback loader's copy (`services/engine/vllm/fetch_model.py` and `entrypoint.sh`; system
design, section 4.4).

The entry point runs as the image runs it. Its `PATH` holds a stand-in for vLLM, which records what
it was started with and what the model's folder held then, and a `python3` that runs the test's own
interpreter. A stub `boto3`, first on `PYTHONPATH`, serves a bucket from a temporary folder. The
program is also loaded by its path, which is no package. The secret is made at each run.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from rhi.pools import vllm_command
from rhi_common.pool_config import parse_pools

if TYPE_CHECKING:
    from types import ModuleType

IMAGE = Path(__file__).resolve().parents[3] / "services/engine/vllm"
PROGRAM = IMAGE / "fetch_model.py"
ENTRY_POINT = IMAGE / "entrypoint.sh"

BUCKET = "example-models"
REVISION = hashlib.sha1(b"a pinned commit", usedforsecurity=False).hexdigest()
PREFIX = f"coursework/{REVISION}/"
# Three files of a model, one of them in a folder of its own.
MODEL = {
    "config.json": b'{"architectures": ["Qwen3ForCausalLM"]}',
    "model-00001-of-00001.safetensors": secrets.token_bytes(4096),
    "tokenizer/chat_template.jinja": b"{{ messages }}",
}
COURSEWORK = {
    "engine": "vllm",
    "model": "example/model",
    "revision": REVISION,
    "instance_types": ["g7e.2xlarge"],
    "prices": {"g7e.2xlarge": 3.36},
    "max_instances": 2,
    "warm_instances": 2,
    "wake": "on-demand",
    "target_concurrency": 8,
    "gpu_hours_per_day": 16,
    "context_length": 262144,
    "largest_output": 32000,
}

# boto3, for the program: the bucket is the folder STUB_BUCKET, under the name STUB_BUCKET_NAME,
# listed one object to a page. Each call is a line of STUB_CALLS.
STUB_BOTO3 = """
import json, os
from pathlib import Path

def _record(call):
    with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as calls:
        calls.write(json.dumps(call) + "\\n")

class _Pages:
    def paginate(self, Bucket, Prefix):
        _record(["list", Bucket, Prefix])
        assert Bucket == os.environ["STUB_BUCKET_NAME"]
        root = Path(os.environ["STUB_BUCKET"])
        keys = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
        for key in keys:
            if key.startswith(Prefix):
                yield {"Contents": [{"Key": key}]}

class _S3:
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Pages()

    def download_file(self, Bucket, Key, Filename):
        _record(["get", Bucket, Key])
        Path(Filename).write_bytes((Path(os.environ["STUB_BUCKET"]) / Key).read_bytes())

def client(service):
    assert service == "s3"
    return _S3()
"""

# vLLM, for the entry point: it records its arguments and every file of the model's folder, as it
# finds them when it starts.
STAND_IN = """#!{python}
import json, os, sys
from pathlib import Path

folder = Path(os.environ["MODEL_FOLDER"])
files = {{path.relative_to(folder).as_posix(): path.read_bytes().hex() for path in folder.rglob("*") if path.is_file()}}
Path(os.environ["STAND_IN_RECORD"]).write_text(json.dumps({{"arguments": sys.argv[1:], "files": files}}))
"""


def program() -> ModuleType:
    """The program, loaded by its path. No bytecode is written beside it: that folder is the image's."""
    spec = importlib.util.spec_from_file_location("fetch_model", PROGRAM)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes
    return module


def bucket(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """The stub bucket: the model's files under its prefix, and one object of another revision."""
    root = tmp_path / "bucket"
    for key, content in {**{PREFIX + name: data for name, data in files.items()}, "coursework/other/x": b"x"}.items():
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(content)
    return root


def started(
    tmp_path: Path, arguments: list[str], root: Path, folder: Path
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The entry point with `arguments`, and the file of the stub bucket's calls."""
    programs, stubs, record, calls = tmp_path / "bin", tmp_path / "stubs", tmp_path / "started", tmp_path / "calls"
    programs.mkdir()
    (stubs / "boto3").mkdir(parents=True)
    (stubs / "boto3" / "__init__.py").write_text(STUB_BOTO3, encoding="utf-8")
    (programs / "vllm").write_text(STAND_IN.format(python=sys.executable), encoding="utf-8")
    (programs / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    for name in ("vllm", "python3"):
        (programs / name).chmod(0o755)
    environ = {
        "PATH": str(programs),
        "PYTHONPATH": str(stubs),
        "PYTHONDONTWRITEBYTECODE": "1",
        "RHI_POOL_SECRET": secrets.token_urlsafe(36),
        "STUB_BUCKET": str(root),
        "STUB_BUCKET_NAME": BUCKET,
        "STUB_CALLS": str(calls),
        "STAND_IN_RECORD": str(record),
        "MODEL_FOLDER": str(folder),
    }
    ran = subprocess.run(
        ["/bin/sh", str(ENTRY_POINT), *arguments], env=environ, capture_output=True, text=True, timeout=60, check=False
    )
    return ran, record


def download_arguments(folder: Path) -> list[str]:
    """A pool on download's engine flags, with its local folder at `folder`."""
    (pool,) = parse_pools({"coursework": {**COURSEWORK, "loader": "download"}})
    flags = vllm_command.arguments(pool, bucket=BUCKET)
    return [flag.replace(vllm_command.MODEL_FOLDER, str(folder)) for flag in flags]


def test_the_entry_point_copies_the_model_before_it_starts_vllm(tmp_path: Path) -> None:
    folder = tmp_path / "model"
    arguments = download_arguments(folder)

    ran, record = started(tmp_path, arguments, bucket(tmp_path, MODEL), folder)

    assert ran.returncode == 0, ran.stderr
    seen = json.loads(record.read_text(encoding="utf-8"))
    # All three files were in the folder, as the bucket holds them, when vLLM started.
    assert {name: bytes.fromhex(data) for name, data in seen["files"].items()} == MODEL
    # vLLM's server with the rest of the arguments: the copy's argument is taken off.
    assert seen["arguments"] == ["serve", *arguments[1:]]
    assert seen["arguments"][1] == str(folder)
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text(encoding="utf-8").splitlines()]
    assert calls[0] == ["list", BUCKET, PREFIX]
    assert sorted(calls[1:]) == sorted(["get", BUCKET, PREFIX + name] for name in MODEL)


def test_a_streamer_pools_arguments_copy_nothing(tmp_path: Path) -> None:
    (pool,) = parse_pools({"coursework": COURSEWORK})
    arguments = vllm_command.arguments(pool, bucket=BUCKET)
    folder = tmp_path / "model"

    ran, record = started(tmp_path, arguments, bucket(tmp_path, MODEL), folder)

    assert ran.returncode == 0, ran.stderr
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["arguments"] == ["serve", *arguments]
    assert seen["files"] == {}
    # Neither the bucket nor the folder was touched.
    assert not (tmp_path / "calls").exists()
    assert not folder.exists()


def test_vllm_does_not_start_when_the_copy_fails(tmp_path: Path) -> None:
    folder = tmp_path / "model"
    # A location that holds no file: a revision that was never copied.
    ran, record = started(tmp_path, download_arguments(folder), bucket(tmp_path, {}), folder)

    assert ran.returncode != 0
    assert "holds no file" in ran.stderr
    assert not record.exists()


def test_the_program_copies_every_page_and_refuses_a_key_that_leaves_the_folder(tmp_path: Path) -> None:
    fetch = program()
    assert fetch.parse(f"--rhi-fetch-model=s3://{BUCKET}/{PREFIX},{tmp_path}") == (BUCKET, PREFIX, tmp_path)
    with pytest.raises(fetch.FetchError):
        fetch.parse(f"--rhi-fetch-model={tmp_path}")

    class Client:
        def __init__(self, pages: list[list[str]]) -> None:
            self.pages = pages

        def get_paginator(self, name: str) -> Client:
            return self

        def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, object]]:
            return [{"Contents": [{"Key": Prefix + key} for key in page]} for page in self.pages]

        def download_file(self, bucket: str, key: str, filename: str) -> None:
            Path(filename).write_text(key, encoding="utf-8")

    # Every page of the listing, and no folder's marker.
    pages = [["a.json", "sub/"], ["sub/b.bin"], [], ["c.txt"]]
    written = fetch.copy(Client(pages), BUCKET, PREFIX, tmp_path / "model")
    assert sorted(path.relative_to(tmp_path / "model").as_posix() for path in written) == [
        "a.json",
        "c.txt",
        "sub/b.bin",
    ]

    with pytest.raises(fetch.FetchError):
        fetch.copy(Client([["../outside"]]), BUCKET, PREFIX, tmp_path / "other")
    assert not (tmp_path / "outside").exists()


def test_the_image_carries_the_program_where_the_entry_point_runs_it() -> None:
    lines = [line.split() for line in (IMAGE / "Dockerfile").read_text(encoding="utf-8").splitlines()]
    copied = {line[1]: line[2] for line in lines if line[:1] == ["COPY"]}
    entry_point = copied[ENTRY_POINT.name]
    # The entry point runs the program from its own folder in the image.
    assert copied[PROGRAM.name] == f"{entry_point.rsplit('/', 1)[0]}/{PROGRAM.name}"
    assert '"${0%/*}/fetch_model.py"' in ENTRY_POINT.read_text(encoding="utf-8")
