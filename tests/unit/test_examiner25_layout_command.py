from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def test_required_layout_command_passes(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    report = tmp_path / "layout.xml"
    result = subprocess.run(
        ["uv", "run", "--locked", "pytest", "tests/unit/test_layout.py", f"--junitxml={report}"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = ET.parse(report).findall(".//testcase")
    assert cases
    assert all(len(case) == 0 for case in cases)
