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
from typing import NamedTuple

import pytest

from rhi_admin import main as admin_main
from rhi_cli import main as rhi_main

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = [
    "sessions.md",
    "capacity-failure.md",
    "suspended-launches.md",
    "scaler-not-running.md",
    "leaked-key.md",
    "bad-deployment.md",
    "secret-rotation.md",
    "park-and-unpark.md",
    "image-and-engine-upgrade.md",
    "course-end.md",
    "agent-session-evaluation.md",
]
SECTIONS = ["when", "check", "act", "verify"]
SHELLS = {"sh", "bash", "shell", "console"}  # a fence's info string, or none, that marks commands


FENCE = re.compile(r"^(`{3,}|~{3,})")


def read(text: str) -> tuple[list[str], list[list[Word]]]:
    """The level-2 headings outside the code blocks, and the words of every line of every fenced code
    block (backticks or tildes) that is shell, then of every substitution's command in it: a `\\`
    continues a line, and blank and comment lines are left out. In a block whose info string names
    another language (`text` for a query, `toml`), only a line that shows a command of this
    repository (`rhi`, the admin CLI, a path, anywhere a command may start) is read as shell: its
    label does not hide it."""
    headings: list[str] = []
    lines: list[list[Word]] = []
    fence, pending, shell = "", "", True
    for line in text.splitlines():
        opening = FENCE.match(line.strip())
        if not fence:
            if opening:
                language = line.strip()[len(opening[1]) :].split(maxsplit=1)
                fence, pending, shell = opening[1], "", not language or language[0] in SHELLS
            elif match := re.match(r"^##\s+(.+)$", line):
                headings.append(match[1].strip().lower())
            continue
        if opening and opening[1][0] == fence[0] and len(opening[1]) >= len(fence) and line.strip() == opening[1]:
            fence = ""
            continue
        if not shell and not pending and not _shows_a_repository_command(line):
            continue
        pending += line.strip().removeprefix("$ ")
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        bodies = [pending]
        while bodies:
            words = split(bodies.pop(0), bodies)
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


CONTROL = {"|", "|&", "||", "&&", "&", ";", ";;", ";&", ";;&", "(", ")"}
REDIRECTION = {"<", ">", ">>", ">|", "<>", "<<", "<<-", "<<<", ">&", "<&", "&>", "&>>"}
OPERATORS = sorted(CONTROL | REDIRECTION, key=len, reverse=True)  # longest first: `>>` before `>`
NUMBERED = REDIRECTION - {"&>", "&>>"}  # the redirections a descriptor's number may start: `2&>` is `2`, then `&>`
SUBSTITUTIONS = ("$(", "<(", ">(")  # a command's output, or a file that stands for it: never a redirection
EXPANDING = set("$*?[{")  # unquoted, what the shell may replace by any number of words


class Word(NamedTuple):
    """A word of a shell line: an operator, or a word the shell may expand (`$x`, `*`, `<(cmd)`), or literal text."""

    text: str
    operator: bool = False
    expanded: bool = False


def _substitution(line: str, index: int, bodies: list[str]) -> int:
    """The index after the substitution whose `(` or backtick is `line[index]`; its body joins `bodies`."""
    close, depth, end = ("`" if line[index] == "`" else ")"), 1, index + 1
    while end < len(line):
        char = line[end]
        if char == "\\":
            end += 2
            continue
        if close == ")" and char in "'\"":
            end = _quoted(line, end, bodies)[1]
            continue
        if close == ")" and (line.startswith(SUBSTITUTIONS, end) or char == "`"):
            end = _substitution(line, end + (char != "`"), bodies)
            continue
        depth += close == ")" and char == "("
        depth -= char == close
        if depth == 0:
            bodies.append(line[index + 1 : end])
            return end + 1
        end += 1
    raise ValueError(f"no end of the substitution in: {line}")  # the shell refuses the line too


def _quoted(line: str, index: int, bodies: list[str]) -> tuple[str, int, bool]:
    """The text of the quoted string that starts at `line[index]`, the index after it, and whether it
    holds an expansion (`$`, a backtick: double quotes only). Inside double quotes a backslash escapes
    only `$`, a backtick, `"` and itself, as in the shell."""
    quote, end, text, expanded = line[index], index + 1, "", False
    while end < len(line) and line[end] != quote:
        start = end
        if quote == '"' and line[end] == "\\" and line[end + 1 : end + 2] in ("$", "`", '"', "\\"):
            end += 1
        elif quote == '"' and (line.startswith("$(", end) or line[end] == "`"):
            end, expanded = _substitution(line, end + (line[end] == "$"), bodies), True
            text += line[start:end]
            continue
        elif quote == '"' and line[end] == "$":
            expanded = True
        text, end = text + line[end], end + 1
    if end >= len(line):
        raise ValueError(f"no closing quotation in: {line}")  # as shlex.split, where the shell refuses the line
    return text, end + 1, expanded


