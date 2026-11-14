import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tools/hygiene").resolve()))
scan = importlib.import_module("scan")


def test_required_look_alikes_are_unreported_and_unchanged():
    scheme = "".join(("https", "://"))
    values = [
        scheme + "docs.aws.amazon.com/AWSEC2/latest/UserGuide/concepts.html",
        scheme + "ec2.eu-north-1.amazonaws.com/",
        "eu-north-1.console.aws.amazon.com/ec2/home",
        scheme + "img.shields.io/github/license/example/repo",
    ]
    for value in values:
        assert scan.scan_text(value) == []
        assert scan.redact(value) == value
