"""No image is built on a base pulled from Docker Hub, but the two that are published nowhere else
(system design, sections 8.2, step 4, and 9).

Docker Hub limits anonymous pulls by address, and the build hosts of the pipeline's asset step
share theirs: a base pulled from there fails the step on a day of several merges. So every image a
Dockerfile makes the builder pull, the image of a FROM line and the one of a `# syntax=` directive,
comes from another registry, or is in `ALLOWED` with the reason it cannot. And every FROM line
names its image by its sha256 digest, whichever registry serves it.

The Dockerfiles are those git lists, tracked or untracked but not ignored: the pipeline's synth
step runs the tests in a work tree with no commit, where every file is untracked. A file is read
the way BuildKit's parser reads it (`frontend/dockerfile/parser`), as far as FROM lines and the
directive go: what a comment, a continued line, a byte-order mark or a here-document does there,
it does here. A line of a here-document is no instruction, so a FROM written there pulls nothing
and names no stage. Where a here-document ends is read as the parser's lexer reads its word
(`frontend/dockerfile/shell`), but for a word beside a `${...}`, which that lexer expands: an
instruction written so is refused.
"""

from __future__ import annotations

import json
import re
import subprocess
from itertools import dropwhile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The names Docker Hub's registry answers to, on any port. An image reference with no host names
# it too.
DOCKER_HUB = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
# The repositories of Docker Hub a Dockerfile may name, each with the reason it stays there.
ALLOWED = {
    "grafana/grafana-oss": (
        "Grafana publishes its image on Docker Hub and has no official repository on Amazon ECR"
        " Public or on GHCR (checked 2026-11-25)."
    ),
    "vllm/vllm-openai": (
        "vLLM publishes its image on Docker Hub and has no official repository on Amazon ECR"
        " Public or on GHCR (checked 2026-11-25)."
    ),
}

ON_DOCKER_HUB = "an image on Docker Hub, outside the allowed list"
NO_DIGEST = "an image without its sha256 digest"
NOT_READ = "an image written with a variable, a quote or an escape, which hides where it comes from"
NO_END = "a here-document opened beside a ${...}, which hides where it ends"

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
# What an image reference is written with. The builder expands a FROM line's image as a shell
# word, so one written with anything else may name another image than it shows.
PLAIN = re.compile(r"[A-Za-z0-9._:/@-]+")
BYTE_ORDER_MARK = "﻿"
# A parser directive, after its comment sign, and the names BuildKit knows: another name ends the
# directives like any other comment.
DIRECTIVE = re.compile(r"([a-zA-Z][a-zA-Z0-9]*)\s*=\s*(.+?)\s*$", re.ASCII)
DIRECTIVES = {"syntax", "escape", "check"}
# The instructions that may open a here-document, alone or under ONBUILD, and the word that opens
# one: `<<EOF`, `<<-EOF` when tabs may come before the line that ends it, a file descriptor before.
OPENS_A_DOCUMENT = {"RUN", "COPY", "ADD"}
BLANKS = re.compile(r"[\t\v\f\r ]+")
HERE_DOCUMENT = re.compile(r"[0-9]*<<(-?)[\t\n\f\r ]*([^<]*)")
# What the lexer reads in one piece: a quoted text, where `\` escapes only `"`, `$` and itself,
# `<<` with the blanks after it, an escaped character.
PIECE = re.compile(
    r"""(?P<single>'[^']*')|(?P<double>"(?:\\["$\\]|[^"])*+")|(?P<opens><<[\t\r ]*)|(?P<escaped>\\.)|(?P<other>.)""",
    re.DOTALL,
)


def docker_hub_repository(image: str) -> str | None:
    """The repository of Docker Hub an image reference names, as its whole path without tag or
    digest, and None when it names another registry. The reference names a host when its first
    path component, of several, has a `.` or a `:` or is `localhost`; with no host, or one of
    Docker Hub's, it names Docker Hub, where a path of one component is under `library/`."""
    name = image.split("@", 1)[0]
    first, slash, rest = name.partition("/")
    if slash and first.lower().split(":", 1)[0] in DOCKER_HUB:
        name = rest
    elif slash and ("." in first or ":" in first or first == "localhost"):
        return None
    folder, slash, last = name.rpartition("/")
    repository = folder + slash + last.split(":", 1)[0]
    return repository if "/" in repository else f"library/{repository}"