def split(line: str, bodies: list[str] | None = None) -> list[Word]:
    """The words of a shell line, less its comment, read as the shell reads them: an unquoted control
    operator or redirection ends a word with or without a blank (`/dev/null;rhi` is a file, `;` and a
    command), a quoted or escaped one is part of a word, a `#` starts a comment only where a word
    starts (so neither `report#draft` nor `report\\ #draft` hides what follows), the digits of a
    descriptor (`2>`) belong to their redirection, and a substitution (`$(cmd)`, `<(cmd)`, a
    backtick) is part of its word, which is marked expanded, its command joining `bodies`."""
    bodies = [] if bodies is None else bodies
    found: list[Word] = []
    word: str | None = None  # None between words; "" for a word of empty quotes so far
    digits = False  # whether the word so far is only unquoted, unescaped digits, which a redirection takes
    expanded = False
    index = 0
    while index < len(line):
        char = line[index]
        operator = next((op for op in OPERATORS if line.startswith(op, index)), "")
        if char == "\\":
            word, index, digits = (word or "") + line[index + 1 : index + 2], index + 2, False
        elif char in "'\"":
            text, index, quoted_expansion = _quoted(line, index, bodies)
            word, digits, expanded = (word or "") + text, False, expanded or quoted_expansion
        elif char == "#" and word is None:
            break
        elif line.startswith(SUBSTITUTIONS, index) or char == "`":
            end = _substitution(line, index + (char != "`"), bodies)
            word, index, digits, expanded = (word or "") + line[index:end], end, False, True
        elif operator or char in " \t":
            if word is not None and not (digits and operator in NUMBERED):
                found.append(Word(word, expanded=expanded))
            word, digits, expanded = None, False, False
            if operator:
                found.append(Word(operator, operator=True))
            index += len(operator) or 1
        else:
            digits = char in "0123456789" and (word is None or digits)  # ASCII only, as the shell reads them
            lone = char == "$" and line[index + 1 : index + 2] in ("", " ", "\t")  # a `$` before a blank is a `$`
            expanded = expanded or (char in EXPANDING and not lone)
            word, index = (word or "") + char, index + 1
    if word is not None:
        found.append(Word(word, expanded=expanded))
    return found


def commands(words: list[Word]) -> list[list[Word]]:
    """The commands of a line: the words between its control operators, each without its
    redirections and their files, so that each command of a pipeline is checked on its own. As in
    the shell, a subshell's `(` only starts a command, and a word does not follow its `)`."""
    found: list[list[Word]] = [[]]
    target = started = closed = False  # a redirection's file is next; the command has begun; a `)` ended it
    for word in words:
        opening = word.operator and word.text == "("
        if (closed and not word.operator and not target) or (opening and started):
            raise ValueError(f"the shell refuses `{word.text}` in: {shlex.join(w.text for w in words)}")
        if target:
            target = False
        elif word.operator and word.text in CONTROL:
            found.append([])
            closed = started = word.text == ")"
        else:
            target, started = word.operator, True
            if not word.operator:
                found[-1].append(word)
    return [command for command in found if command]


CLI_NAMES = {"rhi", admin_main.parser().prog}


_NAMES = "|".join(re.escape(name) for name in sorted(CLI_NAMES))
# A CLI of this repository named as a word anywhere, or a path of the repository where a word starts
# (after a blank, an operator, an assignment or a substitution's opening): what makes a line of a
# block of another language one the shell reads, whether or not the shell then accepts it.
SHOWS_A_COMMAND = re.compile(
    rf"(?<![\w./-])(?:{_NAMES})(?![\w./-])"
    r"|(?:^|[\s;&|(`=])(?:\./)?[\w.-]+/(?!/)"
)


def _shows_a_repository_command(line: str) -> bool:
    """Whether a line of a block of another language (a query, a setting) shows a command of this
    repository: its label does not hide it, and the line is then checked as any shell line is. The
    raw text is searched, and so are the line's words as the shell reads them (so quotes, `r"hi"` or
    `'scripts/x.sh'`, hide nothing): a CLI of this repository as any word, a path of it as the first
    word of a command, in the line or in a substitution of it."""
    text = line.strip().removeprefix("$ ")
    if SHOWS_A_COMMAND.search(text):
        return True
    bodies = [text]
    try:
        while bodies:
            words = split(bodies.pop(0), bodies)
            if any(not word.operator and word.text in CLI_NAMES for word in words):
                return True
            if any(_is_repository_path(command[0].text) for command in commands(words)):
                return True
    except ValueError:
        pass  # what the shell cannot read shows a command only in its raw text, searched above
    return False



