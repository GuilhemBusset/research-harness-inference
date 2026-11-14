"""The step container-tests of the gate script (system design, section 8.3; ADR 0001, amendment 5).

The step is run on a copy of `scripts/check.sh` in a throwaway tree, beside the repository's own
pytest configuration and a `tests/container/` the test fills. Two programs on the PATH are
stand-ins, since this host has no container runtime and the tree has no lock:

- `docker` answers, or does not, as the test says;
- `uv` records its arguments and runs pytest from the interpreter of this test run.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

PASSES = "def test_it():\n    pass\n"
SKIPS = "import pytest\n\n\ndef test_it():\n    pytest.skip('the image is missing')\n"
FAILS = "def test_it():\n    assert False\n"


@pytest.fixture
def run_step(tmp_path: Path):
    """Run the step in a tree of its own; returns the finished process and what `uv` was given."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "tests" / "container").mkdir(parents=True)
    (tree / "tests" / "container" / "README.md").write_text("Not a test.\n", encoding="utf-8")
    for name in ("scripts/check.sh", "pyproject.toml"):
        (tree / name).write_text((REPO / name).read_text(encoding="utf-8"), encoding="utf-8")
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    uv_calls = tmp_path / "uv-calls"

    def stand_in(name: str, body: str) -> None:
        path = stand_ins / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    stand_in(
        "uv",
        f'echo "$*" >> {shlex.quote(str(uv_calls))}\n'
        '[ "$1 $2 $3" = "run --locked pytest" ] || exit 64\n'
        "shift 3\n"
        f'exec {shlex.quote(sys.executable)} -m pytest "$@"',
    )

    def run(*, test: str | None, runtime: bool, ci: bool = False) -> tuple[subprocess.CompletedProcess[str], str]:
        if test is not None:
            (tree / "tests" / "container" / "test_it.py").write_text(test, encoding="utf-8")
        stand_in("docker", f'[ "$1" = info ] && exit {0 if runtime else 1}')
        env = {key: value for key, value in os.environ.items() if key != "CI"}
        env["PATH"] = f"{stand_ins}{os.pathsep}{env['PATH']}"
        if ci:
            env["CI"] = "true"
        done = subprocess.run(
            ["bash", "scripts/check.sh", "container-tests"],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return done, uv_calls.read_text(encoding="utf-8") if uv_calls.exists() else ""

    return run


@pytest.mark.parametrize("runtime", [False, True])
def test_an_empty_folder_passes_and_says_so(run_step, runtime: bool) -> None:
    done, uv_calls = run_step(test=None, runtime=runtime, ci=True)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "holds no test" in done.stdout
    assert uv_calls == ""


def test_no_runtime_fails_the_step_before_any_test_runs(run_step) -> None:
    done, uv_calls = run_step(test=PASSES, runtime=False)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "no container runtime answers" in done.stderr
    assert uv_calls == ""


def test_the_folder_runs_with_the_locked_pytest(run_step) -> None:
    done, uv_calls = run_step(test=PASSES, runtime=True, ci=True)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 collected, 0 skipped" in done.stdout
    (call,) = uv_calls.splitlines()
    assert call.startswith("run --locked pytest ")
    assert call.endswith(" tests/container")


def test_a_failing_test_fails_the_step(run_step) -> None:
    done, _ = run_step(test=FAILS, runtime=True)

    assert done.returncode == 1, done.stdout + done.stderr


@pytest.mark.parametrize(("ci", "status"), [(True, 1), (False, 0)])
def test_a_skipped_test_fails_the_step_under_ci_only(run_step, ci: bool, status: int) -> None:
    done, _ = run_step(test=SKIPS, runtime=True, ci=ci)

    assert done.returncode == status, done.stdout + done.stderr
    assert "1 collected, 1 skipped" in done.stdout
