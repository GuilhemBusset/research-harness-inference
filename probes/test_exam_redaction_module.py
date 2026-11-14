import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

NAME = "exam_" + Path(__file__).stem
PACKAGE = ROOT / "tools/implementation-loop/implement_loop"
spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
package = importlib.util.module_from_spec(spec)
sys.modules[NAME] = package
spec.loader.exec_module(package)
L = SimpleNamespace(**{n: __import__(NAME + "." + n, fromlist=[n]) for n in
    ("redact", "github", "cli", "agents", "config", "sandbox", "graph", "model", "state", "workspace", "engine", "supervisor")})
L.config.STANDING_CHECKS = L.config.HISTORY_CHECKS = ()
L.sandbox.use(L.sandbox.PassThrough())
ACCOUNT = "".join(("9274", "6203", "8501"))
ARN = "arn:aws:iam::" + ACCOUNT + ":role/example"
TEXT = "target " + ACCOUNT + " " + ARN

import subprocess

import pytest


def test_scanner_comes_from_engine_and_keeps_owner_values(monkeypatch, tmp_path):
    values = tmp_path / "owner-values"
    values.write_text("private-test-configuration\n")
    monkeypatch.setenv("HYGIENE_LOCAL_VALUES", str(values))
    monkeypatch.chdir(tmp_path)
    text = TEXT + " private-test-configuration"
    scanner = ROOT / "tools/hygiene/scan.py"
    expected = subprocess.run([sys.executable, str(scanner), "--redact"], input=text,
        capture_output=True, text=True, check=True).stdout
    actual = L.redact.redact(text)
    assert actual == expected
    assert ACCOUNT not in actual and ARN not in actual and "private-test-configuration" not in actual
    assert "[redacted " in actual

def test_scanner_failure_never_returns_raw_text(tmp_path):
    scanner = tmp_path / "scanner.py"
    for source in (None, "import sys; print(sys.stdin.read()); sys.exit(2)"):
        if source:
            scanner.write_text(source)
        with pytest.raises(L.redact.RedactionError) as error:
            L.redact.redact(TEXT, scanner=scanner)
        assert ACCOUNT not in str(error.value)
