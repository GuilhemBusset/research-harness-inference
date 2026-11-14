from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def test_duplicate_module_names_execute(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    for folder, number in (("synth", 1), ("policy", 2)):
        target = tmp_path / "tests" / folder
        target.mkdir(parents=True)
        (target / "test_pipeline.py").write_text(
            f"def test_case_{number}():\n    assert {number} > 0\n"
        )
    report = tmp_path / "results.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(repo / "pyproject.toml"),
         str(tmp_path / "tests"), f"--junitxml={report}"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = ET.parse(report).findall(".//testcase")
    assert {case.attrib["name"] for case in cases} == {"test_case_1", "test_case_2"}
    assert len(cases) == 2
    assert all(len(case) == 0 for case in cases)