def lines_of(dockerfile: str) -> list[str]:
    return [line.rstrip("\r") for line in dockerfile.removeprefix(BYTE_ORDER_MARK).split("\n")]


def directives(lines: list[str], comment: str = "#") -> dict[str, str]:
    """The parser directives at the top of a file: the lines written `# name=value`, up to the
    first line written otherwise, a blank line and any other comment among them."""
    found: dict[str, str] = {}
    for line in lines:
        directive = DIRECTIVE.match(line.removeprefix(comment).lstrip()) if line.startswith(comment) else None
        if not directive or directive[1].lower() not in DIRECTIVES:
            break
        found.setdefault(directive[1].lower(), directive[2])
    return found


def syntax_image(dockerfile: str) -> str | None:
    """The image a file's `syntax` directive makes the builder pull, and None without one. The
    directive is one at the top of the file only, under a shebang line if there is one, written
    `# syntax=`, or `// syntax=`, or as the `syntax` key of a file that is one JSON object."""
    lines = lines_of(dockerfile)
    lines = lines[1:] if lines[0].startswith("#!") else lines
    for comment in ("#", "//"):
        if (value := directives(lines, comment).get("syntax")) is not None:
            return value.split(" ", 1)[0]
    try:
        document = json.loads("\n".join(lines))
    except ValueError:
        return None
    value = document.get("syntax") if isinstance(document, dict) else None
    return value if isinstance(value, str) else None


def instruction_at(lines: list[str], start: int, continues: re.Pattern[str]) -> tuple[str, int]:
    """The instruction a line starts, as one line, and the place of the line under its last. A
    line that ends with the escape character continues on the next, past the comments and the
    blank lines between them."""
    instruction, at = "", start
    piece = lines[start].lstrip()
    while True:
        continued = continues.search(piece)
        instruction += piece[: continued.start()] if continued else piece
        at += 1
        while continued and at < len(lines) and (not lines[at].strip() or lines[at].lstrip().startswith("#")):
            at += 1
        if not continued or at == len(lines):
            return instruction, at
        piece = lines[at]


def first_word(text: str) -> tuple[str, str]:
    """The first word of a text, and what follows the blanks after it."""
    word, *rest = BLANKS.split(text.strip(), maxsplit=1)
    return word, rest[0].strip() if rest else ""


def shell_words(text: str, raw: bool) -> list[str] | None:
    """The words of a text as BuildKit's lexer splits them, on the blanks that are neither quoted
    nor escaped: with their quotes and escape characters when `raw`, without them otherwise. None
    when a quote has no end. A `$` is read as any other character, which it is to the lexer but
    before a `{`."""
    words, word = [], ""
    for piece in PIECE.finditer(text):
        kind, written = piece.lastgroup, piece[0]
        if kind == "other" and written in ("'", '"'):
            return None
        if kind == "other" and written.isspace():
            words, word = [*words, word], ""
        elif kind == "other" and written == "\\":
            word += written if raw else ""
        elif raw or kind in ("opens", "other"):
            word += written
        elif kind == "double":
            word += re.sub(r'\\(["$\\])', r"\1", written[1:-1])
        else:
            word += written[1:-1] if kind == "single" else written[1:]
    return [word for word in [*words, word] if word]


def here_documents(instruction: str) -> list[tuple[str, bool]] | None:
    """The here-documents an instruction opens, each as the line that ends it and whether tabs may
    come before that line. RUN, COPY and ADD open them, alone or under ONBUILD, and not when their
    arguments are a JSON list. None when the instruction also has a `${`: the lexer expands it, an
    expansion may split a word or fail, and then no here-document is opened."""
    name, arguments = first_word(instruction)
    if name.upper() == "ONBUILD":
        name, arguments = first_word(arguments)
    while arguments.startswith("--"):
        arguments = first_word(arguments)[1]
    if name.upper() not in OPENS_A_DOCUMENT or "<<" not in instruction:
        return []
    try:
        listed = json.loads(arguments) if arguments.startswith("[") else None
    except ValueError:
        listed = None
    if isinstance(listed, list) and all(isinstance(argument, str) for argument in listed):
        return []
    if "${" in instruction:
        return None
    found = []
    for word in shell_words(instruction, raw=True) or []:
        opens = HERE_DOCUMENT.fullmatch(word)
        ends = shell_words(opens[2], raw=False) if opens else None
        if opens and ends and len(ends) == 1:
            found.append((ends[0], opens[1] == "-"))
    return found


