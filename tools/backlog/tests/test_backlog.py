"""Tests of the backlog tooling (tools/backlog/backlog.py): the lint, the plan, the publisher.

Each test works in a temporary directory that looks like this repository to the tool: the example
drafts of fixtures/good/ (which are clean), the engine's own package (the real one, linked from
this repository, or from the checkout $BACKLOG_TEST_ENGINE names), a loop.toml with the host's
lacks, and stand-ins for the hygiene scanner (fake_scan.py) and the GitHub CLI (fake_gh.py).

Nothing here can reach GitHub: the `gh` on PATH is the stand-in, and the real CLI, were it ever
run by mistake, is pointed at an empty configuration directory and has no token.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import backlog

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ENGINE = Path(os.environ.get("BACKLOG_TEST_ENGINE") or REPO / "tools" / "implementation-loop").resolve()
SLUG = "example-owner/example-repo"
ORIGIN = f"https://github.com/{SLUG}.git"
SENTINEL = "LEAK-SENTINEL"
LOOP_TOML = """\
[host]
lacks = ["aws:account", "aws:deployed-environment", "aws:console", "gpu:capacity", "human:owner-decision"]
"""

TOOLCHAIN = "E1/01-toolchain-pins.md"
APP = "E1/02-empty-app.md"
INGRESS = "E1/03-no-public-ingress.md"
STATUS = "E0/01-deployment-status.md"
EPIC = "E1/epic.md"
EXAMPLES = ("E0/epic.md", STATUS, EPIC, TOOLCHAIN, APP, INGRESS)

A_COMMAND_ITEM = "- [ ] `uv sync --locked` succeeds on a machine with no AWS credentials."
A_DELIVERABLE = "- [ ] `cdk.json` names the app entry point and turns context lookups off."
AN_OWNER_ITEM = ("- [ ] The pipeline's first execution deploys the empty stack to the account "
                 "(needs aws:deployed-environment).")


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


class Sandbox:
    """A temporary repository the tool is pointed at with `--root`."""

    slug = SLUG
    origin = ORIGIN
    sentinel = SENTINEL
    def __init__(self, tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.tmp = tmp
        self.root = tmp / "repo"
        self.drafts = self.root / "backlog" / "drafts"
        self.state_path = tmp / "gh-state.json"
        self.log_path = tmp / "gh-log.jsonl"
        self.scan_log = tmp / "scan-log.jsonl"
        self._capsys = capsys

    # ---------------------------------------------------------------- the tool
    def run(self, *argv: str) -> Result:
        self._capsys.readouterr()
        code = backlog.main(["--root", str(self.root), *argv])
        captured = self._capsys.readouterr()
        return Result(code, captured.out, captured.err)

    def publish(self, *argv: str) -> Result:
        return self.run("publish", "--pause", "0", *argv)

    # ---------------------------------------------------------------- the drafts
    def read(self, rel: str) -> str:
        return (self.drafts / rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        path = self.drafts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def edit(self, rel: str, old: str, new: str) -> None:
        text = self.read(rel)
        assert text.count(old) == 1, f"{rel} holds {text.count(old)} times: {old!r}"
        self.write(rel, text.replace(old, new))

    def append(self, rel: str, after: str, line: str) -> None:
        """Add `line` right after the line that is exactly `after`."""
        self.edit(rel, after + "\n", f"{after}\n{line}\n")

    def clear(self) -> None:
        shutil.rmtree(self.drafts)
        self.drafts.mkdir()
        (self.drafts / ".gitkeep").touch()

    # ---------------------------------------------------------------- git
    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Backlog Tests", "-c", "user.email=backlog-tests@example.com",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, text=True, check=True)
        return proc.stdout

    def init_git(self, origin: str | None = ORIGIN, commit: bool = True) -> None:
        self.git("init", "-q", "-b", "main")
        if origin:
            self.git("remote", "add", "origin", origin)
        if commit:
            self.commit()

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "drafts")

    # ---------------------------------------------------------------- the fake GitHub
    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def set_state(self, **changes: Any) -> None:
        self.state_path.write_text(json.dumps({**self.state, **changes}), encoding="utf-8")

    def seed_issue(self, number: int, title: str, body: str = "", pull_request: bool = False) -> dict[str, Any]:
        issue: dict[str, Any] = {"number": number, "id": 7_000_000 + 13 * number, "title": title, "body": body,
                                 "state": "open", "labels": []}
        if pull_request:
            issue["pull_request"] = {"url": f"https://api.github.com/repos/{SLUG}/pulls/{number}"}
        state = self.state
        state["issues"][str(number)] = issue
        self.set_state(issues=state["issues"])
        return issue

    @property
    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    @property
    def writes(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] != "GET"]

    def forget_calls(self) -> None:
        self.log_path.write_text("", encoding="utf-8")

    def issue(self, draft_id: str) -> dict[str, Any]:
        """The issue on the fake GitHub that carries this draft's marker."""
        marker = re.compile(rf"<!--\s*backlog-id:\s*{draft_id}\s*-->")
        found = [i for i in self.state["issues"].values() if marker.search(i["body"])]
        assert len(found) == 1, f"{len(found)} issues carry the marker of {draft_id}"
        return found[0]

    def scanned(self) -> list[dict[str, str]]:
        if not self.scan_log.exists():
            return []
        return [json.loads(line) for line in self.scan_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def sandbox(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    box = Sandbox(tmp_path, capsys)
    shutil.copytree(HERE / "fixtures" / "good", box.root)
    (box.drafts / ".gitkeep").touch()
    engine = box.root / "tools" / "implementation-loop"
    engine.mkdir(parents=True)
    (engine / "implement_loop").symlink_to(ENGINE / "implement_loop", target_is_directory=True)
    (engine / "loop.toml").write_text(LOOP_TOML, encoding="utf-8")
    (box.root / "tools" / "hygiene").mkdir()
    shutil.copy(HERE / "fake_scan.py", box.root / "tools" / "hygiene" / "scan.py")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_gh.py"}" "$@"\n', encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(box.state_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(box.log_path))
    monkeypatch.setenv("FAKE_SCAN_WORD", SENTINEL)
    monkeypatch.setenv("FAKE_SCAN_LOG", str(box.scan_log))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "no-gh-configuration"))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST", "GH_REPO", "FAKE_SCAN_EXIT"):
        monkeypatch.delenv(name, raising=False)
    box.state_path.write_text(json.dumps({"repo": SLUG, "labels": [], "issues": {}, "subs": {}, "blocked": {},
                                          "branches": {"main": "4f0c1e7a9b2d3c5e6f708192a3b4c5d6e7f80910"}}),
                              encoding="utf-8")
    box.forget_calls()
    return box


# ============================================================================================
# The lint: what a draft must be for the implement loop to finish it. Each refusal is one change
# to the example drafts.
# ============================================================================================

def test_the_example_drafts_are_clean(sandbox: Sandbox) -> None:
    result = sandbox.run("lint")
    assert result.code == 0, result.text
    assert "2 epics and 4 issues, no problem" in result.out


def test_no_draft_is_clean(sandbox: Sandbox) -> None:
    sandbox.clear()
    result = sandbox.run("lint")
    assert result.code == 0, result.text
    assert "no drafts" in result.out


# ------------------------------------------------------------------------------------ refusals

