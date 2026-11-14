"""Backlog tooling: lint the issue drafts, show their order, publish them as GitHub issues.

    python3 tools/backlog/backlog.py lint               every draft against the engine's input contract
    python3 tools/backlog/backlog.py plan [--zones]     the order and waves the dependencies give
    python3 tools/backlog/backlog.py publish            dry run: what would be created; nothing is sent
    python3 tools/backlog/backlog.py publish --apply    create it on GitHub; a second run creates nothing

The implement loop reads GitHub issues and never writes them from a design, so the backlog is
drafted as files under backlog/drafts/ (format: backlog/README.md) and published by this tool
(docs/adr/0001-software-factory.md, decision 7). A draft is written in the shape the engine
parses. The lint reads every body with the engine's own parser, imported at run time from
tools/implementation-loop/implement_loop/issue_parser.py, and refuses what the loop could not
finish: an issue without a ledger or without zones, an acceptance item that names neither a
check nor the capability it needs, a dependency on something that does not exist.

Publishing is the one place the factory creates things on GitHub. Without `--apply` it calls
neither `gh` nor the network. With `--apply` it needs an `origin` remote on GitHub, at least one
commit, the drafts committed, a clean lint and a clean hygiene scan of every title and body, and
it refuses a repository to which nothing has been pushed yet: a remote that exists is not the
owner's green light. It finds what it already published through a `<!-- backlog-id: ... -->`
line in each body, and it never edits or deletes an issue: after publication the issue on
GitHub is the truth.

Exit status: 0 fine; 1 the drafts have problems (one per line, `path:line: message`); 2 the tool
could not do its job (no engine parser, no hygiene scanner, no remote, a failed `gh` call).
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import itertools
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

DRAFTS = "backlog/drafts"
ENGINE = "tools/implementation-loop"
PARSER = f"{ENGINE}/implement_loop/issue_parser.py"
LOOP_TOML = f"{ENGINE}/loop.toml"
SCANNER = "tools/hygiene/scan.py"

# The REST API version the engine reads with (implement_loop/config.py, `api_version`), so the
# publisher writes what the engine will read back. Endpoints: backlog/README.md, "Publishing".
API_VERSION = "2022-11-28"
GITHUB_HOST = "github.com"   # where `origin` must be, and the only host a call goes to, whatever GH_HOST says
PER_PAGE = 100
MAX_TITLE = 256      # GitHub's limit on an issue title
MAX_BODY = 65536     # GitHub's limit on an issue body

# Every label a draft may carry: name -> (colour, description). The engine reads `size:*` for
# its call ceiling and `loop:infra` to raise it; it creates its other `loop:*` labels itself.
LABELS: dict[str, tuple[str, str]] = {
    "size:S": ("C2E0C6", "backlog: a small issue"),
    "size:M": ("FEF2C0", "backlog: a medium issue"),
    "size:L": ("F9D0C4", "backlog: a large issue; split it if it can be split"),
    "kind:infra": ("1D76DB", "backlog: AWS infrastructure (CDK)"),
    "kind:app": ("5319E7", "backlog: code that runs on the infrastructure"),
    "kind:test": ("0E8A16", "backlog: tests and invariants"),
    "kind:docs": ("0075CA", "backlog: documentation"),
    "kind:factory": ("6F42C1", "backlog: the software factory itself"),
    "loop:infra": ("EDEDED", "implement loop: factory tooling, with the larger call ceiling"),
}
NOT_ON_EPICS = ("size:", "loop:")

EPIC_ID = re.compile(r"^E(?:0|[1-9][0-9]{0,2})$")
ISSUE_FILE = re.compile(r"^([0-9]{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
EPIC_KEYS = frozenset({"id", "kind", "title", "labels"})
ISSUE_KEYS = frozenset({"id", "kind", "title", "labels", "epic", "blocked_by"})

MARKER = re.compile(r"^\s*<!--\s*backlog-id:\s*(E(?:0|[1-9][0-9]{0,2})(?:-[0-9]{2})?)\s*-->\s*$", re.MULTILINE)
MARKER_ANY = re.compile(r"<!--\s*backlog-id\b", re.IGNORECASE)

# How the engine reads a body (implement_loop/issue_parser.py). The lint needs line numbers, which
# the engine's functions do not give, so these patterns are repeated here; `read_body` compares
# its reading with the engine's on every draft and stops if they ever differ.
_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_CHECKBOX = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*\S)\s*$")
_EVIDENCE_SUFFIX = re.compile(r"\s+\(\[evidence\]\([^)]*\)\)\s*$")
_BRANCH = re.compile(r"Suggested branch:\s*`([^`]+)`")
_TOKEN = re.compile(r"`([^`]+)`")
_PATHLIKE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
_DOT_DIRS = (".github/", ".claude/", ".agents/", ".codex/", ".githooks/", ".gitignore")
_FILE_EXT = re.compile(r"[A-Za-z_\-][A-Za-z0-9_\-]*\.[A-Za-z0-9]{1,5}$")
_LEDGER_SECTIONS = {"deliverables": ("deliverable", "D", "Deliverables"),
                    "acceptance criteria": ("acceptance", "A", "Acceptance criteria")}

# The examiner blocks on an item only by quoting at least three of its words (implement_loop/gate.py).
MIN_ITEM_WORDS = 3

# What makes an acceptance item checkable: a backticked command whose first word is one of these
# programs (with arguments) or a shell script of this repository, or a backticked test path.
COMMAND_HEADS = frozenset({
    "uv", "uvx", "python", "python3", "pytest", "ruff", "ty", "cdk", "npx", "npm", "node",
    "bash", "sh", "git", "grep", "rg", "diff", "cmp", "test", "jq",
})
_SCRIPT = re.compile(r"^(?:\./)?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.sh$")
_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
# Commands only an AWS account can answer. Agents hold no credentials (ADR 0001), so an item that
# rests on one is the owner's and says so with `(needs <capability>)`.
ACCOUNT_CDK = frozenset({"deploy", "bootstrap", "destroy", "diff", "watch", "import", "rollback", "gc", "drift"})

_NEEDS = re.compile(r"\(needs ([^()\s]+)\)")
_NEEDS_LOOSE = re.compile(r"\(\s*needs?\b[^)]*\)", re.IGNORECASE)
_NEEDS_END = re.compile(r"\(needs [^()\s]+\)\.?\s*$")

# A backticked token without a slash is a zone for the engine as soon as it ends like a file
# name, which `g5.xlarge` or `ec2.Vpc` also do. Only these endings are taken as files.
KNOWN_EXTENSIONS = frozenset({
    "py", "pyi", "toml", "json", "jsonl", "yaml", "yml", "md", "txt", "sh", "cfg", "ini", "lock", "conf",
    "js", "mjs", "cjs", "ts", "html", "css", "sql", "j2", "xml", "csv",
})
_MIME = re.compile(r"^(?:application|text|image|audio|video|multipart)/[A-Za-z0-9.\-]+$")
_RELATIVE_LINK = re.compile(r"\]\((?![a-z][a-z0-9+.\-]*:|#)([^)\s]+)\)", re.IGNORECASE)
_BRANCH_NAME = re.compile(r"^[a-z0-9][a-z0-9._\-]*(?:/[a-z0-9][a-z0-9._\-]*)+$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9\-]*)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$")


class BacklogError(Exception):
    """The tool cannot do its job: exit 2."""


class GhError(BacklogError):
    """A `gh api` call failed."""


@dataclass(frozen=True, order=True)
class Problem:
    """One thing wrong with a draft. `line` is 0 when the problem is about the whole file."""

    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}" if self.line else f"{self.path}: {self.message}"


@dataclass
class Item:
    """One ledger item, numbered as the engine numbers it (D1.., A1..)."""

    id: str
    section: str     # "deliverable" | "acceptance"
    line: int        # line of the body
    text: str
    checked: bool


@dataclass
class Reading:
    """A body as the engine reads it, with the line of everything."""

    sections: dict[str, list[tuple[int, str]]]
    headings: dict[str, list[int]]
    items: list[Item]
    zones: list[tuple[int, str]]          # backticked paths of Deliverables
    references: list[tuple[int, str]]     # local paths of References
    branch: str | None


@dataclass
class Draft:
    rel: str                         # path from the repository root
    kind: str                        # "epic" | "issue"
    id: str                          # given by the path: E1, or E1-03
    title: str
    labels: list[str]
    epic: str | int | None           # an issue's epic: a draft id, or the number of a published epic
    blocked_by: list[str | int]      # draft ids, or numbers of published issues
    body: str
    body_line: int                   # line of the file where the body starts
    reading: Reading | None = None

    @property
    def folder(self) -> str:
        return self.id.split("-")[0]

    def at(self, body_line: int) -> int:
        return self.body_line + body_line - 1 if body_line else 0


@dataclass
class Backlog:
    drafts: list[Draft]
    problems: list[Problem]
    lacks: list[str] = field(default_factory=list)

    @property
    def epics(self) -> list[Draft]:
        return sorted((d for d in self.drafts if d.kind == "epic"), key=lambda d: id_key(d.id))

    @property
    def issues(self) -> list[Draft]:
        return sorted((d for d in self.drafts if d.kind == "issue"), key=lambda d: id_key(d.id))


def id_key(draft_id: str) -> tuple[int, int]:
    """Sorts E2 before E10, and an epic before its issues."""
    epic, _, nn = draft_id.partition("-")
    return int(epic[1:]), int(nn) if nn else -1


# --------------------------------------------------------------------------------------------
# What the lint depends on: the engine's parser, the host's lacks, the hygiene scanner
# --------------------------------------------------------------------------------------------

def engine_module(root: Path, name: str) -> ModuleType:
    """A module of this repository's engine, imported from its path under a private package name,
    so nothing else on `sys.path` can stand in for it."""
    package_dir = root / ENGINE / "implement_loop"
    source = package_dir / f"{name}.py"
    if not source.is_file() or not (package_dir / "__init__.py").is_file():
        raise BacklogError(f"the engine's {name}.py is missing ({ENGINE}/implement_loop/{name}.py): the backlog "
                           "is read with the engine's own code and cannot be linted or published without it")
    alias = "_backlog_engine_" + hashlib.sha256(str(package_dir.resolve()).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(alias, package_dir / "__init__.py",
                                                  submodule_search_locations=[str(package_dir)])
    try:
        if alias not in sys.modules and spec is not None and spec.loader is not None:
            package = importlib.util.module_from_spec(spec)
            sys.modules[alias] = package
            spec.loader.exec_module(package)
        return importlib.import_module(f"{alias}.{name}")
    except Exception as exc:   # whatever the engine raises on import is one clear failure here
        sys.modules.pop(alias, None)
        raise BacklogError(f"the engine's {name}.py could not be imported from {ENGINE}/implement_loop/: "
                           f"{type(exc).__name__}: {exc}") from exc


def engine_parser(root: Path) -> ModuleType:
    parser = engine_module(root, "issue_parser")
    for name in ("sections", "ledger"):
        if not callable(getattr(parser, name, None)):
            raise BacklogError(f"{PARSER} has no {name}(): the lint reads issue bodies with it")
    return parser


def host_lacks(root: Path) -> list[str]:
    """The capabilities `loop.toml` says the host lacks: the only ones an item may name."""
    path = root / LOOP_TOML
    if not path.is_file():
        raise BacklogError(f"{LOOP_TOML} is missing: it names the capabilities an item may need")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise BacklogError(f"{LOOP_TOML} is not valid TOML: {exc}") from exc
    lacks = (data.get("host") or {}).get("lacks") or []
    if not isinstance(lacks, list) or not all(isinstance(c, str) for c in lacks):
        raise BacklogError(f"{LOOP_TOML}: [host] lacks must be a list of strings")
    return [c.strip() for c in lacks if c.strip()]


def scan(root: Path, paths: Sequence[str | Path]) -> list[str]:
    """The hygiene scanner's findings on these files, one per line; [] when they are clean. A
    scanner that is missing or cannot run is an error, never a pass."""
    scanner = root / SCANNER
    if not scanner.is_file():
        raise BacklogError(f"the hygiene scanner is missing ({SCANNER}): nothing is linted or published "
                           "without the scan that keeps account identifiers and secrets out")
    if not paths:
        return []
    proc = subprocess.run([sys.executable, str(scanner), "--paths", *map(str, paths)], cwd=root,
                          capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return []
    out = (proc.stdout.strip() or proc.stderr.strip())
    if proc.returncode == 1:
        return out.splitlines() or ["the hygiene scanner reported findings and printed none"]
    raise BacklogError(f"the hygiene scanner could not run (exit {proc.returncode}): "
                       f"{(proc.stderr.strip() or proc.stdout.strip())[-800:]}")


# --------------------------------------------------------------------------------------------
# Reading the drafts
# --------------------------------------------------------------------------------------------

def load_drafts(root: Path) -> tuple[list[Draft], list[Problem]]:
    base = root / DRAFTS
    if not base.is_dir():
        raise BacklogError(f"{DRAFTS}/ does not exist")
    drafts: list[Draft] = []
    problems: list[Problem] = []
    for folder in sorted(base.iterdir()):
        rel = folder.relative_to(root).as_posix()
        if folder.name == ".gitkeep" and folder.is_file():
            continue
        if not folder.is_dir() or not EPIC_ID.match(folder.name):
            problems.append(Problem(rel, 0, f"{DRAFTS}/ holds one folder per epic, named E<number> (E0, E1, ... "
                                            "E999, no leading zero), and nothing else"))
            continue
        for path in sorted(folder.iterdir()):
            rel = path.relative_to(root).as_posix()
            match = ISSUE_FILE.match(path.name)
            if not path.is_file() or (path.name != "epic.md" and not match):
                problems.append(Problem(rel, 0, "an epic folder holds epic.md and issue files named <nn>-<slug>.md "
                                                "(two digits, then lower-case words joined by hyphens)"))
                continue
            kind, draft_id = ("issue", f"{folder.name}-{match.group(1)}") if match else ("epic", folder.name)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append(Problem(rel, 0, f"cannot be read as UTF-8 text: {exc}"))
                continue
            draft, found = parse_draft(text, rel, kind, draft_id)
            problems += found
            if draft is not None:
                drafts.append(draft)
    return drafts, problems


def parse_draft(text: str, rel: str, kind: str, draft_id: str) -> tuple[Draft | None, list[Problem]]:
    """A draft from its file: TOML front matter between two `+++` lines, then the body."""
    problems: list[Problem] = []

    def bad(message: str) -> None:
        problems.append(Problem(rel, 1, message))

    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        bad("the file must start with a `+++` line that opens its TOML front matter")
        return None, problems
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "+++"), None)
    if end is None:
        bad("the front matter is not closed by a second `+++` line")
        return None, problems
    try:
        meta = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        bad(f"the front matter is not valid TOML: {exc} (count lines from the one after `+++`)")
        return None, problems

    allowed = EPIC_KEYS if kind == "epic" else ISSUE_KEYS
    for key in sorted(set(meta) - allowed):
        bad(f"unknown front matter key `{key}`: {'an epic' if kind == 'epic' else 'an issue'} takes "
            f"{', '.join(sorted(allowed))}")
    if kind == "epic" and meta.get("kind") != "epic":
        bad('epic.md must say `kind = "epic"`')
    if kind == "issue" and meta.get("kind", "issue") != "issue":
        bad('only epic.md has a `kind`; remove it (or write `kind = "issue"`)')
    if meta.get("id") != draft_id:
        bad(f'`id` must be "{draft_id}", the id this path gives the draft')

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        bad("`title` must be a non-empty string")
        title = ""
    elif title != title.strip() or "\n" in title or len(title) > MAX_TITLE:
        bad(f"`title` must be one line of at most {MAX_TITLE} characters, without spaces around it")

    labels = meta.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        bad("`labels` must be a list of strings")
        labels = []

    epic = meta.get("epic")
    blocked_by = meta.get("blocked_by", [])
    if kind == "issue":
        if not _is_ref(epic):
            bad(f'`epic` must be "{draft_id.split("-")[0]}", or the number of an epic already on GitHub (an integer)')
            epic = None
        if not isinstance(blocked_by, list) or not all(_is_ref(ref) for ref in blocked_by):
            bad("`blocked_by` must be a list of draft ids (strings) and numbers of published issues (integers)")
            blocked_by = []
        elif len({str(ref) for ref in blocked_by}) != len(blocked_by):
            bad("`blocked_by` names the same issue twice")
    else:
        epic, blocked_by = None, []

    body_lines = lines[end + 1:]
    skip = next((i for i, line in enumerate(body_lines) if line.strip()), len(body_lines))
    body = "\n".join(body_lines[skip:]).rstrip()
    return Draft(rel=rel, kind=kind, id=draft_id, title=title, labels=labels, epic=epic, blocked_by=blocked_by,
                 body=body + "\n" if body else "", body_line=end + 2 + skip), problems


def _is_ref(value: object) -> bool:
    """A draft id, or the number of an issue on GitHub."""
    if isinstance(value, bool):
        return False
    return (isinstance(value, str) and bool(value)) or (isinstance(value, int) and value > 0)


def is_path(token: str) -> bool:
    """Whether the engine takes a backticked token for a path (issue_parser._is_path)."""
    if not _PATHLIKE.match(token) or token.startswith(("-", "/")) or token.endswith("."):
        return False
    if token.startswith(".") and not token.startswith(_DOT_DIRS):
        return False
    return "/" in token or bool(_FILE_EXT.search(token))


def read_body(body: str, parser: ModuleType) -> Reading:
    """The body as the engine will read it. Sections and ledger are checked against the engine's
    `sections()` and `ledger()`; zones and references against its `declared_paths()` and
    `reference_paths()` when it has them. A difference means this tool is out of date with the
    engine, which is an error here and not a lint finding."""
    sections: dict[str, list[tuple[int, str]]] = {}
    headings: dict[str, list[int]] = {}
    current: str | None = None
    for n, line in enumerate(body.splitlines(), 1):
        match = _HEADING.match(line)
        if match:
            current = match.group(1).strip().lower()
            sections.setdefault(current, [])
            headings.setdefault(current, []).append(n)
        elif line.lstrip().startswith("<details"):
            current = None
        elif current is not None:
            sections[current].append((n, line))

    items: list[Item] = []
    for heading, lines in sections.items():
        if heading not in _LEDGER_SECTIONS:
            continue
        section, prefix, _ = _LEDGER_SECTIONS[heading]
        count = 0
        for n, line in lines:
            match = _CHECKBOX.match(line)
            if match:
                count += 1
                items.append(Item(id=f"{prefix}{count}", section=section, line=n,
                                  text=_EVIDENCE_SUFFIX.sub("", match.group(2)), checked=match.group(1) != " "))

    def paths_of(heading: str, skip_links: bool) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        for n, line in sections.get(heading, []):
            if skip_links and "](" in line:
                continue
            for token in _TOKEN.findall(line):
                if is_path(token) and all(token != seen for _, seen in found):
                    found.append((n, token))
        return found

    branch = _BRANCH.search(body)
    reading = Reading(sections=sections, headings=headings, items=items, zones=paths_of("deliverables", False),
                      references=paths_of("references", True), branch=branch.group(1).strip() if branch else None)

    theirs = [(i.id, i.section, i.text, bool(i.checked)) for i in parser.ledger(body)]
    same = (parser.sections(body) == {k: [line for _, line in v] for k, v in sections.items()}
            and theirs == [(i.id, i.section, i.text, i.checked) for i in items])
    for name, mine in (("declared_paths", reading.zones), ("reference_paths", reading.references)):
        engine_reads = getattr(parser, name, None)
        if same and callable(engine_reads):
            same = list(engine_reads(body)) == [path for _, path in mine]
    if not same:
        raise BacklogError(f"this tool and the engine ({PARSER}) no longer read an issue body the same way: "
                           "tools/backlog/backlog.py must follow the engine's parser before any draft is trusted")
    return reading


# --------------------------------------------------------------------------------------------
# The lint
# --------------------------------------------------------------------------------------------

def lint(root: Path) -> Backlog:
    """Every draft, with every problem found. Raises `BacklogError` when the lint itself cannot run."""
    parser = engine_parser(root)
    lacks = host_lacks(root)
    scan(root, [])     # a missing scanner fails the lint even when there is nothing to scan
    drafts, problems = load_drafts(root)
    for draft in drafts:
        draft.reading = read_body(draft.body, parser)
    problems += check_graph(drafts)
    for draft in drafts:
        problems += check_labels(draft)
        problems += check_epic(draft) if draft.kind == "epic" else check_issue(draft, lacks)
    problems += check_references(drafts, root)
    problems += [Problem("hygiene", 0, finding) for finding in scan(root, [d.rel for d in drafts])]
    return Backlog(drafts=drafts, problems=sorted(set(problems)), lacks=lacks)


def check_graph(drafts: list[Draft]) -> list[Problem]:
    """Ids, titles, branches, epics and `blocked_by` edges across the drafts."""
    problems: list[Problem] = []
    by_id: dict[str, Draft] = {}
    titles: dict[str, Draft] = {}
    branches: dict[str, Draft] = {}
    for draft in drafts:
        if draft.id in by_id:
            problems.append(Problem(draft.rel, 1, f"{by_id[draft.id].rel} has the same number: both would be "
                                                  f"{draft.id}"))
            continue
        by_id[draft.id] = draft
        if draft.title and draft.title in titles:
            problems.append(Problem(draft.rel, 1, f"same title as {titles[draft.title].rel}"))
        titles.setdefault(draft.title, draft)
        branch = draft.reading.branch if draft.reading else None
        if branch and branch in branches:
            problems.append(Problem(draft.rel, 0, f"suggests the branch `{branch}`, as {branches[branch].rel} does"))
        elif branch:
            branches[branch] = draft

    children: dict[str, int] = {d.id: 0 for d in by_id.values() if d.kind == "epic"}
    deps: dict[str, list[str]] = {}
    for draft in by_id.values():
        if draft.kind != "issue":
            continue
        folder = draft.folder
        if isinstance(draft.epic, str):
            if draft.epic != folder:
                problems.append(Problem(draft.rel, 1, f'`epic` must be "{folder}", the folder this draft is in, or '
                                                      "the number of an epic already on GitHub"))
            elif folder not in children:
                problems.append(Problem(draft.rel, 1, f"epic {folder} has no draft: add {DRAFTS}/{folder}/epic.md, "
                                                      "or give the number of the published epic (`epic = 12`)"))
            else:
                children[folder] += 1
        elif isinstance(draft.epic, int) and folder in children:
            problems.append(Problem(draft.rel, 1, f"`epic` is an issue number, but this folder has an epic.md: "
                                                  f'write `epic = "{folder}"`'))
        deps[draft.id] = []
        for ref in draft.blocked_by:
            if isinstance(ref, int):
                continue
            if ref == draft.id:
                problems.append(Problem(draft.rel, 1, "`blocked_by` names the draft itself"))
            elif ref not in by_id:
                problems.append(Problem(draft.rel, 1, f"`blocked_by` names {ref}, which is no draft (an issue "
                                                      "already on GitHub is named by its number, as an integer)"))
            elif by_id[ref].kind == "epic":
                problems.append(Problem(draft.rel, 1, f"`blocked_by` names the epic {ref}: depend on the issues "
                                                      "that deliver what this one needs, the engine orders only those"))
            else:
                deps[draft.id].append(ref)
    for epic, count in children.items():
        if not count:
            problems.append(Problem(by_id[epic].rel, 1, "an epic needs at least one issue draft in its folder: the "
                                                        "engine takes an issue without sub-issues for a work item"))
    cycle = find_cycle(deps)
    if cycle:
        problems.append(Problem(by_id[cycle[0]].rel, 1, "dependency cycle: " + " -> ".join(cycle)))
    return problems


def find_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    """One cycle of the `blocked_by` graph, as the ids along it, or None."""
    done: set[str] = set()

    def visit(node: str, trail: list[str]) -> list[str] | None:
        if node in trail:
            return [*trail[trail.index(node):], node]
        if node in done:
            return None
        for dep in deps.get(node, []):
            found = visit(dep, [*trail, node])
            if found:
                return found
        done.add(node)
        return None

    for start in sorted(deps, key=id_key):
        found = visit(start, [])
        if found:
            return found
    return None


def check_labels(draft: Draft) -> list[Problem]:
    problems: list[Problem] = []

    def bad(message: str) -> None:
        problems.append(Problem(draft.rel, 1, message))

    for label in draft.labels:
        if label not in LABELS:
            bad(f"unknown label `{label}`: a draft takes {', '.join(LABELS)}")
    if len(set(draft.labels)) != len(draft.labels):
        bad("`labels` names the same label twice")
    sizes = [label for label in draft.labels if label.startswith("size:")]
    kinds = [label for label in draft.labels if label.startswith("kind:")]
    if draft.kind == "issue":
        if len(sizes) != 1:
            bad(f"an issue has exactly one size label (size:S, size:M or size:L); this one has {len(sizes)}")
        if len(kinds) != 1:
            bad(f"an issue has exactly one kind label ({', '.join(k for k in LABELS if k.startswith('kind:'))}); "
                f"this one has {len(kinds)}")
    else:
        if any(label.startswith(NOT_ON_EPICS) for label in draft.labels):
            bad("an epic is a tracking issue: it takes no size label and no loop label, only an optional kind")
        if len(kinds) > 1:
            bad("an epic has at most one kind label")
    return problems


def check_epic(draft: Draft) -> list[Problem]:
    problems = _check_text(draft)
    reading = draft.reading
    if reading and (reading.items or any(h in reading.headings for h in _LEDGER_SECTIONS)):
        problems.append(Problem(draft.rel, draft.body_line, "an epic is a tracking issue: the engine never reads "
                                "its checkboxes; Deliverables and Acceptance criteria belong in its issues"))
    return problems


def _check_text(draft: Draft) -> list[Problem]:
    """What holds for every body, an epic's or an issue's."""
    problems: list[Problem] = []
    if not draft.body.strip():
        return [Problem(draft.rel, 0, "the body is empty")]
    if len(render_body(draft)) > MAX_BODY:
        problems.append(Problem(draft.rel, 0, f"the body is longer than GitHub's {MAX_BODY} characters"))
    fenced = False
    for n, line in enumerate(draft.body.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if MARKER_ANY.search(line):
            problems.append(Problem(draft.rel, draft.at(n), "the `backlog-id` marker is added by the publisher: "
                                                            "remove it from the draft"))
        if line.lstrip().startswith("<details"):
            problems.append(Problem(draft.rel, draft.at(n), "no `<details>` block in a draft: the engine stops "
                                                            "reading a section at it, and would lose what follows"))
        if fenced:
            continue
        for target in _RELATIVE_LINK.findall(_TOKEN.sub("", line)):
            problems.append(Problem(draft.rel, draft.at(n), f"the link to `{target}` is relative and leads nowhere "
                                                            "from an issue: name the path in backticks, or give a "
                                                            "full URL"))
    return problems


def check_issue(draft: Draft, lacks: list[str]) -> list[Problem]:
    """One issue body against the engine's input contract and the rule of ADR 0001, decision 7."""
    problems = _check_text(draft)
    reading = draft.reading
    if reading is None or not draft.body.strip():
        return problems

    def bad(body_line: int, message: str) -> None:
        problems.append(Problem(draft.rel, draft.at(body_line) or draft.body_line, message))

    for heading, (section, _, shown) in _LEDGER_SECTIONS.items():
        lines = reading.headings.get(heading, [])
        if not lines:
            bad(0, f"no `## {shown}` section")
            continue
        if len(lines) > 1:
            bad(lines[1], f"`## {shown}` appears {len(lines)} times: the engine would number the items as one list")
        if not any(item.section == section for item in reading.items):
            bad(lines[0], f"`## {shown}` has no `- [ ] ` item: the engine would have nothing to prove there")
        for n, line in reading.sections.get(heading, []):
            if line.strip() and not _CHECKBOX.match(line):
                bad(n, f"under `## {shown}` every line is one `- [ ] ` item: the engine reads nothing else there, "
                       "so this line would never be proven (context belongs above, in its own section)")

    seen: dict[str, str] = {}
    for item in reading.items:
        if item.checked:
            bad(item.line, f"{item.id} is already ticked: the engine ticks an item when it has the evidence")
        raw = next(line for n, line in reading.sections[_heading_of(item)] if n == item.line)
        if _EVIDENCE_SUFFIX.search(raw):
            bad(item.line, f"{item.id} carries an evidence link: the engine adds those")
        if len(item.text.split()) < MIN_ITEM_WORDS:
            bad(item.line, f"{item.id} is shorter than {MIN_ITEM_WORDS} words: the examiner can only hold the work "
                           "to an item by quoting it")
        if item.text in seen:
            bad(item.line, f"{item.id} repeats {seen[item.text]}")
        seen.setdefault(item.text, item.id)
        for message in check_proof(item, lacks):
            bad(item.line, f"{item.id} {message}")

    if reading.headings.get("deliverables") and not reading.zones:
        bad(reading.headings["deliverables"][0], "Deliverables name no path in backticks: the engine takes those "
            "paths as the issue's zones, and an issue without zones can only be written alone")
    for n, zone in reading.zones:
        why = zone_problem(zone)
        if why:
            bad(n, f"the engine would take `{zone}` for a path this issue changes, but {why}: write it without "
                   "backticks, or name the real file")

    if "suggested branch:" in draft.body.lower():
        if reading.branch is None:
            bad(0, "the engine reads a branch only as: Suggested branch: `type/short-description`")
        elif not _BRANCH_NAME.match(reading.branch) or reading.branch.startswith("loop/"):
            bad(0, f"`{reading.branch}` is not a branch name of the form type/short-description (lower case; "
                   "`loop/` is the engine's own prefix)")
    return problems


