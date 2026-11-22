"""The students' documents: the privacy notice carries the sections the design names (section 6.6),
and the student guide shows only rhi commands the CLI accepts, addresses on example.com and every
cause of the shared enumeration (sections 6.2 and 8.1)."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

from rhi_cli import main

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


def guide_rhi_commands(text: str) -> list[str]:
    """Every rhi command the guide shows: a line of a fenced block, or an inline code span."""
    commands: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            commands.append(line.strip().removeprefix("$ "))
        else:
            commands.extend(re.findall(r"`([^`]+)`", line))
    return [command for command in commands if re.match(r"rhi(\s|$)", command)]


def test_guide_rhi_commands_parse_with_the_cli() -> None:
    commands = guide_rhi_commands(GUIDE.read_text(encoding="utf-8"))
    assert commands
    refused = []
    for command in commands:
        try:
            main.parser().parse_args(shlex.split(command)[1:])
        except SystemExit:
            refused.append(command)
    assert not refused, refused


def test_guide_addresses_are_on_example_com_but_the_install_command() -> None:
    lines = GUIDE.read_text(encoding="utf-8").splitlines()
    installs = []
    for line in lines:
        for url in re.findall(r"https?://[^\s\"'`)<>\]]+", line):
            host = urlsplit(url).hostname or ""
            if host == "github.com":
                installs.append((line, url))
            else:
                assert host == "example.com" or host.endswith(".example.com"), url
    assert len(installs) == 1, installs
    line, url = installs[0]
    assert line.startswith("uv tool install ") and "/OWNER/REPOSITORY" in url
    assert any("instructor" in line and "OWNER" in line and "REPOSITORY" in line for line in lines)


def design_causes() -> set[str]:
    """The causes of the table in section 6.2 of the system design."""
    section = DESIGN.read_text(encoding="utf-8").split("### 6.2 ", 1)[1].split("\n### ", 1)[0]
    return set(re.findall(r"^\s*\| `([a-z_]+)` \|", section, re.MULTILINE))


def test_guide_explains_every_cause() -> None:
    causes = design_causes()
    assert "no_session" in causes
    rows = re.findall(r"^\| `([a-z_]+)` \|\s*\S[^|]*\|\s*\S[^|]*\|$", GUIDE.read_text(encoding="utf-8"), re.MULTILINE)
    assert causes <= set(rows), sorted(causes - set(rows))
