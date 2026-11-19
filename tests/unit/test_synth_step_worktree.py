"""The synth step's `git init` (src/rhi/pipeline/synth.py): the source CodeBuild receives has no
`.git`, and the tests read the tree through git. On a copy of the source with no `.git`, plus what
a build leaves behind, the step's own command makes a work tree in which git lists exactly the
source, and the hygiene scan of the repository passes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from rhi import config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _git_files(root: Path) -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    return {name for name in listed.decode().split("\0") if name and (root / name).is_file()}


def test_the_step_gives_the_tests_a_work_tree_of_the_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The step's module loads aws-cdk-lib, whose runtime refuses a shared package cache.
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    from rhi.pipeline import synth

    source = tmp_path / "src"
    files = _git_files(config.REPO_ROOT)
    for name in files:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config.REPO_ROOT / name, source / name, follow_symlinks=False)
    assert not (source / ".git").exists()
    # What the build adds before the tests: synthesized templates, the lookup cache. The scanner
    # refuses both by name, so a work tree that listed them would fail the scan.
    (source / "cdk.out").mkdir()
    (source / "cdk.out" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (source / "cdk.context.json").write_text("{}\n", encoding="utf-8")

    [init] = [command for command in synth.COMMANDS if command.startswith("git init")]
    assert synth.COMMANDS.index(init) < synth.COMMANDS.index("uv run --locked pytest")
    subprocess.run(init, shell=True, cwd=source, check=True)

    assert _git_files(source) == files
    scan = subprocess.run(
        [sys.executable, "tools/hygiene/scan.py"], cwd=source, capture_output=True, text=True, check=False
    )
    assert scan.returncode == 0, scan.stdout + scan.stderr
