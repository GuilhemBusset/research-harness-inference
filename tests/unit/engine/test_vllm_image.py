"""The vLLM engine's image (`services/engine/vllm/`; system design, sections 4.1 and 4.3).

Nothing is built here. The Dockerfile and the version file are read, and the entry point runs as
the image runs it, with a stand-in for vLLM that records what it was started with. The secret is
made at each run.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
from pathlib import Path

import pytest

IMAGE = Path(__file__).resolve().parents[3] / "services/engine/vllm"
DOCKERFILE = (IMAGE / "Dockerfile").read_text(encoding="utf-8")
VERSION = (IMAGE / "VERSION").read_text(encoding="utf-8")
ENTRY_POINT = IMAGE / "entrypoint.sh"

# The first vLLM that keeps a request's `thinking` field and has the metric names of section 7.2.
FLOOR = (0, 33, 0)
SET = ("VLLM_NO_USAGE_STATS", "DO_NOT_TRACK", "HF_HUB_OFFLINE")
NEVER_SET = ("VLLM_SERVER_DEV_MODE", "VLLM_TORCH_PROFILER_DIR")

# vLLM, for the entry point: it writes its key and then its arguments, each ended by a NUL.
STAND_IN = """#!/bin/sh
printf '%s\\0' "${VLLM_API_KEY-}" "$@" >"$STAND_IN_RECORD"
"""


def instructions(dockerfile: str) -> list[tuple[str, str]]:
    """Each instruction as its keyword and the rest, comments left out and continued lines joined."""
    lines = [line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    parts = [line.strip().partition(" ") for line in joined.split("\n") if line.strip()]
    return [(keyword.upper(), rest.strip()) for keyword, _, rest in parts]


def the(keyword: str, dockerfile: str = DOCKERFILE) -> list[str]:
    return [rest for found, rest in instructions(dockerfile) if found == keyword]


def numbers(version: str) -> tuple[int, ...]:
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    found = tuple(int(part) for part in version.split("."))
    assert found >= FLOOR, f"vLLM {version} is below {'.'.join(map(str, FLOOR))}"
    return found


def base_version(dockerfile: str) -> tuple[int, ...]:
    """The upstream version the one base is named at. It fails unless a digest names the image."""
    (base,) = the("FROM", dockerfile)
    named = re.fullmatch(r"vllm/vllm-openai:v([0-9.]+)@sha256:[0-9a-f]{64}", base)
    assert named, f"{base} is not the upstream image by digest"
    return numbers(named[1])


def recorded_version(text: str) -> tuple[int, ...]:
    assert text.endswith("\n") and text.count("\n") == 1, "VERSION holds one line"
    return numbers(text.strip())


def started(tmp_path: Path, arguments: list[str], **environ: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The image's entry point with `arguments`, as the Dockerfile names it. `PATH` holds the
    stand-in and nothing else, so the entry point calls no other program."""
    copied, (entry_point,) = dict(copy.split() for copy in the("COPY")), the("ENTRYPOINT")
    assert json.loads(entry_point) == ["/bin/sh", copied[ENTRY_POINT.name]]
    programs, record = tmp_path / "bin", tmp_path / "started"
    programs.mkdir()
    (programs / "vllm").write_text(STAND_IN, encoding="utf-8")
    (programs / "vllm").chmod(0o755)
    ran = subprocess.run(
        ["/bin/sh", str(ENTRY_POINT), *arguments],
        env={"PATH": str(programs), "STAND_IN_RECORD": str(record), **environ},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return ran, record


def test_the_base_is_upstream_vllm_by_digest_at_the_recorded_version() -> None:
    assert base_version(DOCKERFILE) == recorded_version(VERSION)

    # A base named by its tag alone is refused, and so is a version below the floor, wherever it
    # is written. 0.9.2 is below 0.33.0 as numbers and above it as text.
    with pytest.raises(AssertionError, match="by digest"):
        base_version(re.sub(r"@sha256:[0-9a-f]{64}", "", DOCKERFILE))
    for below in ("0.32.9", "0.9.2"):
        with pytest.raises(AssertionError, match="below"):
            base_version(DOCKERFILE.replace(f":v{VERSION.strip()}@", f":v{below}@"))
        with pytest.raises(AssertionError, match="below"):
            recorded_version(f"{below}\n")


def test_the_build_context_holds_four_files() -> None:
    assert sorted(path.name for path in IMAGE.iterdir()) == ["Dockerfile", "VERSION", "entrypoint.sh", "fetch_model.py"]


def test_the_contract_environment() -> None:
    pairs = [pair.partition("=") for rest in the("ENV") for pair in shlex.split(rest)]
    environment = {name: value for name, _, value in pairs}
    assert {name: environment.get(name) for name in SET} == dict.fromkeys(SET, "1")

    # Neither of the two that open development and profiling routes, in the image's instructions
    # or in what the entry point runs.
    commands = [line for line in ENTRY_POINT.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    written = "\n".join([*(rest for _, rest in instructions(DOCKERFILE)), *commands])
    assert [name for name in NEVER_SET if name in written] == []


def test_the_secret_reaches_vllm(tmp_path: Path) -> None:
    secret = secrets.token_urlsafe(36)
    arguments = ["/models/coursework", "--served-model-name", "a b", "it's", "*", "", "--chat-template=$HOME\n"]

    ran, record = started(tmp_path, arguments, RHI_POOL_SECRET=secret)

    assert ran.returncode == 0, ran.stderr
    key, *given = record.read_text(encoding="utf-8").split("\0")[:-1]
    assert key == secret
    # vLLM's server, as upstream's own entry point starts it, then the container's arguments.
    assert given == ["serve", *arguments]
    assert not [word for word in given if secret in word]
    assert secret not in ran.stdout + ran.stderr
    # The image has no command of its own: what follows the entry point is the release's alone.
    assert the("CMD") == []


@pytest.mark.parametrize("environ", [{}, {"RHI_POOL_SECRET": ""}], ids=["unset", "empty"])
def test_vllm_is_not_started_without_the_secret(tmp_path: Path, environ: dict[str, str]) -> None:
    ran, record = started(tmp_path, ["/models/coursework"], **environ)

    assert ran.returncode != 0
    assert not record.exists()