REFUSALS = [
    # front matter
    ("no-front-matter", TOOLCHAIN, '+++\nid = "E1-01"', 'id = "E1-01"', "must start with a `+++` line"),
    ("unclosed-front-matter", TOOLCHAIN, 'epic = "E1"\n+++', 'epic = "E1"', "not closed by a second `+++`"),
    ("invalid-toml", TOOLCHAIN, 'title = "Pin the CDK toolchain"', "title = Pin the toolchain", "not valid TOML"),
    ("unknown-key", TOOLCHAIN, 'epic = "E1"', 'epic = "E1"\nowner = "someone"', "unknown front matter key `owner`"),
    ("id-not-the-path", TOOLCHAIN, 'id = "E1-01"', 'id = "E1-1"', '`id` must be "E1-01"'),
    ("empty-title", TOOLCHAIN, 'title = "Pin the CDK toolchain"', 'title = " "', "`title` must be a non-empty string"),
    ("padded-title", TOOLCHAIN, 'title = "Pin the CDK toolchain"', 'title = " Pin it"', "`title` must be one line"),
    ("same-title", APP, 'title = "An empty app that synthesizes with no credentials"',
     'title = "Pin the CDK toolchain"', "same title as"),
    ("labels-not-a-list", TOOLCHAIN, 'labels = ["size:S", "kind:infra"]', 'labels = "size:S"',
     "`labels` must be a list of strings"),
    ("no-size", TOOLCHAIN, '"size:S", "kind:infra"', '"kind:infra"', "exactly one size label"),
    ("two-sizes", TOOLCHAIN, '"size:S", "kind:infra"', '"size:S", "size:M", "kind:infra"', "exactly one size label"),
    ("no-kind", TOOLCHAIN, '"size:S", "kind:infra"', '"size:S"', "exactly one kind label"),
    ("two-kinds", TOOLCHAIN, '"size:S", "kind:infra"', '"size:S", "kind:infra", "kind:docs"',
     "exactly one kind label"),
    ("unknown-label", TOOLCHAIN, '"size:S", "kind:infra"', '"size:S", "kind:infra", "bug"', "unknown label `bug`"),
    ("engine-owned-label", TOOLCHAIN, '"size:S", "kind:infra"', '"size:S", "kind:infra", "loop:plan"',
     "unknown label `loop:plan`"),
    ("repeated-label", TOOLCHAIN, '"size:S", "kind:infra"', '"size:S", "kind:infra", "kind:infra"',
     "names the same label twice"),
    ("issue-with-epic-kind", TOOLCHAIN, 'epic = "E1"', 'epic = "E1"\nkind = "epic"', "only epic.md has a `kind`"),
    ("epic-without-kind", EPIC, 'kind = "epic"\n', "", 'epic.md must say `kind = "epic"`'),
    ("epic-with-size", EPIC, '["kind:infra"]', '["kind:infra", "size:L"]', "takes no size label"),
    ("epic-with-loop-label", EPIC, '["kind:infra"]', '["kind:infra", "loop:infra"]', "takes no size label"),
    ("epic-with-blocked-by", EPIC, 'kind = "epic"', 'kind = "epic"\nblocked_by = ["E0-01"]',
     "unknown front matter key `blocked_by`"),
    # the graph
    ("no-epic", TOOLCHAIN, 'epic = "E1"\n', "", '`epic` must be "E1"'),
    ("another-folder-epic", TOOLCHAIN, 'epic = "E1"', 'epic = "E0"', '`epic` must be "E1", the folder'),
    ("epic-number-beside-epic-md", TOOLCHAIN, 'epic = "E1"', "epic = 12", "this folder has an epic.md"),
    ("epic-number-zero", TOOLCHAIN, 'epic = "E1"', "epic = 0", '`epic` must be "E1"'),
    ("blocked-by-unknown", APP, '["E1-01"]', '["E1-09"]', "names E1-09, which is no draft"),
    ("blocked-by-itself", APP, '["E1-01"]', '["E1-02"]', "names the draft itself"),
    ("blocked-by-an-epic", APP, '["E1-01"]', '["E0"]', "names the epic E0"),
    ("blocked-by-twice", APP, '["E1-01"]', '["E1-01", "E1-01"]', "names the same issue twice"),
    ("blocked-by-not-a-list", APP, '["E1-01"]', '"E1-01"', "`blocked_by` must be a list"),
    ("blocked-by-a-boolean", APP, '["E1-01"]', "[true]", "`blocked_by` must be a list"),
    ("cycle", TOOLCHAIN, 'epic = "E1"', 'epic = "E1"\nblocked_by = ["E1-03"]',
     "dependency cycle: E1-01 -> E1-03 -> E1-02 -> E1-01"),
    # the ledger, as the engine reads it
    ("no-deliverables", TOOLCHAIN, "## Deliverables", "## Outputs", "no `## Deliverables` section"),
    ("no-acceptance", TOOLCHAIN, "## Acceptance criteria", "## Acceptance", "no `## Acceptance criteria` section"),
    ("deliverables-twice", TOOLCHAIN, "## References", "## Deliverables", "`## Deliverables` appears 2 times"),
    ("no-item", INGRESS, "- [ ] `tests/policy/test_no_public_ingress.py` reads",
     "`tests/policy/test_no_public_ingress.py` reads", "`## Deliverables` has no `- [ ] ` item"),
    ("prose-in-the-ledger", TOOLCHAIN, A_COMMAND_ITEM, A_COMMAND_ITEM + "\n\nThis also holds in CI.",
     "under `## Acceptance criteria` every line is one `- [ ] ` item"),
    ("bullet-without-a-box", TOOLCHAIN, A_DELIVERABLE, A_DELIVERABLE.replace("- [ ] ", "- "),
     "under `## Deliverables` every line is one `- [ ] ` item"),
    ("wrapped-item", TOOLCHAIN, A_DELIVERABLE, A_DELIVERABLE.replace(" and turns", "\n  and turns"),
     "under `## Deliverables` every line is one `- [ ] ` item"),
    ("ticked", TOOLCHAIN, A_DELIVERABLE, A_DELIVERABLE.replace("[ ]", "[x]"), "D2 is already ticked"),
    ("evidence-link", TOOLCHAIN, A_DELIVERABLE, A_DELIVERABLE + " ([evidence](https://example.com/1))",
     "D2 carries an evidence link"),
    ("two-word-item", TOOLCHAIN, A_DELIVERABLE, "- [ ] `cdk.json` exists", "D2 is shorter than 3 words"),
    ("repeated-item", TOOLCHAIN, A_COMMAND_ITEM, f"{A_COMMAND_ITEM}\n{A_COMMAND_ITEM}", "A2 repeats A1"),
    ("details-block", TOOLCHAIN, "## Deliverables", "<details>\n\n## Deliverables", "no `<details>` block"),
    # zones
    ("no-zone", INGRESS, "- [ ] `tests/policy/test_no_public_ingress.py` reads", "- [ ] A policy test reads",
     "Deliverables name no path in backticks"),
    ("zone-is-an-instance-type", TOOLCHAIN, "names the app entry point", "names the `m5.large` fleet",
     "take `m5.large` for a path this issue changes, but `.large` is not a file type"),
    ("zone-is-a-class", TOOLCHAIN, "names the app entry point", "names an `ec2.Vpc`", "`.vpc` is not a file type"),
    ("zone-is-a-network", TOOLCHAIN, "names the app entry point", "names `10.0.0.0/16`",
     "it reads as an address or a port"),
    ("zone-is-a-media-type", TOOLCHAIN, "names the app entry point", "answers `application/json`",
     "it reads as a media type"),
    ("zone-leaves-the-repository", TOOLCHAIN, "names the app entry point", "names `infra/../app.py`",
     "it leaves the repository"),
    # what proves an item (ADR 0001, decisions 6 and 7)
    ("acceptance-in-prose", TOOLCHAIN, A_COMMAND_ITEM, "- [ ] The lock file is complete and current.",
     "A1 names no check"),
    ("acceptance-naming-a-file-only", TOOLCHAIN, A_COMMAND_ITEM, "- [ ] `cdk.json` turns every lookup off.",
     "A1 names no check"),
    ("acceptance-naming-a-bare-program", TOOLCHAIN, A_COMMAND_ITEM, "- [ ] `pytest` is green on the branch.",
     "A1 names no check"),
    ("unknown-capability", APP, "(needs aws:deployed-environment)", "(needs aws:production)",
     "A3 needs `aws:production`, which is not a capability"),
    ("capability-in-another-form", APP, "(needs aws:deployed-environment)", "(needs: aws:deployed-environment)",
     "A3 names a capability in a form the lint does not read"),
    ("capability-misspelt", APP, "(needs aws:deployed-environment)", "(need aws:deployed-environment)",
     "A3 names a capability in a form the lint does not read"),
    ("capability-not-at-the-end", APP, "to the account (needs aws:deployed-environment).",
     "(needs aws:deployed-environment) to the account.", "A3 must end with its `(needs <capability>)`"),
    ("two-capabilities", APP, "(needs aws:deployed-environment)", "(needs aws:account) (needs gpu:capacity)",
     "A3 needs more than one capability"),
    ("aws-command-without-capability", TOOLCHAIN, A_COMMAND_ITEM,
     "- [ ] `aws ec2 describe-instances` lists no running instance.",
     "A1 rests on `aws ec2 describe-instances`, which needs an AWS account"),
    ("deploy-without-capability", TOOLCHAIN, A_COMMAND_ITEM, "- [ ] `npx cdk deploy --all` ends without an error.",
     "A1 rests on `npx cdk deploy --all`, which needs an AWS account"),
    ("deliverable-resting-on-the-account", TOOLCHAIN, A_DELIVERABLE,
     "- [ ] `cdk.json` is what `cdk bootstrap` was run with.", "D2 rests on `cdk bootstrap`"),
    # references and links
    ("missing-reference", TOOLCHAIN, "- `docs/design/system-design.md`, section 8.1", "- `docs/design/missing.md`",
     "the reference `docs/design/missing.md` does not exist"),
    ("reference-delivered-later", TOOLCHAIN, "- `docs/design/system-design.md`, section 8.1", "- `infra/stack.py`",
     "the reference `infra/stack.py` does not exist, and no issue this one is blocked by delivers it"),
    ("relative-link", TOOLCHAIN, "Nothing can be synthesized", "See [the design](docs/design/system-design.md). It",
     "the link to `docs/design/system-design.md` is relative"),
    ("marker-in-the-draft", TOOLCHAIN, "## Context", "<!-- backlog-id: E1-01 -->\n\n## Context",
     "the `backlog-id` marker is added by the publisher"),
    # branches
    ("branch-without-backticks", APP, "Suggested branch: `feat/empty-app`", "Suggested branch: feat/empty-app",
     "the engine reads a branch only as"),
    ("branch-main", APP, "`feat/empty-app`", "`main`", "`main` is not a branch name of the form"),
    ("branch-in-the-engine-prefix", APP, "`feat/empty-app`", "`loop/issue-3`", "`loop/issue-3` is not a branch name"),
    # epics
    ("epic-with-a-ledger", EPIC, "Done when every sub-issue is closed.",
     "## Deliverables\n\n- [ ] `app.py` exists and synthesizes.", "an epic is a tracking issue: the engine never reads"),
    ("epic-without-a-body", EPIC, "Done when every sub-issue is closed.", "", None),
]