def instructions(dockerfile: str) -> list[str]:
    """A Dockerfile's instructions, each as one line. The escape character is `\\` unless the
    `escape` directive names the other. A comment is no instruction and never continues. The lines
    of a here-document are none either: those under the instruction that opens it, up to the line
    that ends it, or to the end of the file."""
    lines = lines_of(dockerfile)
    escape = directives([line.lstrip() for line in lines]).get("escape", "\\")
    escape = re.escape(escape if escape == "`" else "\\")
    continues = re.compile(rf"(?<!{escape}){escape}[ \t]*$")
    found = []
    at = 0
    while at < len(lines):
        if lines[at].lstrip().startswith("#"):
            at += 1
            continue
        instruction, at = instruction_at(lines, at, continues)
        if not instruction.strip():
            continue
        found.append(instruction)
        for ends, after_tabs in here_documents(instruction) or []:
            written = [line.lstrip("\t") if after_tabs else line for line in lines[at:]]
            at += written.index(ends) + 1 if ends in written else len(written)
    return found


def bases(dockerfile: str) -> list[str]:
    """The images a Dockerfile's FROM lines make the builder pull. `FROM scratch` pulls nothing,
    and neither does a FROM that names an earlier stage of the same file."""
    found = []
    stages: set[str] = set()
    for instruction in instructions(dockerfile):
        words = instruction.split()
        if words[0].upper() != "FROM":
            continue
        arguments = list(dropwhile(lambda word: word.startswith("--"), words[1:]))
        if arguments and arguments[0] != "scratch" and arguments[0].lower() not in stages:
            found.append(arguments[0])
        if len(arguments) == 3 and arguments[1].upper() == "AS":
            stages.add(arguments[2].lower())
    return found


def refused_on_docker_hub(image: str) -> bool:
    repository = docker_hub_repository(image)
    return repository is not None and repository not in ALLOWED


def findings(dockerfile: str) -> list[tuple[str, str]]:
    """What a Dockerfile's text names that it may not, as (image, what is wrong with it): the image
    of the syntax directive or of a FROM line on Docker Hub and not allowed, and the image of a
    FROM line without its digest, or written so that its name cannot be read, and an instruction
    whose here-document ends where this cannot read."""
    found = [(instruction, NO_END) for instruction in instructions(dockerfile) if here_documents(instruction) is None]
    syntax = syntax_image(dockerfile)
    if syntax is not None and refused_on_docker_hub(syntax):
        found.append((syntax, ON_DOCKER_HUB))
    for image in bases(dockerfile):
        if not PLAIN.fullmatch(image):
            found.append((image, NOT_READ))
            continue
        if refused_on_docker_hub(image):
            found.append((image, ON_DOCKER_HUB))
        if not DIGEST.search(image):
            found.append((image, NO_DIGEST))
    return found


