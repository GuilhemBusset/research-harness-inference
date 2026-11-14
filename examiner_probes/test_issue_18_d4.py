import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tools/hygiene").resolve()))
scan = importlib.import_module("scan")

import runpy


def test_required_test_file_assembles_detectable_values_and_scans_clean():
    path = Path("tools/hygiene/tests/test_console_and_badges.py")
    assert scan.scan_paths([path], root=Path.cwd()) == []
    namespace = runpy.run_path(str(path))
    cases = namespace["REPORTED"]
    assert {rule for rule, _, _ in cases} == {"console-link", "build-badge"}
    for rule, value, _ in cases:
        assert rule in {finding.rule for finding in scan.scan_text(value)}
