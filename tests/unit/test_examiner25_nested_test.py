from __future__ import annotations

import runpy
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def test_named_regression_uses_repo_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    layout = runpy.run_path(str(repo / "tests/unit/test_layout.py"))
    calls = []
    original = subprocess.run
    report = tmp_path / "nested.xml"

    def record(*args, **kwargs):
        command = [*args[0], f"--junitxml={report}"]
        result = original(command, **kwargs)
        calls.append((args, kwargs, result))
        return result

    monkeypatch.setattr(subprocess, "run", record)
    layout["test_same_named_test_files_both_run"](tmp_path)
    assert len(calls) == 1
    args, kwargs, result = calls[0]
    command = args[0]
    assert Path(command[command.index("-c") + 1]).resolve() == repo / "pyproject.toml"
    assert Path(kwargs["cwd"]).resolve() == repo
    files = list(tmp_path.rglob("test_*.py"))
    assert len(files) == 2
    assert files[0].name == files[1].name
    assert files[0].parent != files[1].parent
    assert result.returncode == 0
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 2
    assert all(len(case) == 0 for case in cases)