def listed_dockerfiles() -> list[str]:
    """The Dockerfiles git lists, tracked or untracked but not ignored, as paths from the root."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    names = [name for name in listed.decode().split("\0") if name and (REPO / name).is_file()]
    return sorted(
        name
        for name in names
        if (base := name.rsplit("/", 1)[-1].lower()) == "dockerfile"
        or base.startswith("dockerfile.")
        or base.endswith(".dockerfile")
    )


def test_no_dockerfile_takes_an_image_from_docker_hub_but_the_allowed_ones_and_each_base_has_its_digest() -> None:
    dockerfiles = listed_dockerfiles()
    assert any(name.startswith("services/") for name in dockerfiles), "the Dockerfiles were not found"

    texts = {name: (REPO / name).read_text(encoding="utf-8") for name in dockerfiles}
    assert {name: found for name, text in texts.items() if (found := findings(text))} == {}
    # An allowance is for an image a Dockerfile still takes from Docker Hub.
    assert set(ALLOWED) <= {docker_hub_repository(image) for text in texts.values() for image in bases(text)}


SHA = "@sha256:" + "0" * 64
ECR_PUBLIC = f"public.ecr.aws/docker/library/python:3.13{SHA}"


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        pytest.param(f"FROM python{SHA}\n", [(f"python{SHA}", ON_DOCKER_HUB)], id="python by digest, with no host"),
        pytest.param(
            f"FROM docker.io/library/python{SHA}\n",
            [(f"docker.io/library/python{SHA}", ON_DOCKER_HUB)],
            id="docker.io/library/python by digest",
        ),
        pytest.param(
            "FROM public.ecr.aws/docker/library/python:3.13\n",
            [("public.ecr.aws/docker/library/python:3.13", NO_DIGEST)],
            id="a tag and no digest",
        ),
        pytest.param(
            f"# syntax=docker/dockerfile:1\nFROM {ECR_PUBLIC}\n",
            [("docker/dockerfile:1", ON_DOCKER_HUB)],
            id="a syntax directive naming docker/dockerfile",
        ),
        pytest.param(
            f"\ufeff#!/usr/bin/env -S docker build -f\n# escape=\\\n#Syntax = docker/dockerfile:1 \nFROM {ECR_PUBLIC}\n",
            [("docker/dockerfile:1", ON_DOCKER_HUB)],
            id="a syntax directive under a byte-order mark, a shebang line and another directive",
        ),
        pytest.param(
            "// syntax=docker/dockerfile:1\n", [("docker/dockerfile:1", ON_DOCKER_HUB)], id="a syntax directive in //"
        ),
        pytest.param(
            '{"syntax": "docker/dockerfile:1"}\n', [("docker/dockerfile:1", ON_DOCKER_HUB)], id="a syntax key in JSON"
        ),
        pytest.param(
            f"FROM --platform=linux/arm64 registry-1.docker.io/grafana/grafana:13{SHA} AS base\n",
            [(f"registry-1.docker.io/grafana/grafana:13{SHA}", ON_DOCKER_HUB)],
            id="another name of Docker Hub, and a repository beside an allowed one",
        ),
        pytest.param(
            f"FROM registry-1.docker.io:443/library/python{SHA}\n",
            [(f"registry-1.docker.io:443/library/python{SHA}", ON_DOCKER_HUB)],
            id="Docker Hub by its port",
        ),
        pytest.param(
            f"FROM docker.io/library/grafana/grafana-oss{SHA}\n",
            [(f"docker.io/library/grafana/grafana-oss{SHA}", ON_DOCKER_HUB)],
            id="a longer path that ends like an allowed repository",
        ),
        pytest.param(
            "FROM vllm/vllm-openai:v0.33.0\n", [("vllm/vllm-openai:v0.33.0", NO_DIGEST)], id="an allowed image by tag"
        ),
        pytest.param(
            f"FROM {ECR_PUBLIC} AS build\nFROM builder\n",
            [("builder", ON_DOCKER_HUB), ("builder", NO_DIGEST)],
            id="a name that is no earlier stage",
        ),
        pytest.param(
            "# an old command \\\nFROM python:3.13\n",
            [("python:3.13", ON_DOCKER_HUB), ("python:3.13", NO_DIGEST)],
            id="a FROM under a comment that ends with the escape character",
        ),
        pytest.param(
            f"from \\\n  # the base\n\n  python{SHA} \\\n  as base\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM continued over several lines",
        ),
        pytest.param(
            f"# escape=`\nFROM {ECR_PUBLIC}\nRUN dir C:\\\nFROM python{SHA}\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under a line that ends with a backslash, when the escape character is another",
        ),
        pytest.param(
            "FROM scratch AS files\nCOPY <<EOF /Dockerfile\nFROM scratch AS python\nEOF\nFROM python\n",
            [("python", ON_DOCKER_HUB), ("python", NO_DIGEST)],
            id="a FROM that names a stage only a here-document's FROM line names",
        ),
        pytest.param(
            f"FROM scratch\nRUN <<#EOF\necho \\\n#EOF\nFROM python{SHA}\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under a here-document whose last line ends with the escape character",
        ),
        pytest.param(
            f"FROM scratch\nRUN cat<<EOF\nFROM python{SHA}\nEOF\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under a << in a longer word, which opens no here-document",
        ),
        pytest.param(
            f'FROM scratch\nRUN ["sh", "-c", "cat", "<<EOF"]\nFROM python{SHA}\n',
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under a << in a JSON list, which opens no here-document",
        ),
        pytest.param(
            f"FROM scratch\nRUN echo it's <<EOF\nFROM python{SHA}\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under a << after a quote with no end, which opens no here-document",
        ),
        pytest.param(
            f"FROM scratch\nRUN <<-EOF\n\techo built\n\tEOF\nFROM python{SHA}\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under a here-document that ends after tabs",
        ),
        pytest.param(
            f"FROM scratch\nCOPY <<ONE <<TWO /srv/\nONE\nTWO\nFROM python{SHA}\n",
            [(f"python{SHA}", ON_DOCKER_HUB)],
            id="a FROM under two here-documents",
        ),
        pytest.param(
            "FROM scratch\nRUN <<${END:-EOF}\nFROM scratch\nEOF\n",
            [("RUN <<${END:-EOF}", NO_END)],
            id="a here-document opened beside a ${...}",
        ),
        pytest.param(
            f'ARG BASE=python\nFROM "docker.io"/library/python{SHA}\nFROM ${{BASE}}{SHA}\n',
            [(f'"docker.io"/library/python{SHA}', NOT_READ), (f"${{BASE}}{SHA}", NOT_READ)],
            id="an image written with a quote, and one with a variable",
        ),
    ],
)
def test_an_image_on_docker_hub_or_without_its_digest_is_refused(
    dockerfile: str, expected: list[tuple[str, str]]
) -> None:
    assert findings(dockerfile) == expected


@pytest.mark.parametrize(
    "dockerfile",
    [
        pytest.param(f"# The base.\nFROM {ECR_PUBLIC}\n", id="another registry, by digest"),
        pytest.param(f"FROM grafana/grafana-oss:13.0.2{SHA}\n", id="an allowed image, by digest"),
        pytest.param(
            f"FROM docker.io/grafana/grafana-oss:13.0.2{SHA}\n", id="an allowed image, with Docker Hub's host"
        ),
        pytest.param("FROM scratch\nCOPY . /srv\n", id="scratch"),
        pytest.param(
            f"FROM --platform=$BUILDPLATFORM {ECR_PUBLIC} AS build\nFROM build AS test\nFROM scratch\n",
            id="an earlier stage",
        ),
        pytest.param(
            f"FROM {ECR_PUBLIC} AS build\nRUN <<EOF\necho built\nEOF\nFROM build\n",
            id="a stage named above a here-document",
        ),
        pytest.param(
            "FROM scratch AS source\nCOPY <<EOF /Dockerfile\nFROM python:3.13\nEOF\nFROM source AS packaged\n"
            "FROM packaged\n",
            id="a FROM line in a here-document, and stages named under it",
        ),
        pytest.param(
            "FROM scratch\nONBUILD RUN --network=none sh << 'E O F'\nFROM python:3.13\n\tE O F\nEOF\nE O F\n",
            id="a FROM line in a here-document under ONBUILD, whose end is quoted",
        ),
        pytest.param(
            "FROM scratch\nRUN echo $((1<<2)) && cat <<EOF\nFROM python:3.13\nEOF\n",
            id="a FROM line in a here-document opened beside a $",
        ),
        pytest.param(f"# syntax=ghcr.io/example/dockerfile:1{SHA}\nFROM localhost/base{SHA}\n", id="other hosts"),
        pytest.param("FROM scratch\n# syntax=docker/dockerfile:1\n", id="a comment like the directive, under a FROM"),
        pytest.param(
            "# The image.\n# syntax=docker/dockerfile:1\nFROM scratch\n",
            id="a comment like the directive, under another",
        ),
        pytest.param(
            "\n# syntax=docker/dockerfile:1\nFROM scratch\n", id="a comment like the directive, under a blank"
        ),
    ],
)
def test_what_pulls_nothing_from_docker_hub_is_accepted(dockerfile: str) -> None:
    assert findings(dockerfile) == []