@pytest.mark.parametrize(("path", "old", "new", "expected"), [pytest.param(*case[1:], id=case[0]) for case in REFUSALS])
def test_lint_refuses(sandbox: Sandbox, path: str, old: str, new: str, expected: str | None) -> None:
    if expected is None:    # the one case that empties a body: remove all of it
        text = sandbox.read(path)
        sandbox.write(path, text[:text.index("+++", 3) + 4])
        expected = "the body is empty"
    else:
        sandbox.edit(path, old, new)
    result = sandbox.run("lint")
    assert result.code == 1, result.text
    lines = [line for line in result.out.splitlines() if expected in line]
    assert lines, result.out
    assert all(line.startswith(f"backlog/drafts/{path}") for line in lines), lines


def test_a_problem_names_its_line(sandbox: Sandbox) -> None:
    sandbox.edit(TOOLCHAIN, A_COMMAND_ITEM, "- [ ] The lock file is complete and current.")
    number = sandbox.read(TOOLCHAIN).splitlines().index("- [ ] The lock file is complete and current.") + 1
    result = sandbox.run("lint")
    assert f"backlog/drafts/{TOOLCHAIN}:{number}: A1 names no check" in result.out


def test_two_drafts_may_not_suggest_one_branch(sandbox: Sandbox) -> None:
    sandbox.append(TOOLCHAIN, "## Context", "\nSuggested branch: `feat/empty-app`")
    result = sandbox.run("lint")
    assert result.code == 1
    assert "suggests the branch `feat/empty-app`" in result.out


# ------------------------------------------------------------------------------------ the folder

def test_a_stray_file_in_the_drafts_folder_is_refused(sandbox: Sandbox) -> None:
    sandbox.write("notes.md", "to do\n")
    result = sandbox.run("lint")
    assert result.code == 1
    assert "backlog/drafts/notes.md: backlog/drafts/ holds one folder per epic" in result.out


@pytest.mark.parametrize("name", ["3-short-number.md", "04_Underscore.md", "notes.txt", "05-Upper.md"])
def test_a_misnamed_file_in_an_epic_folder_is_refused(sandbox: Sandbox, name: str) -> None:
    sandbox.write(f"E1/{name}", sandbox.read(TOOLCHAIN))
    result = sandbox.run("lint")
    assert result.code == 1
    assert f"backlog/drafts/E1/{name}: an epic folder holds epic.md and issue files named <nn>-<slug>.md" in result.out


def test_two_files_with_one_number_are_refused(sandbox: Sandbox) -> None:
    sandbox.write("E1/01-again.md", sandbox.read(TOOLCHAIN).replace("Pin the CDK toolchain", "Pin it again"))
    result = sandbox.run("lint")
    assert result.code == 1
    assert "has the same number: both would be E1-01" in result.out


def test_an_epic_needs_an_issue(sandbox: Sandbox) -> None:
    sandbox.write("E2/epic.md", sandbox.read(EPIC).replace('"E1"', '"E2"').replace("Walking", "Running"))
    result = sandbox.run("lint")
    assert result.code == 1
    assert "backlog/drafts/E2/epic.md:1: an epic needs at least one issue draft" in result.out


def test_an_issue_needs_its_epic_draft_or_a_number(sandbox: Sandbox) -> None:
    text = sandbox.read(TOOLCHAIN).replace("E1-01", "E2-01").replace("Pin the CDK", "Pin another")
    sandbox.write("E2/01-later-work.md", text.replace('epic = "E1"', 'epic = "E2"'))
    result = sandbox.run("lint")
    assert result.code == 1
    assert "epic E2 has no draft" in result.out

    sandbox.write("E2/01-later-work.md", text.replace('epic = "E1"', "epic = 12"))
    result = sandbox.run("lint")
    assert result.code == 0, result.text


# ------------------------------------------------------------------------------------ accepted

@pytest.mark.parametrize("zone", ["tests/", "client/", "tests/policy/", "infra/stack.py", "cdk.json", "README.md"])
def test_a_folder_or_a_file_is_a_zone(zone: str) -> None:
    assert backlog.is_path(zone)
    assert backlog.zone_problem(zone) is None


def test_published_issues_are_named_by_number(sandbox: Sandbox) -> None:
    sandbox.edit(APP, 'blocked_by = ["E1-01"]', 'blocked_by = ["E1-01", 57]')
    assert sandbox.run("lint").code == 0


def test_a_deliverable_may_be_the_owners(sandbox: Sandbox) -> None:
    sandbox.edit(TOOLCHAIN, A_DELIVERABLE, "- [ ] `cdk.json` carries the qualifier the account was bootstrapped with "
                                           "by `cdk bootstrap` (needs aws:account).")
    assert sandbox.run("lint").code == 0


def test_an_item_about_the_form_itself_is_not_a_capability(sandbox: Sandbox) -> None:
    sandbox.edit(TOOLCHAIN, A_COMMAND_ITEM, "- [ ] `uv run pytest tests/unit/test_lint.py` shows that an item "
                                            "ending in `(needs aws:nothing)` is refused.")
    assert sandbox.run("lint").code == 0


