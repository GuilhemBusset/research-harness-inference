"""The task spike S3 gives each harness (system design, section 12): `spikes/s3_task/`.

The task is a folder a harness works on in a scratch copy: an optimisation model to finish, solved
with HiGHS, and a check, `check_task.py`, which is a plain program and not a test. The check fails
on the task as given, on purpose, so nothing here runs it in place: each test copies the folder,
and runs the check there in a second interpreter. `spikes/s3_reference/` holds the reference fix,
as the files that differ, which a harness never sees.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TASK = REPO / "spikes" / "s3_task"
REFERENCE = REPO / "spikes" / "s3_reference"


def listing(folder: Path) -> dict[str, str]:
    """Every file of a folder: its path in the folder -> the SHA-256 of its content."""
    return {
        file.relative_to(folder).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(folder.rglob("*"))
        if file.is_file()
    }


def check_a_copy(scratch: Path, *, lay_over: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the task's check on a copy of the task, with a folder's files laid over it or as given.

    The task's own folder must come out as it went in: what the model and the check write, they
    write in the copy.
    """
    before = listing(TASK)
    shutil.copytree(TASK, scratch, ignore=shutil.ignore_patterns("__pycache__"))
    if lay_over:
        shutil.copytree(lay_over, scratch, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    # From another folder than the copy: the check finds its files by its own place.
    result = subprocess.run(
        [sys.executable, str(scratch / "check_task.py")],
        cwd=scratch.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert listing(TASK) == before
    return result


def test_the_check_fails_on_the_task_as_given(tmp_path: Path) -> None:
    result = check_a_copy(tmp_path / "task")

    assert result.returncode != 0, result.stdout
    # The check's own verdict on an unfinished model, not a program that could not start.
    assert "FAIL" in result.stdout, result.stdout + result.stderr
    assert not (tmp_path / "task" / "plan.tex").exists()


def test_the_check_passes_with_the_reference_fix(tmp_path: Path) -> None:
    result = check_a_copy(tmp_path / "task", lay_over=REFERENCE)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "task" / "plan.tex").is_file()


def test_the_reference_holds_only_the_files_that_differ() -> None:
    task, reference = listing(TASK), listing(REFERENCE)
    reference = {path: digest for path, digest in reference.items() if "__pycache__" not in path}

    assert reference
    for path, digest in reference.items():
        assert path in task, path
        assert task[path] != digest, path
    # The check and the data are the task's own: the fix is in the model.
    assert not {"check_task.py", "instance.json"} & set(reference)


def test_ruff_and_ty_leave_the_task_and_its_reference_out() -> None:
    folders = {"spikes/s3_task", "spikes/s3_reference"}
    ruff = tomllib.loads((REPO / "ruff.toml").read_text(encoding="utf-8"))
    ty = tomllib.loads((REPO / "ty.toml").read_text(encoding="utf-8"))

    assert folders <= set(ruff["extend-exclude"])
    assert folders <= set(ty["src"]["exclude"])
