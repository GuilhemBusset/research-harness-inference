"""The runbooks (system design, section 9): each has the sections when, check, act and verify, and
every command of this repository it shows is one the repository has.

A code block's line is told apart by its first word: `rhi` parses with the rhi CLI's parser, the
admin CLI's program name with the admin CLI's parser, and a path of the repository must exist.
Any other first word (aws, gh, git, ...) is a step the walk-through on the deployed system tests.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

from rhi_admin import main as admin_main
from rhi_cli import main as rhi_main

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = ["session-on-and-off.md", "capacity-failure.md", "disabled-pool.md"]
SECTIONS = ["when", "check", "act", "verify"]


FENCE = re.compile(r"^(`{3,}|~{3,})")


def read(text: str) -> tuple[list[str], list[list[str]]]:
    """The level-2 headings outside the code blocks, and the words of every line of every fenced code
    block (backticks or tildes): a `\\` continues a line, and blank and comment lines are left out."""
    headings: list[str] = []
    lines: list[list[str]] = []
    fence, pending = "", ""
    for line in text.splitlines():
        opening = FENCE.match(line.strip())
        if not fence:
            if opening:
                fence, pending = opening[1], ""
            elif match := re.match(r"^##\s+(.+)$", line):
                headings.append(match[1].strip().lower())
            continue
        if opening and opening[1][0] == fence[0] and len(opening[1]) >= len(fence) and line.strip() == opening[1]:
            fence = ""
            continue
        pending += line.strip().removeprefix("$ ")
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        words = shlex.split(pending, comments=True)
        if words:
            lines.append(words)
        pending = ""
    return headings, lines


def _refuses(parser: argparse.ArgumentParser, argv: list[str]) -> bool:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(argv)
    except SystemExit as exit_:
        return exit_.code not in (0, None)  # argparse exits 0 after printing help
    return False


def _is_repository_path(word: str) -> bool:
    return "/" in word and not word.startswith(("/", "~", "$", "-")) and "://" not in word


def problems(text: str, root: Path = ROOT) -> list[str]:
    """What is wrong with a runbook: a missing section, a command that does not parse, a path that is not there."""
    found = []
    headings, lines = read(text)
    if [h for h in headings if h in SECTIONS] != SECTIONS:
        found.append(f"the sections are {headings}, not {SECTIONS} in that order")
    admin = admin_main.parser()
    for words in lines:
        first, rest = words[0], words[1:]
        if first == "rhi" and _refuses(rhi_main.parser(), rest):
            found.append(f"the rhi CLI refuses: {shlex.join(words)}")
        elif first == admin.prog and _refuses(admin, rest):
            found.append(f"the admin CLI refuses: {shlex.join(words)}")
        elif _is_repository_path(first) and not (root / first.removeprefix("./")).exists():
            found.append(f"no such path in the repository: {first}")
    return found


@pytest.mark.parametrize("name", RUNBOOKS)
def test_runbook_has_its_sections_and_only_commands_the_repository_has(name: str) -> None:
    text = (ROOT / "docs" / "runbooks" / name).read_text(encoding="utf-8")
    assert read(text)[1], "the runbook shows no command"
    assert problems(text) == []


def test_detects_an_unknown_command() -> None:
    sections = "".join(f"## {section.title()}\n\n" for section in SECTIONS)
    unknown_rhi = sections + "```sh\nrhi frobnicate coursework\n```\n"
    missing_script = sections + "```sh\nscripts/no-such-script.sh --step all\n```\n"
    assert problems(sections + "```sh\nrhi status\nscripts/check.sh fast\n```\n") == []
    assert problems(unknown_rhi) == ["the rhi CLI refuses: rhi frobnicate coursework"]
    assert problems(missing_script) == ["no such path in the repository: scripts/no-such-script.sh"]
    # A tilde fence is a code block too, and a backtick line inside it does not close it.
    assert problems(sections + "~~~sh\n```\nrhi frobnicate\n~~~\n") == ["the rhi CLI refuses: rhi frobnicate"]


def test_section_headings_count_only_outside_code_blocks() -> None:
    sections = "".join(f"## {section.title()}\n\n" for section in SECTIONS)
    assert len(problems("```sh\n" + sections + "rhi status\n```\n")) == 1
