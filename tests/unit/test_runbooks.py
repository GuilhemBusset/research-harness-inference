"""The runbooks (system design, section 9): each has the sections when, check, act and verify, and
every command of this repository it shows is one the repository has.

Each command of a code block's line (the words between its control operators, such as `|`, `||`,
`&&` and `;`, less its redirections) is told apart by its first word: `rhi` parses with the rhi CLI's parser, the
admin CLI's program name with the admin CLI's parser, and a path of the repository must exist.
Any other first word (aws, gh, git, ...) is a step the walk-through on the deployed system tests.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from rhi_admin import main as admin_main
from rhi_cli import main as rhi_main

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = [
    "session-on-and-off.md",
    "capacity-failure.md",
    "disabled-pool.md",
    "leaked-key.md",
    "bad-deployment.md",
    "secret-rotation.md",
    "park-and-unpark.md",
    "image-and-engine-upgrade.md",
    "course-end.md",
]
SECTIONS = ["when", "check", "act", "verify"]


FENCE = re.compile(r"^(`{3,}|~{3,})")


def read(text: str) -> tuple[list[str], list[list[tuple[str, bool]]]]:
    """The level-2 headings outside the code blocks, and the words of every line of every fenced code
    block (backticks or tildes): a `\\` continues a line, and blank and comment lines are left out."""
    headings: list[str] = []
    lines: list[list[tuple[str, bool]]] = []
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
        words = split(pending)
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


CONTROL = {"|", "|&", "||", "&&", "&", ";", ";;", ";&", ";;&"}
REDIRECTION = {"<", ">", ">>", ">|", "<>", "<<", "<<-", "<<<", ">&", "<&", "&>", "&>>"}
OPERATORS = sorted(CONTROL | REDIRECTION, key=len, reverse=True)  # longest first: `>>` before `>`
NUMBERED = REDIRECTION - {"&>", "&>>"}  # the redirections a descriptor's number may start: `2&>` is `2`, then `&>`


def _quoted(line: str, index: int) -> tuple[str, int]:
    """The text of the quoted string that starts at `line[index]`, and the index after it. Inside
    double quotes a backslash escapes only `$`, a backtick, `"` and itself, as in the shell."""
    quote, end, text = line[index], index + 1, ""
    while end < len(line) and line[end] != quote:
        if quote == '"' and line[end] == "\\" and line[end + 1 : end + 2] in ("$", "`", '"', "\\"):
            end += 1
        text, end = text + line[end], end + 1
    if end >= len(line):
        raise ValueError(f"no closing quotation in: {line}")  # as shlex.split, where the shell refuses the line
    return text, end + 1


def split(line: str) -> list[tuple[str, bool]]:
    """The words of a shell line, less its comment, each with whether it is an operator, read as the
    shell reads them: an unquoted control operator or redirection ends a word with or without a
    blank (`/dev/null;rhi` is a file, `;` and a command), a quoted or escaped one is part of a word,
    a `#` starts a comment only where a word starts (so neither `report#draft` nor `report\\ #draft`
    hides what follows), and the digits of a descriptor (`2>`) belong to their redirection."""
    found: list[tuple[str, bool]] = []
    word: str | None = None  # None between words; "" for a word of empty quotes so far
    digits = False  # whether the word so far is only unquoted, unescaped digits, which a redirection takes
    index = 0
    while index < len(line):
        char = line[index]
        operator = next((op for op in OPERATORS if line.startswith(op, index)), "")
        if char == "\\":
            word, index, digits = (word or "") + line[index + 1 : index + 2], index + 2, False
        elif char in "'\"":
            text, index = _quoted(line, index)
            word, digits = (word or "") + text, False
        elif char == "#" and word is None:
            break
        elif operator or char in " \t":
            if word is not None and not (digits and operator in NUMBERED):
                found.append((word, False))
            word, digits = None, False
            if operator:
                found.append((operator, True))
            index += len(operator) or 1
        else:
            digits = char in "0123456789" and (word is None or digits)  # ASCII only, as the shell reads them
            word, index = (word or "") + char, index + 1
    if word is not None:
        found.append((word, False))
    return found


def commands(words: list[tuple[str, bool]]) -> list[list[str]]:
    """The commands of a line: the words between its control operators, each without its
    redirections and their files, so that each command of a pipeline is checked on its own."""
    found: list[list[str]] = [[]]
    target = False
    for word, operator in words:
        if target:
            target = False
        elif operator and word in CONTROL:
            found.append([])
        elif operator:
            target = True
        else:
            found[-1].append(word)
    return [command for command in found if command]


def problems(text: str, root: Path = ROOT) -> list[str]:
    """What is wrong with a runbook: a missing section, a command that does not parse, a path that is not there."""
    found = []
    headings, lines = read(text)
    if [h for h in headings if h in SECTIONS] != SECTIONS:
        found.append(f"the sections are {headings}, not {SECTIONS} in that order")
    admin = admin_main.parser()
    for words in (command for line in lines for command in commands(line)):
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
    # A redirection is not an argument, and each command of a pipeline or a list is checked.
    redirected = "```sh\nrhi_admin privacy export 1234567 > \"$HOME/export.json\"\nrhi status | cat && rhi frobnicate\n```\n"
    assert problems(sections + redirected) == ["the rhi CLI refuses: rhi frobnicate"]
    # A quoted operator is an argument, which the CLI is given.
    assert problems(sections + "```sh\nrhi status '|'\n```\n") == ["the rhi CLI refuses: rhi status '|'"]
    # A `#` inside a word starts no comment, so what follows it is still given to the CLI.
    hashed = "```sh\nrhi status > report#draft unexpected\nrhi status # a comment\n```\n"
    assert problems(sections + hashed) == ["the rhi CLI refuses: rhi status unexpected"]
    # An operator ends the redirection's file without a blank, and an escaped blank does not end it.
    joined = "```sh\nrhi status > /dev/null;rhi frobnicate\nrhi status 2>report\\ #draft unexpected\n```\n"
    assert problems(sections + joined) == [
        "the rhi CLI refuses: rhi frobnicate",
        "the rhi CLI refuses: rhi status unexpected",
    ]
    # Only unquoted, unescaped digits name a descriptor: an escaped or quoted one is an argument.
    escaped = "```sh\nrhi status \\2>/dev/null\nrhi status '2'>/dev/null\n```\n"
    assert problems(sections + escaped) == ["the rhi CLI refuses: rhi status 2"] * 2
    # `&>` takes no descriptor, and a digit that is not ASCII names none.
    unnumbered = "```sh\nrhi status 2&>/dev/null\nrhi status \u00b2>/dev/null\nrhi status 2>/dev/null\n```\n"
    assert problems(sections + unnumbered) == ["the rhi CLI refuses: rhi status 2", "the rhi CLI refuses: rhi status '\u00b2'"]
    # A line the shell refuses for an unterminated quote is refused here too.
    with pytest.raises(ValueError, match="no closing quotation"):
        problems(sections + '```sh\nrhi "status\n```\n')


def test_section_headings_count_only_outside_code_blocks() -> None:
    sections = "".join(f"## {section.title()}\n\n" for section in SECTIONS)
    assert len(problems("```sh\n" + sections + "rhi status\n```\n")) == 1


def _factory_key_replacement() -> str:
    """The shell block of the secret rotation runbook that installs a new key of the factory's App."""
    text = (ROOT / "docs" / "runbooks" / "secret-rotation.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```sh\n(.*?)```", text, re.DOTALL)
    return next(b for b in blocks if "Downloads/<new-key>" in b and "LOOP_GITHUB_APP_KEY_FILE" in b)