def test_a_reference_may_be_delivered_by_a_blocker_of_a_blocker(sandbox: Sandbox) -> None:
    sandbox.edit(INGRESS, "- `infra/stack.py`", "- `infra/stack.py`\n- `cdk.json`\n- `tests/unit/`")
    assert sandbox.run("lint").code == 0


def test_every_acceptance_item_may_be_the_owners(sandbox: Sandbox) -> None:
    """ADR 0001, decision 7 refuses a draft with no checkable part and no named obligation; one
    whose acceptance is all obligations still ends `delivered`, with the agents' deliverables proven."""
    text = sandbox.read(APP)
    start, end = text.index("## Acceptance criteria"), text.index("## References")
    sandbox.write(APP, f"{text[:start]}## Acceptance criteria\n\n{AN_OWNER_ITEM}\n\n{text[end:]}")
    assert sandbox.run("lint").code == 0


# ------------------------------------------------------------------------------------ what the lint depends on

def test_the_engine_parser_is_required(sandbox: Sandbox) -> None:
    (sandbox.root / "tools" / "implementation-loop" / "implement_loop").unlink()
    result = sandbox.run("lint")
    assert result.code == 2
    assert "tools/implementation-loop/implement_loop/issue_parser.py" in result.err
    assert "is missing" in result.err


def _fake_engine(sandbox: Sandbox, source: str) -> None:
    package = sandbox.root / "tools" / "implementation-loop" / "implement_loop"
    package.unlink()
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "issue_parser.py").write_text(source, encoding="utf-8")


def test_an_engine_parser_without_the_fixed_interface_is_refused(sandbox: Sandbox) -> None:
    _fake_engine(sandbox, "def sections(body):\n    return {}\n")
    result = sandbox.run("lint")
    assert result.code == 2
    assert "has no ledger()" in result.err


def test_an_engine_parser_that_cannot_be_imported_is_refused(sandbox: Sandbox) -> None:
    _fake_engine(sandbox, "from .nowhere import nothing\n")
    result = sandbox.run("lint")
    assert result.code == 2
    assert "could not be imported" in result.err


def test_the_lint_stops_when_the_engine_reads_a_body_differently(sandbox: Sandbox) -> None:
    """The lint repeats the engine's patterns to know the line of each item. An engine that has
    moved on must stop it: a lint that reads a draft its own way proves nothing about the loop."""
    package = sandbox.root / "tools" / "implementation-loop" / "implement_loop"
    package.unlink()
    shutil.copytree(ENGINE / "implement_loop", package, ignore=shutil.ignore_patterns("__pycache__"))
    with (package / "issue_parser.py").open("a", encoding="utf-8") as handle:
        handle.write("\n_ledger = ledger\n\n\ndef ledger(body):\n    return _ledger(body)[1:]\n")
    result = sandbox.run("lint")
    assert result.code == 2
    assert "no longer read an issue body the same way" in result.err


def test_loop_toml_is_required(sandbox: Sandbox) -> None:
    (sandbox.root / "tools" / "implementation-loop" / "loop.toml").unlink()
    result = sandbox.run("lint")
    assert result.code == 2
    assert "tools/implementation-loop/loop.toml is missing" in result.err


def test_capabilities_come_from_loop_toml(sandbox: Sandbox) -> None:
    (sandbox.root / "tools" / "implementation-loop" / "loop.toml").write_text('[host]\nlacks = ["gpu:capacity"]\n',
                                                                               encoding="utf-8")
    result = sandbox.run("lint")
    assert result.code == 1
    assert "needs `aws:deployed-environment`, which is not a capability" in result.out


def test_a_missing_hygiene_scanner_fails_the_lint(sandbox: Sandbox) -> None:
    (sandbox.root / "tools" / "hygiene" / "scan.py").unlink()
    result = sandbox.run("lint")
    assert result.code == 2
    assert "the hygiene scanner is missing (tools/hygiene/scan.py)" in result.err

    sandbox.clear()
    result = sandbox.run("lint")
    assert result.code == 2, "a missing scanner is a failure even when there is nothing to scan"


def test_a_hygiene_finding_fails_the_lint(sandbox: Sandbox) -> None:
    sandbox.edit(TOOLCHAIN, "Nothing can be synthesized", f"{sandbox.sentinel} Nothing can be synthesized")
    result = sandbox.run("lint")
    assert result.code == 1
    assert f"hygiene: backlog/drafts/{TOOLCHAIN}:10: fake-rule: [redacted]" in result.out
    assert sandbox.sentinel not in result.text


def test_the_scanner_reads_every_draft(sandbox: Sandbox) -> None:
    assert sandbox.run("lint").code == 0
    assert sorted(entry["path"] for entry in sandbox.scanned()) == sorted(
        f"backlog/drafts/{name}" for name in ("E0/epic.md", STATUS, EPIC, TOOLCHAIN, APP, INGRESS))