def _heading_of(item: Item) -> str:
    return next(heading for heading, (section, _, _) in _LEDGER_SECTIONS.items() if section == item.section)


def command_words(span: str) -> list[str]:
    """The words of a backticked command, without leading `NAME=value` assignments."""
    words = span.split()
    while words and _ENV_PREFIX.match(words[0]):
        words.pop(0)
    return words


def needs_account(span: str) -> bool:
    """A command only an AWS account can answer: `aws ...`, `cdk deploy` and its kin."""
    words = command_words(span)
    if len(words) > 1 and words[0] == "aws":
        return True
    return any(word == "cdk" and following in ACCOUNT_CDK for word, following in itertools.pairwise(words[:5]))


def is_command(span: str) -> bool:
    """A command the verifier can run: a known program with arguments, or a shell script of the
    repository (`scripts/check.sh all`). A Python file is run through `python3` or `uv run`."""
    words = command_words(span)
    if not words or needs_account(span):
        return False
    return (words[0] in COMMAND_HEADS and len(words) > 1) or bool(_SCRIPT.match(words[0]))


def is_test(span: str) -> bool:
    """A test: a path under a `tests/` folder, or a test file, with or without `::name`."""
    path = span.split("::", 1)[0]
    if not _PATHLIKE.match(path) or path.startswith(("/", "-")):
        return False
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return (path.startswith("tests/") or "/tests/" in path
            or (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py"))


def check_proof(item: Item, lacks: list[str]) -> list[str]:
    """Why this item could not be proven or handed to the owner; [] when it can. An acceptance
    item names its check, or the capability it needs (ADR 0001, decisions 6 and 7)."""
    spans = _TOKEN.findall(item.text)
    prose = _TOKEN.sub("", item.text)
    needs = _NEEDS.findall(prose)
    listed = ", ".join(lacks) or "none"
    problems: list[str] = []
    if len(_NEEDS_LOOSE.findall(prose)) != len(needs):
        problems.append("names a capability in a form the lint does not read: write `(needs <capability>)`, with "
                        f"one of {listed}")
    if len(needs) > 1:
        problems.append("needs more than one capability: the engine hands an item to the owner for one, so split it")
    problems += [f"needs `{cap}`, which is not a capability of {LOOP_TOML} [host] lacks ({listed})"
                 for cap in needs if cap not in lacks]
    if needs and not _NEEDS_END.search(prose):
        problems.append("must end with its `(needs <capability>)`")
    if needs or problems:
        return problems
    problems += [f"rests on `{span}`, which needs an AWS account and agents never have one: end the item with "
                 f"`(needs <capability>)`, one of {listed}" for span in spans if needs_account(span)]
    if item.section == "acceptance" and not problems and not any(is_command(s) or is_test(s) for s in spans):
        problems.append("names no check: an acceptance item carries, in backticks, the command or the test that "
                        "shows it (`uv run pytest tests/...`, `tests/policy/test_x.py`), or ends with "
                        f"`(needs <capability>)`, one of {listed}")
    return problems


def zone_problem(zone: str) -> str | None:
    """Why a backticked token of Deliverables cannot be a path of this repository, or None."""
    segments = zone.rstrip("/").split("/")
    if ".." in segments:
        return "it leaves the repository"
    if "/" not in zone:      # a bare name is a zone only because it ends like a file name; `tests/` is a folder
        extension = zone.rsplit(".", 1)[-1].lower()
        return None if extension in KNOWN_EXTENSIONS else f"`.{extension}` is not a file type this repository holds"
    if any(segment.isdigit() for segment in segments):
        return "it reads as an address or a port"
    if _MIME.match(zone):
        return "it reads as a media type"
    return None


def _zones(draft: Draft) -> list[tuple[int, str]]:
    return draft.reading.zones if draft.reading else []


def check_references(drafts: list[Draft], root: Path) -> list[Problem]:
    """A local path under `## References` exists now, or is delivered by an issue this one waits for."""
    by_id = {d.id: d for d in drafts if d.kind == "issue"}
    problems: list[Problem] = []
    for draft in by_id.values():
        if draft.reading is None:
            continue
        upstream: set[str] = set()
        todo = [ref for ref in draft.blocked_by if isinstance(ref, str)]
        while todo:
            ref = todo.pop()
            if ref in upstream or ref not in by_id or ref == draft.id:
                continue
            upstream.add(ref)
            todo += [r for r in by_id[ref].blocked_by if isinstance(r, str)]
        delivered = [zone.rstrip("/") for ref in upstream for _, zone in _zones(by_id[ref])]
        for n, path in draft.reading.references:
            wanted = path.rstrip("/")
            if ".." in wanted.split("/"):
                problems.append(Problem(draft.rel, draft.at(n), f"the reference `{path}` leaves the repository"))
            elif not (root / wanted).exists() and not any(
                    wanted == z or wanted.startswith(z + "/") or z.startswith(wanted + "/") for z in delivered):
                problems.append(Problem(draft.rel, draft.at(n), f"the reference `{path}` does not exist, and no "
                                        "issue this one is blocked by delivers it: the engine would call the issue "
                                        "stale"))
    return problems


# --------------------------------------------------------------------------------------------
# Order
# --------------------------------------------------------------------------------------------

def order_and_waves(issues: list[Draft]) -> tuple[list[str], dict[str, int]]:
    """The order the engine would work the drafts in, and the wave of each: the engine's own
    rule (implement_loop/graph.py), lowest id first among the issues that are ready."""
    ids = {d.id for d in issues}
    deps = {d.id: {ref for ref in d.blocked_by if isinstance(ref, str) and ref in ids} for d in issues}
    waiting = {draft_id: len(needed) for draft_id, needed in deps.items()}
    ready = sorted((i for i, count in waiting.items() if count == 0), key=id_key)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for other in sorted(ids, key=id_key):
            if current in deps[other]:
                waiting[other] -= 1
                if waiting[other] == 0:
                    ready.append(other)
                    ready.sort(key=id_key)
    if len(order) != len(ids):
        raise BacklogError("the drafts' `blocked_by` links form a cycle: run the lint")
    waves: dict[str, int] = {}
    for draft_id in order:
        waves[draft_id] = 1 + max((waves[dep] for dep in deps[draft_id]), default=-1)
    return order, waves


def _label(draft: Draft, prefix: str) -> str:
    return next((label.split(":", 1)[1] for label in draft.labels if label.startswith(prefix)), "-")


def _after(draft: Draft) -> str:
    refs = [ref if isinstance(ref, str) else f"#{ref}" for ref in draft.blocked_by]
    return f"   after {', '.join(refs)}" if refs else ""


def render_plan(backlog: Backlog, zones: bool) -> str:
    issues = backlog.issues
    order, waves = order_and_waves(issues)
    by_id = {d.id: d for d in issues}
    count = 1 + max(waves.values(), default=-1)
    lines = [f"plan: {len(backlog.epics)} epics, {len(issues)} issues, {count} waves", ""]
    for epic in backlog.epics:
        lines.append(f"{epic.id:<7}{epic.title}")
    published = sorted({d.epic for d in issues if isinstance(d.epic, int)})
    lines += [f"#{n:<6}an epic already on GitHub" for n in published]
    width = max((len(d.title) for d in issues), default=0)
    for wave in range(count):
        lines += ["", f"wave {wave}"]
        for draft_id in (i for i in order if waves[i] == wave):
            draft = by_id[draft_id]
            lines.append(f"  {draft.id:<8}{_label(draft, 'size:'):<3}{_label(draft, 'kind:'):<9}"
                         f"{draft.title:<{width}}{_after(draft)}".rstrip())
            if zones and draft.reading:
                lines += [f"            zone  {zone}" for _, zone in draft.reading.zones]
    lines += ["", "order: " + ", ".join(order)]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------------------------

def render_body(draft: Draft) -> str:
    """The body as it is sent: the marker the publisher finds it by, then the draft's body. The
    marker is an HTML comment before any `## ` heading, so it belongs to no section the engine reads."""
    return f"<!-- backlog-id: {draft.id} -->\n\n{draft.body}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise BacklogError("git is not on PATH") from None


def origin_repo(root: Path) -> str:
    """`owner/name` of the GitHub repository `origin` points at."""
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    if not top or Path(top).resolve() != root.resolve():
        raise BacklogError(f"{root} is not the top of a git repository: the backlog is published from the "
                           "repository it describes, to that repository's `origin`")
    proc = _git(root, "remote", "get-url", "origin")
    url = proc.stdout.strip()
    if proc.returncode != 0 or not url:
        raise BacklogError("no `origin` remote: the backlog is published to the repository `origin` points at, "
                           "and to nothing else")
    match = _GITHUB_REMOTE.match(url)
    if not match:
        raise BacklogError(f"`origin` ({url}) is not a github.com repository")
    return f"{match.group(1)}/{match.group(2)}"


def require_committed(root: Path) -> None:
    """Publishing comes after the first commit, and sends only what the history holds."""
    if _git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        raise BacklogError("this repository has no commit yet: the backlog is published after the first commit "
                           "and push, never before (docs/factory/green-light.md)")
    changed = _git(root, "status", "--porcelain", "--untracked-files=all", "--", DRAFTS).stdout.rstrip()
    if changed:
        raise BacklogError(f"{DRAFTS}/ has uncommitted changes: commit the drafts first, so that what is published "
                           f"is what the history holds\n{changed}")


def scan_payloads(root: Path, drafts: list[Draft]) -> list[str]:
    """The hygiene scan of every title and body exactly as they would be sent."""
    with tempfile.TemporaryDirectory(prefix="backlog-publish-") as tmp:
        paths = []
        for draft in drafts:
            path = Path(tmp) / f"{draft.id}.md"
            path.write_text(f"{draft.title}\n\n{render_body(draft)}", encoding="utf-8")
            paths.append(path)
        prefixes = {f"{tmp}/", f"{Path(tmp).resolve()}/"}
        findings = scan(root, paths)
    for prefix in prefixes:
        findings = [finding.replace(prefix, "what would be sent for ") for finding in findings]
    return findings


class Gh:
    """The REST API of one repository through the authenticated `gh` CLI. Nothing is retried: a
    failed call stops the publication, and the next `publish --apply` carries on from what GitHub
    holds. Writes are spaced out, as GitHub asks of a client that creates content in a row."""

    def __init__(self, repo: str, pause_s: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.repo = repo
        self.pause_s = pause_s
        self._sleep = sleep
        self.writes = 0

    def api(self, method: str, tail: str, payload: dict[str, Any] | None = None) -> Any:
        path = f"repos/{self.repo}/{tail}" if tail else f"repos/{self.repo}"
        args = ["gh", "api", "--hostname", GITHUB_HOST, "--method", method,
                "-H", f"X-GitHub-Api-Version: {API_VERSION}", "-H", "Accept: application/vnd.github+json", path]
        if payload is not None:
            args += ["--input", "-"]
        if method != "GET":
            if self.writes and self.pause_s > 0:
                self._sleep(self.pause_s)
            self.writes += 1
        try:
            proc = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                  capture_output=True, text=True, check=False)
        except FileNotFoundError:
            raise BacklogError("the GitHub CLI (`gh`) is not on PATH") from None
        if proc.returncode != 0:
            raise GhError(f"gh api {method} {path} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
        out = proc.stdout.strip()
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError as exc:
            raise GhError(f"gh api {method} {path} did not answer with JSON: {out[:200]}") from exc

    def get(self, tail: str) -> Any:
        return self.api("GET", tail)

    def pages(self, tail: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        separator = "&" if "?" in tail else "?"
        page = 1
        while True:
            batch = self.get(f"{tail}{separator}per_page={PER_PAGE}&page={page}") or []
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items
            page += 1


def publication_order(backlog: Backlog) -> list[Draft]:
    """Epics first, then the issues in dependency order, so numbers follow the order of work."""
    by_id = {d.id: d for d in backlog.issues}
    return backlog.epics + [by_id[i] for i in order_and_waves(backlog.issues)[0]]


def links(backlog: Backlog) -> tuple[list[tuple[str | int, str]], list[tuple[str, str | int]]]:
    """(parent, child) sub-issue links and (issue, blocker) dependencies the drafts ask for."""
    issues = backlog.issues
    parents = [(d.epic, d.id) for d in issues if d.epic is not None]
    blockers = [(d.id, ref) for d in issues for ref in d.blocked_by]
    return parents, blockers


def render_dry_run(backlog: Backlog, target: str) -> str:
    drafts = publication_order(backlog)
    parents, blockers = links(backlog)
    lines = ["publish: dry run. Nothing is sent and `gh` is not called; `--apply` creates what follows.",
             f"target: {target}",
             f"labels: {', '.join(LABELS)} (created when missing)",
             (f"would create, unless a body on GitHub already carries the id: {len(backlog.epics)} epics, "
              f"{len(backlog.issues)} issues"), ""]
    width = max((len(d.title) for d in drafts), default=0)
    for draft in drafts:
        notes = []
        if draft.kind == "issue":
            notes.append(f"sub-issue of {draft.epic if isinstance(draft.epic, str) else f'#{draft.epic}'}")
            if draft.blocked_by:
                notes.append("blocked by " + ", ".join(r if isinstance(r, str) else f"#{r}" for r in draft.blocked_by))
        lines.append(f"  {draft.kind:<6}{draft.id:<8}{draft.title:<{width}}  [{', '.join(draft.labels)}]"
                     f"{'  ' + '; '.join(notes) if notes else ''}")
    lines += ["", f"would link: {len(parents)} sub-issues, {len(blockers)} dependencies"]
    return "\n".join(lines)


def require_pushed(gh: Gh, branch: str) -> None:
    """Refuse a repository whose default branch has no commit. A remote can exist long before
    the owner's green light; the first push cannot (docs/factory/green-light.md puts it before
    the publication). This is asked before any label, issue or link is even read."""
    head: Any = None
    if branch:
        try:
            head = gh.get(f"branches/{urllib.parse.quote(branch, safe='/')}")
        except GhError as exc:
            if "HTTP 404" not in str(exc):
                raise      # an outage is not an answer: stop, without concluding anything
    if not ((head or {}).get("commit") or {}).get("sha"):
        raise BacklogError(f"nothing is pushed to {gh.repo} yet: its default branch `{branch or 'main'}` has no "
                           "commit. The backlog is published after the first commit is pushed, never before: "
                           "follow docs/factory/green-light.md in order. Nothing was read or created beyond this "
                           "check")


def apply(backlog: Backlog, gh: Gh, say: Callable[[str], None]) -> dict[str, int]:
    """Create on GitHub whatever the drafts describe and GitHub does not hold yet. Reads first,
    and stops before the first write if anything on GitHub contradicts the drafts."""
    repo = gh.get("")
    if str(repo.get("full_name", "")).lower() != gh.repo.lower():
        raise BacklogError(f"`origin` names {gh.repo}, but GitHub answers for {repo.get('full_name')}: "
                           "point `origin` at the repository's current name")
    if repo.get("has_issues") is False:
        raise BacklogError(f"issues are disabled on {gh.repo}")
    require_pushed(gh, str(repo.get("default_branch") or ""))
    existing_labels = {str(label.get("name", "")).lower(): str(label.get("name", "")) for label in gh.pages("labels")}
    for name in LABELS:
        there = existing_labels.get(name.lower(), name)
        if there != name:
            raise BacklogError(f"the label `{there}` exists on {gh.repo}, and GitHub takes it for `{name}`: the "
                               f"engine reads `{name}` exactly, so rename the label there. Nothing was created")
    on_github = [i for i in gh.pages("issues?state=all") if not i.get("pull_request")]

    published: dict[str, dict[str, Any]] = {}
    for issue in on_github:
        for draft_id in MARKER.findall(issue.get("body") or ""):
            other = published.setdefault(draft_id, issue)
            if other["number"] != issue["number"]:
                raise BacklogError(f"#{other['number']} and #{issue['number']} both carry the marker of {draft_id}: "
                                   "remove one marker by hand, nothing was created")
    drafts = publication_order(backlog)
    titles = {issue.get("title"): issue for issue in on_github if not MARKER.search(issue.get("body") or "")}
    for draft in drafts:
        if draft.id not in published and draft.title in titles:
            raise BacklogError(f"{draft.id}: #{titles[draft.title]['number']} already has this title and carries no "
                               f"backlog-id marker. If it is this draft, put `<!-- backlog-id: {draft.id} -->` on "
                               "the first line of its body; otherwise change one title. Nothing was created")
    parents, blockers = links(backlog)
    numbered: dict[int, dict[str, Any]] = {}
    for number in sorted({ref for pair in (*parents, *blockers) for ref in pair if isinstance(ref, int)}):
        try:
            issue = gh.get(f"issues/{number}")
        except GhError as exc:
            raise BacklogError(f"a draft names #{number}, which GitHub does not give: {exc}. Nothing was created") \
                from exc
        if issue.get("pull_request"):
            raise BacklogError(f"a draft names #{number}, which is a pull request. Nothing was created")
        numbered[number] = issue

    done = {"labels": 0, "issues": 0, "sub-issues": 0, "dependencies": 0}

    def on_gh(ref: str | int) -> dict[str, Any]:
        return numbered[ref] if isinstance(ref, int) else published[ref]

    try:
        for name, (colour, description) in LABELS.items():
            if name.lower() not in existing_labels:
                gh.api("POST", "labels", {"name": name, "color": colour, "description": description})
                done["labels"] += 1
                say(f"label {name}: created")
        created: set[int] = set()
        for draft in drafts:
            if draft.id in published:
                there = published[draft.id]
                other = "" if there.get("title") == draft.title else f', there titled "{there.get("title")}"; left as is'
                say(f"{draft.id}: already #{there['number']}{other}")
                continue
            issue = gh.api("POST", "issues", {"title": draft.title, "body": render_body(draft),
                                              "labels": draft.labels})
            published[draft.id] = issue
            created.add(issue["number"])
            done["issues"] += 1
            say(f"{draft.id}: created #{issue['number']}")

        def linked(holder: dict[str, Any], listing: str) -> set[int]:
            """The ids GitHub already links there; an issue created a moment ago has none."""
            return set() if holder["number"] in created else {i["id"] for i in gh.pages(listing)}

        for parent in sorted({p for p, _ in parents}, key=_ref_key):
            holder = on_gh(parent)
            listing = f"issues/{holder['number']}/sub_issues"
            present = linked(holder, listing)
            for child in [c for p, c in parents if p == parent]:
                target = on_gh(child)
                what = f"#{target['number']} ({child}) under #{holder['number']}"
                if target["id"] in present:
                    say(f"{what}: already a sub-issue")
                    continue
                gh.api("POST", listing, {"sub_issue_id": target["id"]})
                done["sub-issues"] += 1
                say(f"{what}: linked as a sub-issue")
        for draft_id in sorted({i for i, _ in blockers}, key=id_key):
            holder = on_gh(draft_id)
            listing = f"issues/{holder['number']}/dependencies/blocked_by"
            present = linked(holder, listing)
            for ref in [r for i, r in blockers if i == draft_id]:
                target = on_gh(ref)
                what = f"#{holder['number']} ({draft_id}) blocked by #{target['number']}"
                if target["id"] in present:
                    say(f"{what}: already linked")
                    continue
                gh.api("POST", listing, {"issue_id": target["id"]})
                done["dependencies"] += 1
                say(f"{what}: linked")
    except GhError as exc:
        raise BacklogError(f"{exc}\npublish: stopped after creating {_counts(done)}. Run `publish --apply` again: "
                           "it finds what exists by its marker and creates only the rest") from exc
    return done


def _ref_key(ref: str | int) -> tuple[int, int, int]:
    """Published issues by number, then drafts by id."""
    return (0, ref, 0) if isinstance(ref, int) else (1, *id_key(ref))


def _counts(done: dict[str, int]) -> str:
    return ", ".join(f"{count} {what}" for what, count in done.items())


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------

def _report_problems(command: str, backlog: Backlog) -> int:
    for problem in backlog.problems:
        print(problem)
    count = len(backlog.problems)
    tail = "" if command == "lint" else f"; `{command}` does nothing until the lint is clean"
    print(f"{command}: {count} problem{'' if count == 1 else 's'}{tail}", file=sys.stderr)
    return 1


def cmd_lint(root: Path, _args: argparse.Namespace) -> int:
    backlog = lint(root)
    if backlog.problems:
        return _report_problems("lint", backlog)
    if not backlog.drafts:
        print(f"lint: no drafts in {DRAFTS}/")
    else:
        print(f"lint: {len(backlog.epics)} epics and {len(backlog.issues)} issues, no problem")
    return 0


def cmd_plan(root: Path, args: argparse.Namespace) -> int:
    backlog = lint(root)
    if backlog.problems:
        return _report_problems("plan", backlog)
    print(render_plan(backlog, args.zones) if backlog.drafts else f"plan: no drafts in {DRAFTS}/")
    return 0


def cmd_publish(root: Path, args: argparse.Namespace) -> int:
    backlog = lint(root)
    if not backlog.problems:
        backlog.problems = [Problem("hygiene", 0, f) for f in scan_payloads(root, backlog.drafts)]
    if backlog.problems:
        return _report_problems("publish", backlog)
    if not backlog.drafts:
        print(f"publish: no drafts in {DRAFTS}/, nothing to publish")
        return 0
    if not args.apply:
        try:
            target = f"{origin_repo(root)} (the `origin` remote)"
        except BacklogError as exc:
            target = f"none; `--apply` would refuse ({exc})"
        print(render_dry_run(backlog, target))
        return 0
    repo = origin_repo(root)
    require_committed(root)
    print(f"publish: applying to {repo}")
    done = apply(backlog, Gh(repo, pause_s=args.pause), print)
    print(f"publish: created {_counts(done)}" if any(done.values())
          else "publish: nothing to create, GitHub already holds every draft and every link")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # No abbreviations anywhere: `--apply` is the one word that publishes, and `--app` must not be.
    parser = argparse.ArgumentParser(
        prog="backlog", description="Lint the issue drafts in backlog/drafts/, show their order, publish them.",
        epilog="Exit status: 0 fine, 1 the drafts have problems, 2 the tool could not do its job.", allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                        help="the repository to work on (default: the one this file is in)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lint", help="check every draft against the engine's input contract", allow_abbrev=False)
    plan = commands.add_parser("plan", help="the order and waves the dependencies give", allow_abbrev=False)
    plan.add_argument("--zones", action="store_true", help="also list the paths the engine takes as each issue's zones")
    publish = commands.add_parser("publish", help="dry run by default: print what would be created on GitHub",
                                  allow_abbrev=False)
    publish.add_argument("--apply", action="store_true", help="create it: issues, sub-issue and dependency links, labels")
    publish.add_argument("--pause", type=float, default=1.0, metavar="SECONDS",
                         help="wait between two writes to GitHub (default: 1)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = {"lint": cmd_lint, "plan": cmd_plan, "publish": cmd_publish}[args.command]
    try:
        return command(args.root.resolve(), args)
    except BacklogError as exc:
        print(f"backlog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