@pytest.mark.parametrize("fault", [None, "copy cut short", "download kept"])
def test_the_factory_key_replacement_keeps_the_old_key_unless_the_copy_completes(
    tmp_path: Path, fault: str | None
) -> None:
    download = tmp_path / "Downloads" / "replacement.private-key.pem"
    download.parent.mkdir()
    download.write_bytes(b"new key\n" * 512)
    key = tmp_path / "factory.pem"
    key.write_bytes(b"old key\n")
    # A file that already has the name of the old file plus .new is someone's, and stays as it is.
    bystander = Path(f"{key}.new")
    bystander.write_bytes(b"not the factory's\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if fault == "download kept":  # an rm that cannot remove anything in the downloads
        rm = bin_dir / "rm"
        rm.write_text('#!/bin/sh\nfor a do case "$a" in */Downloads/*) exit 1;; esac; done\nexec /bin/rm "$@"\n')
        rm.chmod(0o700)
    # A file-size limit of one block cuts the copy short, as a full disk would.
    script = ("ulimit -f 1\n" if fault == "copy cut short" else "") + _factory_key_replacement().replace(
        "<new-key>", "replacement"
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "LOOP_GITHUB_APP_KEY_FILE": str(key)}
    result = subprocess.run(["bash", "-c", script], env=env, cwd=tmp_path, capture_output=True, check=False, text=True)
    assert bystander.read_bytes() == b"not the factory's\n"
    assert sorted(f.name for f in tmp_path.glob("factory.pem*")) == ["factory.pem", "factory.pem.new"]
    if fault == "copy cut short":
        assert key.read_bytes() == b"old key\n"
        assert download.exists()
        assert "not installed" in result.stdout
    else:
        assert key.read_bytes() == b"new key\n" * 512
        assert download.exists() == (fault == "download kept")
        assert "the new key is installed" in result.stdout
        assert "not installed" not in result.stdout


def _retained_inventory() -> str:
    """The shell block of the course-end runbook that lists what the stage's stacks retain."""
    text = (ROOT / "docs" / "runbooks" / "course-end.md").read_text(encoding="utf-8")
    blocks = re.findall(r"^ *```sh\n(.*?)^ *```", text, re.DOTALL | re.MULTILINE)
    return next(b for b in blocks if "get-template" in b and "describe-stack-resource" in b)


# A stand-in for the aws CLI: each stack retains one resource, but Rhi-Gateway retains none, and the
# read named by FAIL (an operation and a stack) fails.
FAKE_AWS = """#!/bin/sh
operation=$2; shift 2
while [ "$#" -gt 0 ]; do
  case "$1" in --stack-name) stack=$2;; --logical-resource-id) id=$2;; esac; shift
done
[ "$FAIL" = "$operation $stack" ] && exit 42
case "$operation" in
  get-template) [ "$stack" = Rhi-Gateway ] && retain=Delete || retain=Retain
    echo '{"Resources": {"Kept": {"DeletionPolicy": "'$retain'"}, "Gone": {}}}';;
  describe-stack-resource) printf '%s\\tAWS::Example\\t%s\\n' "$stack" "$id";;
esac
"""


@pytest.mark.parametrize("fail", [None, "get-template Rhi-Access", "describe-stack-resource Rhi-Pools"])
def test_the_retained_inventory_is_complete_or_fails(tmp_path: Path, fail: str | None) -> None:
    aws = tmp_path / "aws"
    aws.write_text(FAKE_AWS)
    aws.chmod(0o700)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "FAIL": fail or ""}
    result = subprocess.run(["bash", "-c", _retained_inventory()], env=env, capture_output=True, check=False, text=True)
    if fail is None:
        assert result.returncode == 0, result.stderr
        stacks = [line.split("\t")[0] for line in result.stdout.splitlines()[:-1]]
        assert stacks == ["RhiPipeline", "Rhi-Network", "Rhi-Access", "Rhi-Observability", "Rhi-Pools", "Rhi-Guardrails"]
        assert result.stdout.splitlines()[-1] == "The list is complete."
    else:
        assert result.returncode != 0
        assert "complete." not in result.stdout
        assert "INCOMPLETE" in result.stderr
