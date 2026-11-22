"""The students' documents: the privacy notice carries the sections the design names (section 6.6),
and the student guide shows only rhi commands the CLI accepts, addresses on example.com and every
cause of the shared enumeration (sections 6.2 and 8.1)."""

from __future__ import annotations

import ast
import contextlib
import io
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

from rhi_cli import main
from rhi_common.causes import Cause

ROOT = Path(__file__).resolve().parents[2]
NOTICE = ROOT / "docs" / "privacy-notice.md"
GUIDE = ROOT / "docs" / "student-guide.md"
DESIGN = ROOT / "docs" / "design" / "system-design.md"

HEADINGS = {
    "who is responsible",
    "what is recorded",
    "what is not recorded",
    "where it is kept",
    "for how long",
    "who can see it",
    "whether it counts toward grading",
    "how to ask for a copy or for deletion",
}


def test_privacy_notice_has_its_headings() -> None:
    text = NOTICE.read_text(encoding="utf-8")
    found = {m.strip().lower() for m in re.findall(r"^##\s+(.+)$", text, re.MULTILINE)}
    assert HEADINGS <= found, sorted(HEADINGS - found)


def test_privacy_notice_states_ninety_days() -> None:
    assert "90 days" in NOTICE.read_text(encoding="utf-8")


def guide_rhi_commands(text: str) -> list[list[str]]:
    """The arguments of every rhi command the guide shows: a line of a fenced block, an inline code
    span, or the argument list a code sample passes to a subprocess."""
    commands: list[list[str]] = []
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        spans = [line.strip().removeprefix("$ ")] if fenced else re.findall(r"`([^`]+)`", line)
        commands.extend(shlex.split(span) for span in spans if re.match(r"rhi(\s|$)", span))
    # An argument list may span several lines.
    commands.extend(ast.literal_eval(argv) for argv in re.findall(r"\[\s*[\"']rhi[\"'][^\]]*\]", text))
    return commands


def test_guide_rhi_commands_parse_with_the_cli() -> None:
    commands = guide_rhi_commands(GUIDE.read_text(encoding="utf-8"))
    assert commands
    refused = []
    for argv in commands:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                main.parser().parse_args(argv[1:])
        except SystemExit as exit_:
            if exit_.code not in (0, None):  # argparse exits 0 after printing help
                refused.append(argv)
    assert not refused, refused


def test_guide_addresses_are_on_example_com_but_the_install_command() -> None:
    lines = GUIDE.read_text(encoding="utf-8").splitlines()
    installs = []
    for line in lines:
        # Any scheme, or none: a destination starting with // inherits the page's scheme.
        for url in re.findall(r"(?:[a-z][a-z0-9+.-]*:)?//[^\s\"'`)<>\]]+", line, re.IGNORECASE):
            host = urlsplit(url).hostname or ""
            if host == "github.com":
                installs.append((line, url))
            else:
                assert host == "example.com" or host.endswith(".example.com"), url
    assert len(installs) == 1, installs
    line, url = installs[0]
    assert line.startswith("uv tool install ")
    assert urlsplit(url).path.split("/") == ["", "OWNER", "REPOSITORY"], url
    assert any("instructor" in line and "OWNER" in line and "REPOSITORY" in line for line in lines)


def design_causes() -> set[str]:
    """The causes of the table in section 6.2 of the system design."""
    section = DESIGN.read_text(encoding="utf-8").split("### 6.2 ", 1)[1].split("\n### ", 1)[0]
    return set(re.findall(r"^\s*\| `([a-z_]+)` \|", section, re.MULTILINE))


def test_guide_explains_every_cause() -> None:
    """Every cause the gateway can answer today, and every cause of the design's table."""
    design = design_causes()
    assert "no_session" in design
    causes = design | {cause.value for cause in Cause}
    rows = re.findall(r"^\| `([a-z_]+)` \|\s*\S[^|]*\|\s*\S[^|]*\|$", GUIDE.read_text(encoding="utf-8"), re.MULTILINE)
    assert causes <= set(rows), sorted(causes - set(rows))
