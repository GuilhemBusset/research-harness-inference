"""Tests of the scanner's console-link and build-badge rules (tools/hygiene/scan.py).

Every URL a rule must catch is assembled at run time, never written out, so this file passes the
scanner itself. The look-alikes are written out: a rule that fired on one would say so here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scan


def j(*parts: str) -> str:
    return "".join(parts)


WEB = j("https", "://")
CONSOLE = j("console", ".aws.amazon.com")
SHIELDS = j(WEB, "img.shields", ".io")
WORKFLOW = "github.com/example/repo/actions/workflows/ci.yml"

REGIONAL_CONSOLE = j(WEB, "eu-north-1.", CONSOLE, "/ec2/home?region=eu-north-1#Instances")
CODEBUILD_BADGE = j(WEB, "codebuild", ".eu-north-1.amazonaws.com", "/badges?uuid=example&branch=main")

REPORTED = [
    ("console-link", REGIONAL_CONSOLE, "a regional console link"),
    ("console-link", j(WEB, "example-alias.", "signin", ".aws.amazon.com/console"), "a sign-in page of an account alias"),
    ("console-link", j(WEB, CONSOLE, "/billing/home"), "the console host alone"),
    ("console-link", j(WEB, "cn-north-1.", "console", ".amazonaws.cn/s3"), "a China console"),
    ("console-link", j(WEB, "example-alias.", "signin", ".amazonaws-us-gov.com/console"), "a GovCloud sign-in page"),
    ("build-badge", CODEBUILD_BADGE, "a CodeBuild badge"),
    ("build-badge", j(WEB, WORKFLOW, "/badge", ".svg?branch=main"), "a GitHub Actions badge"),
    ("build-badge", j(SHIELDS, "/github/actions/workflow/status/example/repo/ci.yml"), "a shields.io workflow badge"),
    ("build-badge", j(SHIELDS, "/badge/build-passing-brightgreen"), "a shields.io build badge"),
]


@pytest.mark.parametrize(("rule", "url"), [c[:2] for c in REPORTED], ids=[c[2] for c in REPORTED])
def test_a_link_is_reported_and_masked(rule: str, url: str) -> None:
    text = f"The status is at {url} for now."
    [finding] = scan.scan_text(text)
    assert finding.rule == rule
    assert finding.excerpt == f"The status is at [redacted {rule}] for now."
    assert scan.redact(text) == f"The status is at [redacted {rule}] for now."


def test_look_alikes_stay_quiet() -> None:
    look_alikes = [
        "https://docs.aws.amazon.com/awsconsolehelpdocs/latest/gsg/what-is.html",   # a page of AWS's documentation
        "https://aws.amazon.com/console/",                                          # a product page
        "https://codebuild.eu-north-1.amazonaws.com and https://ec2.eu-north-1.amazonaws.com/",   # service endpoints
        "open eu-north-1.console.aws.amazon.com/ec2/home or signin.aws.amazon.com",   # host names without a scheme
        "https://console.aws.amazon.com.example.com/",                              # another host that starts like one
        "https://img.shields.io/github/license/example/repo",                       # a shields.io badge of a licence
        "https://img.shields.io/badge/licence-MIT-blue?style=flat",
        f"https://{WORKFLOW}",                                                      # a workflow's page, not its badge
    ]
    for text in look_alikes:
        assert scan.scan_text(text) == [], text


@pytest.mark.parametrize(("rule", "other", "url"), [
    ("console-link", "build-badge", REGIONAL_CONSOLE),
    ("build-badge", "console-link", CODEBUILD_BADGE),
])
def test_an_inline_marker_allows_the_rule_on_its_line_and_redaction_ignores_it(rule: str, other: str, url: str) -> None:
    allowed = f"{url}  # hygiene: allow {rule} (public on purpose)"
    assert scan.scan_text(allowed) == []
    assert scan.redact(allowed).startswith(f"[redacted {rule}]  #")
    assert [f.rule for f in scan.scan_text(f"{url}  # hygiene: allow {other} (another rule)")] == [rule]
    assert [f.rule for f in scan.scan_text(f"# hygiene: allow {rule} (another line)\n{url}")] == [rule]


def test_a_url_is_masked_whole_and_the_text_around_it_is_kept() -> None:
    dashboard = j(WEB, CONSOLE, "/cloudwatch/home#dashboards:name=a~(b~'c)")
    assert scan.redact(f"[the dashboard]({dashboard}), then '{CODEBUILD_BADGE}'.") == (
        "[the dashboard]([redacted console-link]), then '[redacted build-badge]'.")


def test_this_file_holds_no_value_a_rule_catches() -> None:
    assert scan.scan_paths([Path(__file__)], root=Path(scan.__file__).resolve().parents[2]) == []