def problems(text: str, root: Path = ROOT) -> list[str]:
    """What is wrong with a runbook: a missing section, a command that does not parse, a path that
    is not there, or a command of this repository whose words the shell decides only when it runs."""
    found = []
    headings, lines = read(text)
    if [h for h in headings if h in SECTIONS] != SECTIONS:
        found.append(f"the sections are {headings}, not {SECTIONS} in that order")
    admin = admin_main.parser()
    clis = {"rhi": ("the rhi CLI", rhi_main.parser), admin.prog: ("the admin CLI", lambda: admin)}
    for words in (command for line in lines for command in commands(line)):
        first, rest = words[0].text, [word.text for word in words[1:]]
        shown = shlex.join(word.text for word in words)
        if first in clis and any(word.expanded for word in words[1:]):
            found.append(f"the arguments are known only when the shell runs: {shown}")
        elif first not in clis and any(word in clis for word in rest):
            found.append(f"a command of this repository the check cannot read: {shown}")
        elif first in clis and _refuses(clis[first][1](), rest):
            found.append(f"{clis[first][0]} refuses: {shown}")
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
    # A tilde fence is a code block too, and a backtick line inside it (here two empty substitutions,
    # which the shell reads) does not close it.
    assert problems(sections + "~~~sh\n````\nrhi frobnicate\n~~~\n") == ["the rhi CLI refuses: rhi frobnicate"]
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
    # A query or a setting in a block of another language is no command; a shell block, named or
    # not, holds commands, and so does a line of another block that starts a command of this repository.
    other = "```text\nsum by (pool) (rate(x[5m]))\nfilter alias = \"A\"\n| stats count(*)\n```\n```toml\nparked = true\n```\n"
    assert problems(sections + other + "```bash\nrhi frobnicate\n```\n") == ["the rhi CLI refuses: rhi frobnicate"]
    labelled = "```text\nrhi status unexpected\nrhi_admin roster list unexpected\nscripts/no-such-script.sh\n```\n"
    assert problems(sections + "```sh\nrhi status\n```\n" + labelled) == [
        "the rhi CLI refuses: rhi status unexpected",
        "the admin CLI refuses: rhi_admin roster list unexpected",
        "no such path in the repository: scripts/no-such-script.sh",
    ]
    # Behind an assignment, inside a substitution, or unterminated, such a line is read as in a shell block.
    hidden = "```text\nX=1 rhi status\nx=$(rhi_admin roster list unexpected)\ncat <(scripts/no-such-script.sh)\n```\n"
    assert problems(sections + hidden) == [
        "a command of this repository the check cannot read: X=1 rhi status",
        "the admin CLI refuses: rhi_admin roster list unexpected",
        "no such path in the repository: scripts/no-such-script.sh",
    ]
    with pytest.raises(ValueError, match="no closing quotation"):
        problems(sections + '```text\nrhi "status\n```\n')
    # Quotes hide no command or path from the shell, so they hide none here.
    quoted = "```text\nr\"hi\" status unexpected\nrhi_\"admin\" roster list unexpected\n'scripts/no-such-script.sh'\n```\n"
    assert problems(sections + quoted) == [
        "the rhi CLI refuses: rhi status unexpected",
        "the admin CLI refuses: rhi_admin roster list unexpected",
        "no such path in the repository: scripts/no-such-script.sh",
    ]


def test_refuses_arguments_the_shell_decides_and_checks_commands_inside_substitutions() -> None:
    sections = "".join(f"## {section.title()}\n\n" for section in SECTIONS)
    # Bash gives each of these a word, or any number of words, that the text does not show.
    for argument in ["<(true)", ">(cat)", "$id", '"$id"', "`id`", "*", "{a,b}"]:
        found = problems(sections + f"```sh\nrhi status {argument}\n```\n")
        assert [p.split(":")[0] for p in found] == ["the arguments are known only when the shell runs"], argument
    # A command that runs inside a substitution or a subshell is checked; one behind a prefix cannot be read.
    nested = "```sh\nx=$(rhi frobnicate)\ncat <(rhi frobnicate) \"`rhi frobnicate`\"\n( rhi frobnicate )\n```\n"
    assert problems(sections + nested) == ["the rhi CLI refuses: rhi frobnicate"] * 4
    assert problems(sections + "```sh\n( ( rhi status ) ) > /dev/null && (rhi status)\n```\n") == []
    # A `(` that starts no command, or a word after a subshell's `)`, is a syntax error of the shell.
    for line in ["rhi status (unexpected)", "rhi_admin roster list (x)", "(rhi status) unexpected", "rhi status > f (x)"]:
        with pytest.raises(ValueError, match="the shell refuses"):
            problems(sections + f"```sh\n{line}\n```\n")
    assert problems(sections + "```sh\nX=1 rhi status\n```\n") == [
        "a command of this repository the check cannot read: X=1 rhi status"
    ]
    # A redirection's file may expand: it is not an argument. A substitution the shell cannot end is refused.
    assert problems(sections + '```sh\nrhi status > "$HOME/status.txt" 2>/dev/null\n```\n') == []
    with pytest.raises(ValueError, match="no end of the substitution"):
        problems(sections + "```sh\nrhi status $(true\n```\n")


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
