"""Independent checks of the student-key hygiene rule using freshly generated keys."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from rhi_common import keys

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "tools/hygiene/scan.py"
sys.path.insert(0, str(SCANNER.parent))
scan = importlib.import_module("scan")
MASK = "[redacted token]"


def test_embedded_keys_and_suffix_boundaries() -> None:
    for _ in range(8):
        key = keys.generate_key()
        assert keys.is_valid_key(key)
        for before in ("", "word", "_", "="):
            for after in ("", " ", ".", "_", "-"):
                text = f"heading\n{before}{key}{after}\nfooter"
                findings = scan.scan_text(text)
                assert [(finding.rule, finding.line) for finding in findings] == [("token", 2)]
                assert key not in findings[0].render()
        for after in ("a", "Z", "0"):
            assert scan.scan_text(f"value={key}{after}") == []


def test_filename_detection_uses_the_real_cli(tmp_path: Path) -> None:
    key = keys.generate_key()
    path = tmp_path / f"capture-{key}.txt"
    path.write_text("ordinary capture", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--paths", str(path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert ":0: token:" in result.stdout
    assert MASK in result.stdout
    assert key not in result.stdout + result.stderr


def test_redaction_masks_multiple_keys_through_api_and_cli() -> None:
    generated = [keys.generate_key() for _ in range(4)]
    text = "\n".join(f"value=prefix{key};" for key in generated)
    expected = "\n".join(f"value=prefix{MASK};" for _ in generated)
    assert scan.redact(text) == expected
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--redact"],
        cwd=ROOT, input=text, capture_output=True, text=True, check=False,
    )
    assert (result.returncode, result.stdout) == (0, expected)


def test_look_alikes_and_test_source_remain_clean() -> None:
    assert scan.STUDENT_KEY == keys.KEY_PATTERN
    packages = sorted({path.name for path in ROOT.glob("**/rhi_*") if path.is_dir()})
    assert "rhi_common" in packages
    text = "\n".join([keys.generate_key()[:-1], keys.PREFIX, *packages])
    assert scan.scan_text(text) == []
    source = ROOT / "tools/hygiene/tests/test_student_key.py"
    assert scan.scan_text(source.read_text(encoding="utf-8")) == []


def test_tree_hygiene_gate() -> None:
    result = subprocess.run(
        ["scripts/check.sh", "hygiene"], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