def test_a_scanner_that_cannot_run_fails_the_lint(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SCAN_EXIT", "2")
    result = sandbox.run("lint")
    assert result.code == 2
    assert "the hygiene scanner could not run (exit 2)" in result.err


# ------------------------------------------------------------------------------------ the rules, one by one

@pytest.mark.parametrize("span", [
    "uv run pytest tests/policy -q", "uv sync --locked", "uvx ruff@0.17.3 check .", "python3 tools/hygiene/scan.py",
    "npx cdk synth --quiet", "cdk synth", "scripts/check.sh all", "./scripts/check.sh", "python3 tools/backlog/backlog.py lint",
    "CDK_DEFAULT_REGION= npx cdk synth", "git diff --exit-code uv.lock", "grep -R 0.0.0.0/0 cdk.out", "test -f cdk.json",
])
def test_a_command(span: str) -> None:
    assert backlog.is_command(span)


@pytest.mark.parametrize("span", [
    "pytest", "uv", "cdk.json", "AWS::EC2::Instance", "max_size = 4", "check.sh", "aws s3 ls", "npx cdk deploy",
    "cdk diff", "gh issue list", "docker build .", "FOO=bar", "infra/stack.py", "tools/backlog/backlog.py lint",
])
def test_not_a_command(span: str) -> None:
    assert not backlog.is_command(span)


@pytest.mark.parametrize("span", [
    "tests/policy/test_ingress.py", "tests/policy/", "tests/policy/test_ingress.py::test_no_open_rule",
    "tools/backlog/tests/test_lint.py", "infra/test_stack.py", "infra/stack_test.py", "test_app.py",
])
def test_a_test(span: str) -> None:
    assert backlog.is_test(span)


@pytest.mark.parametrize("span", ["infra/stack.py", "the tests", "tests", "/tests/x.py", "contest/app.py", "latest.py"])
def test_not_a_test(span: str) -> None:
    assert not backlog.is_test(span)


@pytest.mark.parametrize("span", [
    "aws sts get-caller-identity", "AWS_PROFILE=x aws s3 ls", "cdk deploy", "npx cdk deploy --all", "uv run cdk bootstrap",
    "npx --no-install cdk destroy", "cdk diff",
])
def test_a_command_only_an_account_answers(span: str) -> None:
    assert backlog.needs_account(span)


@pytest.mark.parametrize("span", ["cdk synth", "npx cdk ls", "aws", "uv run pytest tests/ -k deploy", "aws_cdk.App"])
def test_a_command_agents_can_run(span: str) -> None:
    assert not backlog.needs_account(span)


# ------------------------------------------------------------------------------------ in step with the engine

BODIES = [
    "",
    "no heading at all\n- [ ] not an item\n",
    ("## Deliverables\n- [ ] `a/b.py` one\n* [x] `c.md` two ([evidence](https://example.com/e))\n  - [ ] nested `d/e`\n"
    "## Acceptance criteria\n- [X] three words here\n-[ ] not a box\n- [] nor this\n"),
    "## DELIVERABLES  \n- [ ] `x/y.py` upper case heading\n##  Acceptance   Criteria\n- [ ] `uv run pytest` ok\n",
    "## Deliverables\r\n- [ ] `a/b.py` with carriage returns\r\n\r\n## References\r\n- `docs/x.md`\r\n",
    "## Deliverables\n- [ ] `a/b.py` kept\n<details>\n- [ ] `c/d.py` lost\n</details>\n## Deliverables\n- [ ] `e/f.py` back\n",
    ("## References\n- `README.md` and `.github/workflows/ci.yml`, not `.env`, `-x/y` or `/abs/path`\n"
    "- [a link](https://example.com) with `skipped/because.md` of the link\n- `10.0.0.0/8` `g5.xlarge` `trailing.`\n"),
    "### Deliverables\n- [ ] `a/b.py` under a third-level heading is no ledger\n## Deliverables\n- [ ] `g.py` `g.py` twice\n",
]


@pytest.mark.parametrize("body", BODIES)
def test_the_lint_reads_a_body_as_the_engine_does(sandbox: Sandbox, body: str) -> None:
    """`read_body` raises when its reading differs from the engine's; here it must also agree with
    the engine's own path functions, which it compares against whenever the engine has them."""
    parser = backlog.engine_parser(sandbox.root)
    reading = backlog.read_body(body, parser)
    assert [(i.id, i.text, i.checked) for i in reading.items] == [(i.id, i.text, i.checked) for i in parser.ledger(body)]
    if callable(getattr(parser, "declared_paths", None)):
        assert [path for _, path in reading.zones] == list(parser.declared_paths(body))
    if callable(getattr(parser, "reference_paths", None)):
        assert [path for _, path in reading.references] == list(parser.reference_paths(body))


def test_the_example_bodies_have_the_ledger_the_engine_will_work(sandbox: Sandbox) -> None:
    parser = backlog.engine_parser(sandbox.root)
    drafts, problems = backlog.load_drafts(sandbox.root)
    assert not problems
    app = next(d for d in drafts if d.id == "E1-02")
    assert [(i.id, i.section) for i in parser.ledger(backlog.render_body(app))] == [
        ("D1", "deliverable"), ("D2", "deliverable"), ("A1", "acceptance"), ("A2", "acceptance"), ("A3", "acceptance")]
    assert [path for _, path in backlog.read_body(app.body, parser).zones] == [
        "app.py", "infra/stack.py", "tests/unit/test_synth.py"]


def test_the_wrapper_script_runs_the_tool(sandbox: Sandbox) -> None:
    bash = shutil.which("bash")
    assert bash
    proc = subprocess.run([bash, str(REPO / "scripts" / "backlog.sh"), "--root", str(sandbox.root), "lint"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no problem" in proc.stdout


# ============================================================================================
# The plan: the order and waves the dependencies give, by the engine's own rule
# ============================================================================================

def _waves(text: str) -> dict[str, int]:
    waves: dict[str, int] = {}
    current = -1
    for line in text.splitlines():
        if line.startswith("wave "):
            current = int(line.split()[1])
        elif current >= 0 and re.match(r"  E[0-9]+-[0-9]{2} ", line):
            waves[line.split()[0]] = current
    return waves


def test_plan_orders_the_examples(sandbox: Sandbox) -> None:
    result = sandbox.run("plan")
    assert result.code == 0, result.text
    assert "plan: 2 epics, 4 issues, 3 waves" in result.out
    assert "order: E0-01, E1-01, E1-02, E1-03" in result.out
    assert _waves(result.out) == {"E0-01": 0, "E1-01": 0, "E1-02": 1, "E1-03": 2}
    assert "after E1-02, E0-01" in result.out
    assert "zone" not in result.out


def test_plan_lists_the_zones_the_engine_will_take(sandbox: Sandbox) -> None:
    result = sandbox.run("plan", "--zones")
    assert result.code == 0, result.text
    zones = [line.split()[1] for line in result.out.splitlines() if line.strip().startswith("zone ")]
    assert zones == ["infra/pipeline/status_reporter.py", "tests/unit/test_status_reporter.py", "pyproject.toml",
                     "uv.lock", "cdk.json", "app.py", "infra/stack.py", "tests/unit/test_synth.py",
                     "tests/policy/test_no_public_ingress.py"]


def test_plan_shows_prerequisites_already_on_github(sandbox: Sandbox) -> None:
    sandbox.edit(APP, 'blocked_by = ["E1-01"]', 'blocked_by = ["E1-01", 57]')
    result = sandbox.run("plan")
    assert result.code == 0, result.text
    assert "after E1-01, #57" in result.out
    assert _waves(result.out)["E1-02"] == 1


def test_plan_refuses_drafts_with_problems(sandbox: Sandbox) -> None:
    sandbox.edit(APP, '["E1-01"]', '["E1-09"]')
    result = sandbox.run("plan")
    assert result.code == 1
    assert "wave" not in result.out
    assert "`plan` does nothing until the lint is clean" in result.err


def test_plan_with_no_draft(sandbox: Sandbox) -> None:
    sandbox.clear()
    result = sandbox.run("plan")
    assert result.code == 0
    assert "no drafts" in result.out


@pytest.mark.parametrize("seed", range(12))
def test_the_order_is_the_engines(sandbox: Sandbox, seed: int) -> None:
    """Same order and same waves as implement_loop/graph.py gives the published issues, whose
    numbers follow the ids."""
    graph = backlog.engine_module(sandbox.root, "graph")
    rng = random.Random(seed)
    ids = [f"E{epic}-{number:02d}" for epic in (0, 2, 10) for number in range(1, rng.randint(2, 6))]
    rng.shuffle(ids)
    drafts = []
    for position, draft_id in enumerate(ids):
        blockers: list[str | int] = [other for other in ids[:position] if rng.random() < 0.35]
        drafts.append(backlog.Draft(rel="", kind="issue", id=draft_id, title=draft_id, labels=[],
                                    epic=draft_id.split("-")[0], blocked_by=blockers, body="", body_line=1))
    number = {draft_id: n for n, draft_id in enumerate(sorted(ids, key=backlog.id_key), 1)}
    deps = {number[d.id]: {number[b] for b in d.blocked_by if isinstance(b, str)} for d in drafts}

    order, waves = backlog.order_and_waves(drafts)
    theirs = graph.topological_order(set(number.values()), deps)
    assert [number[i] for i in order] == theirs
    assert {number[i]: wave for i, wave in waves.items()} == graph.compute_waves(theirs, deps)


def test_neither_lint_nor_plan_calls_gh(sandbox: Sandbox) -> None:
    sandbox.init_git()
    assert sandbox.run("lint").code == 0
    assert sandbox.run("plan").code == 0
    assert sandbox.calls == []


# ============================================================================================
# The publisher. Without --apply it calls nothing; with it, a second run creates nothing.
# ============================================================================================

def _loaded(sandbox: Sandbox) -> dict[str, backlog.Draft]:
    drafts, problems = backlog.load_drafts(sandbox.root)
    assert not problems
    return {draft.id: draft for draft in drafts}


def test_a_dry_run_is_the_default_and_calls_nothing(sandbox: Sandbox) -> None:
    sandbox.init_git()
    result = sandbox.publish()
    assert result.code == 0, result.text
    assert sandbox.calls == []
    assert sandbox.state["issues"] == {}
    assert sandbox.state["labels"] == []
    assert "dry run" in result.out
    assert f"target: {SLUG} (the `origin` remote)" in result.out
    assert "2 epics, 4 issues" in result.out
    assert "would link: 4 sub-issues, 3 dependencies" in result.out
    listed = [line.split()[1] for line in result.out.splitlines() if line.startswith(("  epic", "  issue"))]
    assert listed == ["E0", "E1", "E0-01", "E1-01", "E1-02", "E1-03"]
    assert "sub-issue of E1; blocked by E1-02, E0-01" in result.out


def test_a_dry_run_needs_no_repository(sandbox: Sandbox) -> None:
    result = sandbox.publish()
    assert result.code == 0, result.text
    assert "target: none; `--apply` would refuse" in result.out
    assert sandbox.calls == []


def test_a_dry_run_of_drafts_with_problems_calls_nothing(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.edit(APP, '["E1-01"]', '["E1-09"]')
    result = sandbox.publish()
    assert result.code == 1
    assert "names E1-09" in result.out
    assert "`publish` does nothing until the lint is clean" in result.err
    assert sandbox.calls == []


@pytest.mark.parametrize("flag", ["--app", "--a", "-a", "--apply=yes", "--APPLY", "apply"])
def test_only_the_exact_flag_applies(sandbox: Sandbox, flag: str) -> None:
    sandbox.init_git()
    with pytest.raises(SystemExit) as refused:
        sandbox.publish(flag)
    assert refused.value.code == 2
    assert sandbox.calls == []


def test_apply_refuses_drafts_with_problems(sandbox: Sandbox) -> None:
    sandbox.edit(APP, '["E1-01"]', '["E1-09"]')
    sandbox.init_git()
    result = sandbox.publish("--apply")
    assert result.code == 1
    assert sandbox.calls == []


def test_apply_needs_an_origin_remote(sandbox: Sandbox) -> None:
    sandbox.init_git(origin=None)
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "no `origin` remote" in result.err
    assert sandbox.calls == []


@pytest.mark.parametrize("url", ["https://git.example.com/example-owner/example-repo.git", "../elsewhere.git",
                                 "https://github.com/example-owner", "https://github.example.com/a/b.git"])
def test_apply_needs_origin_on_github(sandbox: Sandbox, url: str) -> None:
    sandbox.init_git(origin=url)
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "is not a github.com repository" in result.err
    assert sandbox.calls == []


@pytest.mark.parametrize("url", [f"https://github.com/{SLUG}.git", f"https://github.com/{SLUG}",
                                 f"git@github.com:{SLUG}.git", f"ssh://git@github.com/{SLUG}.git"])
def test_origin_names_the_repository(sandbox: Sandbox, url: str) -> None:
    sandbox.init_git(origin=url)
    assert backlog.origin_repo(sandbox.root) == SLUG


def test_apply_needs_to_be_at_the_top_of_its_own_repository(sandbox: Sandbox) -> None:
    """A folder inside another repository must not publish to that repository's origin."""
    inner = sandbox.root
    outer = sandbox.tmp
    subprocess.run(["git", "-C", str(outer), "init", "-q", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(outer), "remote", "add", "origin", ORIGIN], check=True, capture_output=True)
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert f"{inner} is not the top of a git repository" in result.err
    assert sandbox.calls == []


def test_apply_needs_a_first_commit(sandbox: Sandbox) -> None:
    sandbox.init_git(commit=False)
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "has no commit yet" in result.err
    assert sandbox.calls == []


def test_apply_sends_only_committed_drafts(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.edit(TOOLCHAIN, "Nothing can be synthesized", "Nothing at all can be synthesized")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "backlog/drafts/ has uncommitted changes" in result.err
    assert TOOLCHAIN in result.err
    assert sandbox.calls == []

    sandbox.git("checkout", "--", ".")
    sandbox.write("E1/04-new.md", sandbox.read(TOOLCHAIN).replace("E1-01", "E1-04").replace("Pin the", "Pin another"))
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "E1/04-new.md" in result.err
    assert sandbox.calls == []


def test_apply_refuses_a_repository_nothing_was_pushed_to(sandbox: Sandbox) -> None:
    """A remote that exists is not the green light: the owner's repository was created, empty,
    before any of this was written. The first push is what the publication waits for."""
    sandbox.init_git()
    sandbox.set_state(branches={})
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert f"nothing is pushed to {SLUG} yet" in result.err
    assert "docs/factory/green-light.md" in result.err
    assert [(call["method"], call["path"]) for call in sandbox.calls] == [
        ("GET", f"repos/{SLUG}"), ("GET", f"repos/{SLUG}/branches/main")], "no label, issue or link is even read"
    state = sandbox.state
    assert (state["labels"], state["issues"], state["subs"], state["blocked"]) == ([], {}, {}, {})

    sandbox.forget_calls()
    dry = sandbox.publish()
    assert dry.code == 0, dry.text
    assert "would create" in dry.out
    assert sandbox.calls == [], "the dry run stays available, and still calls nothing"


def test_the_pushed_branch_must_be_the_default_one(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(default_branch="trunk", branches={"main": "4f0c1e7a9b2d3c5e6f708192a3b4c5d6e7f80910"})
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "its default branch `trunk` has no commit" in result.err
    assert sandbox.writes == []

    sandbox.set_state(branches={"trunk": "4f0c1e7a9b2d3c5e6f708192a3b4c5d6e7f80910"})
    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert len(sandbox.state["issues"]) == 6


def test_an_outage_while_asking_for_the_branch_stops_without_a_verdict(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(fail_get="/branches/")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "HTTP 502" in result.err
    assert "nothing is pushed" not in result.err
    assert [call["path"] for call in sandbox.calls] == [f"repos/{SLUG}", f"repos/{SLUG}/branches/main"]


def test_a_repository_that_names_no_default_branch_is_refused(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(default_branch="")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "nothing is pushed" in result.err
    assert [call["path"] for call in sandbox.calls] == [f"repos/{SLUG}"]


def test_apply_with_no_draft_calls_nothing(sandbox: Sandbox) -> None:
    sandbox.clear()
    sandbox.init_git()
    result = sandbox.publish("--apply")
    assert result.code == 0
    assert "nothing to publish" in result.out
    assert sandbox.calls == []


def test_apply_creates_labels_issues_and_links(sandbox: Sandbox) -> None:
    sandbox.init_git()
    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    state = sandbox.state
    drafts = _loaded(sandbox)

    assert {label["name"]: (label["color"], label["description"]) for label in state["labels"]} == backlog.LABELS
    assert "labels_created_by_issues" not in state, "every label exists before an issue uses it"

    numbers = {draft_id: sandbox.issue(draft_id)["number"] for draft_id in drafts}
    assert numbers == {"E0": 1, "E1": 2, "E0-01": 3, "E1-01": 4, "E1-02": 5, "E1-03": 6}
    for draft_id, draft in drafts.items():
        issue = sandbox.issue(draft_id)
        assert issue["title"] == draft.title
        assert issue["body"] == f"<!-- backlog-id: {draft_id} -->\n\n{draft.body}"
        assert issue["body"].endswith(sandbox.read(draft.rel.removeprefix("backlog/drafts/")).split("+++\n", 2)[2].strip() + "\n")
        assert [label["name"] for label in issue["labels"]] == draft.labels

    ids = {draft_id: sandbox.issue(draft_id)["id"] for draft_id in drafts}
    assert state["subs"] == {"1": [ids["E0-01"]], "2": [ids["E1-01"], ids["E1-02"], ids["E1-03"]]}
    assert state["blocked"] == {"5": [ids["E1-01"]], "6": [ids["E1-02"], ids["E0-01"]]}
    assert "created 9 labels, 6 issues, 4 sub-issues, 3 dependencies" in result.out


def test_apply_calls_the_documented_endpoints(sandbox: Sandbox) -> None:
    sandbox.init_git()
    assert sandbox.publish("--apply").code == 0
    prefix = f"repos/{SLUG}"
    for call in sandbox.calls:
        assert call["argv"][:5] == ["api", "--hostname", "github.com", "--method", call["method"]]
        assert "X-GitHub-Api-Version: 2022-11-28" in call["argv"]
        assert "Accept: application/vnd.github+json" in call["argv"]
    reads = [call["path"] for call in sandbox.calls if call["method"] == "GET"]
    assert reads == [prefix, f"{prefix}/branches/main", f"{prefix}/labels?per_page=100&page=1",
                     f"{prefix}/issues?state=all&per_page=100&page=1"]
    writes = [(call["method"], call["path"]) for call in sandbox.writes]
    assert writes == (
        [("POST", f"{prefix}/labels")] * 9 + [("POST", f"{prefix}/issues")] * 6
        + [("POST", f"{prefix}/issues/1/sub_issues")] + [("POST", f"{prefix}/issues/2/sub_issues")] * 3
        + [("POST", f"{prefix}/issues/5/dependencies/blocked_by")]
        + [("POST", f"{prefix}/issues/6/dependencies/blocked_by")] * 2)
    links = [call["payload"] for call in sandbox.writes if call["path"].endswith(("sub_issues", "blocked_by"))]
    assert [sorted(payload) for payload in links] == [["sub_issue_id"]] * 4 + [["issue_id"]] * 3
    assert all(isinstance(value, int) and value > 7_000_000 for payload in links for value in payload.values()), \
        "a link names an issue by its id, never by its number"


def test_a_second_apply_creates_nothing(sandbox: Sandbox) -> None:
    sandbox.init_git()
    assert sandbox.publish("--apply").code == 0
    before = sandbox.state
    sandbox.forget_calls()

    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert sandbox.writes == []
    assert sandbox.state == before
    assert "nothing to create" in result.out
    assert "E1-02: already #5" in result.out
    assert "#5 (E1-02) under #2: already a sub-issue" in result.out
    assert "#6 (E1-03) blocked by #3: already linked" in result.out


def test_a_second_apply_reads_every_page(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backlog, "PER_PAGE", 2)
    sandbox.init_git()
    assert sandbox.publish("--apply").code == 0
    sandbox.forget_calls()
    assert sandbox.publish("--apply").code == 0
    assert sandbox.writes == []
    assert f"repos/{SLUG}/issues?state=all&per_page=2&page=4" in [call["path"] for call in sandbox.calls]
    assert len(sandbox.state["issues"]) == 6


def test_the_engine_reads_back_the_graph_that_was_published(sandbox: Sandbox) -> None:
    """The engine's own GitHub client and planner, pointed at what the publisher created."""
    sandbox.init_git()
    assert sandbox.publish("--apply").code == 0
    github = backlog.engine_module(sandbox.root, "github")
    graph = backlog.engine_module(sandbox.root, "graph")

    plan = graph.build_plan(github.GhCli(SLUG), 2, sandbox.root)      # the epic E1
    assert plan.mode == "epic"
    assert plan.tracking == {2: [4, 5, 6]}
    assert plan.work == [3, 4, 5, 6]
    assert plan.external == {3}, "E0-01 blocks E1-03 from another epic: the engine pulls it in"
    assert plan.deps == {3: set(), 4: set(), 5: {4}, 6: {3, 5}}
    assert plan.waves == {3: 0, 4: 0, 5: 1, 6: 2}
    assert [item.id for item in plan.specs[5].ledger] == ["D1", "D2", "A1", "A2", "A3"]
    assert not any(item.checked for spec in plan.specs.values() for item in spec.ledger)
    assert plan.specs[5].branch == "feat/empty-app"
    assert plan.specs[5].declared_paths == ["app.py", "infra/stack.py", "tests/unit/test_synth.py"]
    assert plan.issues[5].size == "M"
    assert "loop:infra" in plan.issues[3].labels
    assert not [w for w in plan.warnings if "no Deliverables" in w or "same branch" in w]

    single = graph.build_plan(github.GhCli(SLUG), 6, sandbox.root)    # one issue and its open prerequisites
    assert single.mode == "issue"
    assert single.work == [3, 4, 5, 6]


def test_apply_carries_on_after_a_failed_write(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(fail_post=12)          # nine labels, two epics, then the first issue fails
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "HTTP 502" in result.err
    assert "stopped after creating 9 labels, 2 issues, 0 sub-issues, 0 dependencies" in result.err
    assert "Run `publish --apply` again" in result.err
    assert len(sandbox.state["issues"]) == 2

    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert "created 0 labels, 4 issues, 4 sub-issues, 3 dependencies" in result.out
    assert len(sandbox.state["issues"]) == 6
    ids = {draft_id: sandbox.issue(draft_id)["id"] for draft_id in _loaded(sandbox)}
    assert sandbox.state["subs"] == {"1": [ids["E0-01"]], "2": [ids["E1-01"], ids["E1-02"], ids["E1-03"]]}
    assert sandbox.state["blocked"] == {"5": [ids["E1-01"]], "6": [ids["E1-02"], ids["E0-01"]]}


def test_a_write_whose_answer_was_lost_is_not_repeated(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(fail_post=12, fail_after_effect=True)     # the issue is created, the answer is a 502
    assert sandbox.publish("--apply").code == 2
    assert len(sandbox.state["issues"]) == 3

    assert sandbox.publish("--apply").code == 0
    assert len(sandbox.state["issues"]) == 6
    for draft_id in _loaded(sandbox):
        sandbox.issue(draft_id)      # exactly one issue carries each marker


def test_a_failed_link_is_made_by_the_next_apply(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(fail_post=18)          # 9 labels, 6 issues, 2 sub-issue links, then a failure
    assert sandbox.publish("--apply").code == 2
    assert sum(len(children) for children in sandbox.state["subs"].values()) == 2

    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert "created 0 labels, 0 issues, 2 sub-issues, 3 dependencies" in result.out
    assert "#4 (E1-01) under #2: already a sub-issue" in result.out


def test_an_issue_published_earlier_is_found_by_its_marker_and_left_alone(sandbox: Sandbox) -> None:
    sandbox.init_git()
    body = "<!--backlog-id:  E1-01 -->\r\n\r\nEdited on GitHub since.\r\n"
    held = sandbox.seed_issue(40, "Pin the toolchain (retitled on GitHub)", body)
    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert 'E1-01: already #40, there titled "Pin the toolchain (retitled on GitHub)"; left as is' in result.out
    after = sandbox.issue("E1-01")
    assert (after["number"], after["title"], after["body"]) == (40, held["title"], body)
    assert len(sandbox.state["issues"]) == 6
    epic = sandbox.issue("E1")
    assert held["id"] in sandbox.state["subs"][str(epic["number"])]
    assert sandbox.state["blocked"][str(sandbox.issue("E1-02")["number"])] == [held["id"]]


def test_existing_labels_are_kept_as_they_are(sandbox: Sandbox) -> None:
    sandbox.init_git()
    by_hand = {"name": "size:S", "color": "000000", "description": "set by hand"}
    sandbox.set_state(labels=[by_hand])
    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert by_hand in sandbox.state["labels"]
    assert len(sandbox.state["labels"]) == 9
    assert "created 8 labels" in result.out


def test_a_label_in_another_case_stops_the_publication(sandbox: Sandbox) -> None:
    """GitHub would attach `Size:S` to an issue that asks for `size:S`, and the engine, which
    reads the label exactly, would take the issue for a medium one."""
    sandbox.init_git()
    sandbox.set_state(labels=[{"name": "Size:S", "color": "000000", "description": ""}])
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "the label `Size:S` exists" in result.err
    assert sandbox.writes == []


def test_two_issues_with_one_marker_stop_the_publication(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.seed_issue(40, "one", "<!-- backlog-id: E1-01 -->\n")
    sandbox.seed_issue(41, "two", "<!-- backlog-id: E1-01 -->\n")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "both carry the marker of E1-01" in result.err
    assert sandbox.writes == []


def test_an_unmarked_issue_with_a_drafts_title_stops_the_publication(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.seed_issue(40, "Pin the CDK toolchain", "written by hand\n")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "E1-01: #40 already has this title and carries no backlog-id marker" in result.err
    assert sandbox.writes == []


def test_a_pull_request_is_not_an_issue(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.seed_issue(40, "Pin the CDK toolchain", "<!-- backlog-id: E1-01 -->\n", pull_request=True)
    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    assert len(sandbox.state["issues"]) == 7
    assert "E1-01: created #" in result.out


def _later_work(sandbox: Sandbox, epic: int, blocker: int) -> None:
    """A draft that joins an epic and waits for an issue which are both on GitHub already."""
    text = sandbox.read(TOOLCHAIN).replace("E1-01", "E2-01").replace("Pin the CDK toolchain", "Pin one more tool")
    sandbox.write("E2/01-later-work.md", text.replace('epic = "E1"', f"epic = {epic}\nblocked_by = [{blocker}]"))


def test_later_work_joins_what_is_already_on_github(sandbox: Sandbox) -> None:
    _later_work(sandbox, epic=40, blocker=41)
    sandbox.init_git()
    epic = sandbox.seed_issue(40, "An epic published long ago", "<!-- backlog-id: E2 -->\n")
    blocker = sandbox.seed_issue(41, "An issue published long ago")
    dry = sandbox.publish()
    assert "sub-issue of #40; blocked by #41" in dry.out
    assert sandbox.calls == []

    result = sandbox.publish("--apply")
    assert result.code == 0, result.text
    new = sandbox.issue("E2-01")
    assert sandbox.state["subs"]["40"] == [new["id"]]
    assert sandbox.state["blocked"][str(new["number"])] == [blocker["id"]]
    assert epic["id"] not in sandbox.state["blocked"][str(new["number"])]

    sandbox.forget_calls()
    assert sandbox.publish("--apply").code == 0
    assert sandbox.writes == []


def test_a_number_github_does_not_know_stops_the_publication(sandbox: Sandbox) -> None:
    _later_work(sandbox, epic=40, blocker=99)
    sandbox.init_git()
    sandbox.seed_issue(40, "An epic published long ago")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "a draft names #99, which GitHub does not give" in result.err
    assert sandbox.writes == []


def test_a_number_that_is_a_pull_request_stops_the_publication(sandbox: Sandbox) -> None:
    _later_work(sandbox, epic=40, blocker=41)
    sandbox.init_git()
    sandbox.seed_issue(40, "An epic published long ago")
    sandbox.seed_issue(41, "A pull request", pull_request=True)
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "a draft names #41, which is a pull request" in result.err
    assert sandbox.writes == []


def test_apply_refuses_a_repository_that_answers_under_another_name(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(full_name="example-owner/renamed")
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "GitHub answers for example-owner/renamed" in result.err
    assert sandbox.writes == []


def test_apply_refuses_a_repository_without_issues(sandbox: Sandbox) -> None:
    sandbox.init_git()
    sandbox.set_state(has_issues=False)
    result = sandbox.publish("--apply")
    assert result.code == 2
    assert "issues are disabled" in result.err
    assert sandbox.writes == []


def test_every_title_and_body_is_scanned_as_it_is_sent(sandbox: Sandbox) -> None:
    sandbox.init_git()
    assert sandbox.publish().code == 0
    sent = {Path(entry["path"]).name: entry["text"] for entry in sandbox.scanned()
            if not entry["path"].startswith("backlog/drafts/")}
    drafts = _loaded(sandbox)
    assert sorted(sent) == sorted(f"{draft_id}.md" for draft_id in drafts)
    for draft_id, draft in drafts.items():
        assert sent[f"{draft_id}.md"] == f"{draft.title}\n\n<!-- backlog-id: {draft_id} -->\n\n{draft.body}"


@pytest.mark.parametrize("apply", [False, True])
def test_a_finding_in_what_would_be_sent_stops_the_publication(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch,
                                                                apply: bool) -> None:
    """Only what is sent holds the marker, so a scanner that objects to it tells the two scans apart."""
    sandbox.init_git()
    monkeypatch.setenv("FAKE_SCAN_WORD", "backlog-id")
    result = sandbox.publish("--apply") if apply else sandbox.publish()
    assert result.code == 1
    assert "hygiene: what would be sent for E1-01.md:3: fake-rule: [redacted]" in result.out
    assert str(sandbox.tmp) not in result.text
    assert sandbox.calls == []


def test_writes_are_spaced_out_and_reads_are_not(sandbox: Sandbox) -> None:
    pauses: list[float] = []
    gh = backlog.Gh(SLUG, pause_s=1.5, sleep=pauses.append)
    gh.get("labels")
    for name in ("a", "b", "c"):
        gh.api("POST", "labels", {"name": name, "color": "ededed", "description": ""})
        gh.get("labels")
    assert pauses == [1.5, 1.5], "a pause before each write but the first, none before a read"


def test_a_refused_call_is_an_error_with_githubs_answer(sandbox: Sandbox) -> None:
    gh = backlog.Gh(SLUG, pause_s=0)
    with pytest.raises(backlog.GhError, match=r"gh api GET repos/example-owner/example-repo/issues/99 failed: .*HTTP 404"):
        gh.get("issues/99")


# ============================================================================================
# The documentation the planner follows, and the repository as it stands
# ============================================================================================

def test_the_readme_examples_are_the_linted_examples() -> None:
    """backlog/README.md shows two of the example drafts. They are the files the lint tests keep
    clean, so the format the planner copies is one the lint accepts."""
    readme = (REPO / "backlog" / "README.md").read_text(encoding="utf-8")
    examples = dict(re.findall(r"<!-- example: (\S+) -->\n+````markdown\n(.*?)\n````", readme, flags=re.DOTALL))
    assert sorted(examples) == sorted([EPIC, APP])
    for name, text in examples.items():
        expected = (HERE / "fixtures" / "good" / "backlog" / "drafts" / name).read_text(encoding="utf-8")
        assert text + "\n" == expected, f"the example {name} in backlog/README.md differs from the fixture"


@pytest.mark.parametrize("document", ["backlog/README.md", ".claude/skills/backlog/SKILL.md"])
def test_the_documents_name_every_label_and_capability(document: str) -> None:
    text = (REPO / document).read_text(encoding="utf-8")
    for label in backlog.LABELS:
        assert f"`{label}`" in text, f"{document} does not name the label {label}"
    for capability in backlog.host_lacks(REPO):
        assert f"`{capability}`" in text, f"{document} does not name the capability {capability} of loop.toml"


def test_the_template_in_the_skill_passes_the_lint(sandbox: Sandbox) -> None:
    skill = (REPO / ".claude" / "skills" / "backlog" / "SKILL.md").read_text(encoding="utf-8")
    template = re.search(r"```markdown\n(\+\+\+\n.*?)\n```", skill, flags=re.DOTALL)
    assert template, "the skill shows a draft in a ```markdown block"
    sandbox.write(APP, template.group(1) + "\n")
    result = sandbox.run("lint")
    assert result.code == 0, result.text


def test_the_skill_is_the_same_for_both_agents() -> None:
    claude = (REPO / ".claude" / "skills" / "backlog" / "SKILL.md").read_bytes()
    codex = (REPO / ".agents" / "skills" / "backlog" / "SKILL.md").read_bytes()
    assert claude == codex
    assert claude.startswith(b"---\nname: backlog\n")


REAL_SCANNER = REPO / "tools" / "hygiene" / "scan.py"
needs_the_real_scanner = pytest.mark.skipif(not REAL_SCANNER.is_file(), reason="tools/hygiene/scan.py is not there")


@needs_the_real_scanner
def test_everything_the_backlog_workstream_wrote_passes_the_real_hygiene_scanner() -> None:
    proc = subprocess.run([sys.executable, str(REAL_SCANNER), "--paths", "tools/backlog", "backlog",
                           ".claude/skills/backlog", ".agents/skills/backlog", "scripts/backlog.sh"],
                          cwd=REPO, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_the_real_scanner
def test_the_example_drafts_pass_with_the_real_scanner_and_the_real_loop_toml(sandbox: Sandbox) -> None:
    """The examples against this repository's own gate and capabilities, not the stand-ins."""
    shutil.rmtree(sandbox.root / "tools" / "hygiene")
    shutil.copytree(REAL_SCANNER.parent, sandbox.root / "tools" / "hygiene",
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    shutil.copy(REPO / "tools" / "implementation-loop" / "loop.toml", sandbox.root / "tools" / "implementation-loop")
    result = sandbox.run("lint")
    assert result.code == 0, result.text
    assert sandbox.publish().code == 0
