import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tools/hygiene").resolve()))
scan = importlib.import_module("scan")


def test_markers_are_line_and_rule_scoped_and_do_not_affect_redaction():
    scheme = "".join(("https", "://"))
    cases = [
        ("console-link", "build-badge", scheme + "console.aws.amazon.com/home"),
        ("build-badge", "console-link", scheme + "codebuild.eu-north-1.amazonaws.com/badges?uuid=example"),
    ]
    for rule, other, value in cases:
        marker = f"# hygiene: allow {rule} (public example for scanner testing)"
        text = f"before {value} after {marker}"
        assert scan.scan_text(text) == []
        assert scan.redact(text) == f"before [redacted {rule}] after {marker}"
        assert [f.rule for f in scan.scan_text(marker + "\n" + value)] == [rule]
        wrong = f"{value} # hygiene: allow {other} (another content rule)"
        assert [f.rule for f in scan.scan_text(wrong)] == [rule]
