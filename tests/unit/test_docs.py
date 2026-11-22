"""The students' documents: the privacy notice carries the sections the design names (section 6.6),
and the student guide shows only rhi commands the CLI accepts, addresses on example.com, every
cause of the shared enumeration, and OpenCode and Pi entries that hold no key (sections 6.1, 6.2
and 8.1)."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

from rhi_cli import main
from rhi_common.causes import Cause
from rhi_common.keys import KEY_PATTERN, generate_key

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def provider_entries(text: str) -> dict[str, tuple[str, str]]:
    """Each of the OpenCode and Pi sections of the guide, and the first JSON block in it."""
    entries = {}
    for harness in ("OpenCode", "Pi"):
        section = text.split(f"\n### {harness}\n", 1)[1].split("\n#", 1)[0]
        entries[harness] = (section, section.split("```json\n", 1)[1].split("```", 1)[0])
    return entries


def _pairs(value: object) -> Iterator[tuple[str, object]]:
    """Every field name and value of a JSON document, at any depth; a list item has no name."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for name, item in items:
        yield str(name), item
        yield from _pairs(item)


def check_provider_entries(text: str) -> None:
    """Each entry parses, sends to a /v1 on example.com, holds no key, and its section names rhi
    token. A key field may only name where the key comes from: an environment variable or a
    command."""
    for harness, (section, block) in provider_entries(text).items():
        entry = json.loads(block)
        pairs = list(_pairs(entry))
        urls = [urlsplit(str(value)) for name, value in pairs if name.lower() == "baseurl"]
        assert urls, harness
        for url in urls:
            host = url.hostname or ""
            assert host == "example.com" or host.endswith(".example.com"), (harness, url)
            assert url.path.rstrip("/").endswith("/v1"), (harness, url)
        assert not re.search(KEY_PATTERN, block), harness
        for name, value in pairs:
            if name.lower() in ("apikey", "key", "token"):
                assert re.fullmatch(r"!.+|\$\{?\w+\}?|\{env:\w+\}", str(value)), (harness, name)
        assert "rhi token" in section, harness


def test_guide_opencode_and_pi_entries_hold_no_key() -> None:
    check_provider_entries(GUIDE.read_text(encoding="utf-8"))


def test_a_pi_entry_holding_a_literal_key_is_refused() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    pi = provider_entries(text)["Pi"][1]
    entry = json.loads(pi)
    for provider in entry["providers"].values():
        provider["apiKey"] = generate_key()
    with pytest.raises(AssertionError):
        check_provider_entries(text.replace(pi, json.dumps(entry, indent=2) + "\n"))
