import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tools/hygiene").resolve()))
scan = importlib.import_module("scan")

import pytest


@pytest.mark.parametrize(("rule", "host", "route"), [
    ("console-link", "eu-north-1.console.aws.amazon.com", "/ec2/home"),
    ("console-link", "example-alias.signin.aws.amazon.com", "/console"),
    ("build-badge", "codebuild.eu-north-1.amazonaws.com", "/badges?uuid=example"),
    ("build-badge", "github.com", "/example/repo/actions/workflows/ci.yml/badge.svg"),
    ("build-badge", "img.shields.io", "/badge/build-passing-brightgreen"),
])
def test_required_links_are_reported_and_masked(rule, host, route):
    value = "".join(("https", "://", host, route))
    text = f"[status]({value})"
    findings = scan.scan_text(text)
    assert [finding.rule for finding in findings] == [rule]
    assert findings[0].excerpt == f"[status]([redacted {rule}])"
    assert scan.redact(text) == f"[status]([redacted {rule}])"
