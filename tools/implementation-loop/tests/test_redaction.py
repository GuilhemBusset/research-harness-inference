"""Everything the engine publishes on GitHub is masked first by the hygiene scanner of the checkout
the engine runs from (ADR 0001, decision 4): `redact.py`, `github.RedactingGitHub`, and the client
`cli.py` builds. Without the scanner nothing is published and the issue waits with the kind github.

The engine runs are real (git, the verifier, squash merges into a local remote); the agents and
GitHub are fakes, and the scanner is this repository's. The account number and the ARN are put
together at run time, so no line of this file holds one."""

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import REPO_ROOT, SHIP, TempRepo, git, issue_body, present
from implement_loop import config as loaded

EPIC = 1
LACKS = ["hardware:laptop"]
PARTS = ("4711", "0815", "0042")
ACCOUNT = "".join(PARTS)
ARN = f"arn:aws:iam::{ACCOUNT}:role/deploy"
LEAK = f"deployed to account {ACCOUNT} as {ARN}"
T = unittest.TestCase()


def loop_beside_these_tests() -> SimpleNamespace:
    """The modules of the `implement_loop` beside these tests, which is the one they judge. The
    verifier's pytest plugin belongs to the engine that runs the loop, and loading it imports that
    engine's `implement_loop`. It drops the package again only in the pytest that records, not in
    an xdist worker, so the tests of `scripts/check.sh engine-tests` would find there the running
    engine: one without `redact.py`, while this change is built. There, the package beside these
    tests is loaded whole under another name, with the two defaults `helpers.py` sets."""
    mine = Path(__file__).resolve().parent.parent / "implement_loop"
    name = beside = "implement_loop"
    if Path(present(loaded.__file__)).resolve().parent != mine:
        name = "implement_loop_beside_test_redaction"
        spec = present(importlib.util.spec_from_file_location(name, mine / "__init__.py",
                                                              submodule_search_locations=[str(mine)]))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        present(spec.loader).exec_module(sys.modules[name])
    modules = SimpleNamespace(**{m: importlib.import_module(f"{name}.{m}") for m in (
        "agents", "cli", "config", "engine", "github", "graph", "model", "redact", "report", "sandbox", "state",
        "workspace")})
    if name != beside:
        modules.sandbox.use(modules.sandbox.PassThrough())
        modules.config.STANDING_CHECKS = modules.config.HISTORY_CHECKS = ()
    return modules


L = loop_beside_these_tests()
Phase, WAIT_GITHUB = L.model.Phase, L.model.WAIT_GITHUB


def masked() -> str:
    """`LEAK` as the scanner masks it. A text that quotes `LEAK` holds this once it is masked."""
    text = L.redact.redact(LEAK)
    assert ACCOUNT not in text and text.startswith("deployed to account "), text
    return text


def failing_on(refused):
    """A scanner that fails on the texts `refused` picks, and masks the others."""
    def mask(text: str) -> str:
        if refused(text):
            raise L.redact.RedactionError("the hygiene scanner failed (in this test)")
        return L.redact.redact(text)
    return mask


def says(phase):
    """Picks the progress comment that announces `phase`."""
    return lambda text: (text.startswith(L.report.PROGRESS_MARKER)
                         and text.splitlines()[1].endswith(": " + L.report.PHASE_TEXT[phase]))


def published(fake) -> list[str]:
    """Every title and body the fake GitHub holds."""
    return ([c["body"] for comments in fake.comments.values() for c in comments]
            + [text for pr in fake.prs.values() for text in (pr["title"], pr["body"])]
            + [text for issue in fake.issues.values() for text in (issue["title"], issue["body"])])


class Leaky(L.agents.FakeAgents):
    """Agents whose output carries the account and the ARN: a decision of the plan (the pull request
    body), the output of the builder's test (the evidence comment), what the examiner observed (the
    follow-up issue) and, for the issues in `crashing`, the error of a build (the fault issue)."""
    crashing: tuple[int, ...] = ()
    scanner_in_worktree = False

    def _test_script(self, n):   # its output carries what no file holds, as a synthesized template's can
        return super()._test_script(n) + f"a=$(printf %s {' '.join(PARTS)})\necho \"{LEAK.replace(ACCOUNT, '$a')}\"\n"

    def plan(self, vendor, req, worktree):
        result = super().plan(vendor, req, worktree)
        result.decisions.append({"id": "target", "choice": LEAK, "rationale": "read in a synthesized template"})
        return result

    def build(self, vendor, req, plan, worktree, feedback):
        self.scanner_in_worktree = (worktree / "tools" / "hygiene" / "scan.py").is_file()
        if req.issue.number in self.crashing:
            raise L.agents.AgentFailure(f"{vendor} build: {LEAK}", kind="crash", vendor=vendor)
        return super().build(vendor, req, plan, worktree, feedback)

    def examine(self, *args):
        result = super().examine(*args)
        for finding in result.findings:
            finding.observed = LEAK
        return result


class Loop:
    """A throwaway repository, a fake GitHub, and engine runs that reach it as a real run does:
    through the redacting client."""

    def __init__(self):
        self.repo = TempRepo()
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        self.scripts = {}

    def add_work(self, n, items=(), **script):
        path = f"docs/{n}.txt"
        body = issue_body(f"feat/issue-{n}", [f"`{path}` contains `text`.", *items], ["The test passes on main."])
        self.fake.add_issue(n, f"Issue {n}", body, labels=("size:M",))
        self.scripts[n] = L.agents.Script(files={path: "text"}, **script)
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=tuple(self.scripts))

    def run(self, mask=None, crashing=(), **caps):
        gh = L.github.RedactingGitHub(self.fake, mask) if mask else L.github.RedactingGitHub(self.fake)
        self.agents = Leaky(self.scripts)
        self.agents.crashing = crashing
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        engine = L.engine.Engine(
            L.graph.build_plan(gh, EPIC, self.repo.work), self.store, gh,
            L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir), self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0, **caps)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None, policy={"lacks": LACKS})
        engine.run()

    def state(self, n):
        """(phase, kind of wait, phase it resumes in, state on GitHub)"""
        st = self.store.issue(n)
        wait = st.wait or {}
        return st.phase, wait.get("kind"), wait.get("resume"), self.fake.issues[n]["state"]

    def labelled(self, label) -> list[str]:
        return [i["body"] for i in self.fake.issues.values() if {"name": label} in i["labels"]]

    def evidence(self) -> list[str]:
        return [text for text in published(self.fake) if L.report.EVIDENCE_MARKER in text]


@contextmanager
def loop():
    run = Loop()
    try:
        yield run
    finally:
        run.repo.cleanup()


# ---------------------------------------------------------------------- redact.py and the client
def test_redact_asks_the_scanner_beside_the_engine_and_never_returns_the_text_unmasked():
    assert L.redact.SCANNER == REPO_ROOT / "tools" / "hygiene" / "scan.py"
    assert "[" in masked()
    with tempfile.TemporaryDirectory() as tmp:
        scanner = Path(tmp) / "scan.py"
        for failing in (None, "import sys\nsys.stdout.write(sys.stdin.read())\nsys.exit(2)\n"):   # missing, then failing
            if failing:
                scanner.write_text(failing)
            with T.assertRaises(L.redact.RedactionError) as raised:
                L.redact.redact(LEAK, scanner=scanner)
            assert ACCOUNT not in str(raised.exception)


def test_the_client_masks_what_it_sends_and_sends_nothing_it_could_not_mask():
    fake = L.github.FakeGitHub()
    fake.add_issue(7, "An issue", "its body")
    gh = L.github.RedactingGitHub(fake)
    pr = gh.create_pr("branch", "main", f"title: {LEAK}", "body")
    gh.update_pr(pr["number"], LEAK)
    gh.update_comment(gh.create_comment(7, "body")["id"], LEAK)
    gh.create_issue(f"title: {LEAK}", "body", [])
    texts = published(fake)
    assert not [t for t in texts if ACCOUNT in t]
    assert len([t for t in texts if masked() in t]) == 4

    before = (list(fake.calls), published(fake))
    gh = L.github.RedactingGitHub(fake, failing_on(lambda text: True))
    for send in (lambda: gh.create_comment(7, LEAK), lambda: gh.update_comment(1001, LEAK), lambda: gh.update_pr(5001, LEAK),
                 lambda: gh.create_pr("other", "main", "title", LEAK), lambda: gh.create_issue("title", LEAK, [])):
        with T.assertRaises(L.github.GitHubUnavailable):   # what the engine turns into the wait github
            send()
    assert (list(fake.calls), published(fake)) == before
    ticked = f"- [x] the owner's own words: {LEAK}"
    gh.edit_issue(7, body=ticked)
    assert fake.issues[7]["body"] == ticked, "a tick sends the issue's own text back, as written"


def test_the_run_command_publishes_through_the_redacting_client():
    sent, given = [], []

    class Gh:   # stands for GhCli
        def __init__(self, slug):
            pass

        def create_comment(self, n, body):
            sent.append(body)

    def plan(gh, issue, root):
        given.append(gh)
        raise L.graph.CycleError("far enough")

    args = SimpleNamespace(operator="claude", repo="test/test", issue=EPIC)
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ), T.assertRaises(SystemExit), \
            mock.patch.multiple(L.cli, GhCli=Gh, build_plan=plan, preflight=lambda root, slug: None,
                                repo_root=lambda: Path(tmp), runs_dir=lambda root: Path(tmp)):
        L.cli.cmd_run(args)
    given[0].create_comment(2, LEAK)
    assert sent == [masked()]


# ---------------------------------------------------------------------- the engine
def test_what_the_engine_publishes_reaches_github_masked():
    with loop() as run:
        run.add_work(2, unquoted_findings=1)   # lands, with a finding that becomes a follow-up issue
        run.add_work(3)                        # its build crashes: not delivered, filed as a fault issue
        run.run(crashing=(3,), infra_budget=1)
        assert (run.state(2)[0], run.state(3)[0]) == (Phase.DONE, Phase.FAILED)
        places = {"a comment": run.evidence(), "a pull request body": [pr["body"] for pr in run.fake.prs.values()],
                  "a fault issue": run.labelled("loop:engine-bug"), "a follow-up issue": run.labelled("loop:follow-up")}
        for place, texts in places.items():
            assert len(texts) == 1 and masked() in texts[0], place
        assert not [text for text in published(run.fake) if ACCOUNT in text]


def test_no_scanner_no_publication():
    with loop() as run:
        run.add_work(2)
        before = published(run.fake)   # the epic and the issue, as their author wrote them
        with mock.patch.object(L.redact, "SCANNER", run.repo.tmp / "tools" / "hygiene" / "scan.py"):   # not there
            run.run()
        assert run.state(2)[:2] == (Phase.WAITING, WAIT_GITHUB)
        assert published(run.fake) == before
        assert not [call for call in run.fake.calls if call[0].startswith(("create_", "update_"))]
        assert "- [x]" not in run.fake.issues[2]["body"] and run.fake.issues[2]["state"] == "open"
        run.run()   # the scanner is back: the next run resumes the issue, as the supervisor's would
        assert run.state(2) == (Phase.DONE, None, None, "closed")
        assert masked() in run.evidence()[0]


def test_the_worktree_scanner_is_never_run():
    with loop() as run:
        trace = run.repo.tmp / "the-worktree-scanner-ran"
        scanner = run.repo.merger / "tools" / "hygiene" / "scan.py"   # on main, so in the issue's worktree
        scanner.parent.mkdir(parents=True)
        scanner.write_text(f"import sys\nopen({str(trace)!r}, 'a').write('ran')\nsys.stdout.write(sys.stdin.read())\n")
        git("add", "tools", cwd=run.repo.merger)
        git("commit", "-q", "-m", "a scanner that masks nothing", cwd=run.repo.merger)
        git("push", "-q", "origin", "HEAD:main", cwd=run.repo.merger)
        git("pull", "-q", cwd=run.repo.work)   # and in the repository the engine is given
        run.add_work(2)
        run.run()
        assert run.agents.scanner_in_worktree and run.state(2)[0] == Phase.DONE
        assert masked() in run.evidence()[0]
        assert not [text for text in published(run.fake) if ACCOUNT in text]
        assert not trace.exists()


def test_a_publication_whose_failure_the_engine_would_swallow_makes_the_issue_wait():
    """`Engine._safe` journals a GitHub failure and goes on: the evidence comment, the follow-up
    issue, the comment about deferred items and the progress comment are written that way. A text
    the scanner cannot mask is not one of those failures: the issue stays open and waits in the
    phase that publishes it again."""
    deferring = {"items": ["It plays in the widget."], "deferred": (("D2", 9),)}
    for refused, resume, script, end in (
            (lambda text: L.report.EVIDENCE_MARKER in text, "land", {}, (Phase.DONE, "closed")),
            (lambda text: text.startswith("Follow-up to #2"), "merged", {}, (Phase.DONE, "closed")),
            (says(Phase.DONE), "merged", {}, (Phase.DONE, "closed")),
            (lambda text: text.startswith(L.report.DEFERRED_MARKER), "merged", deferring, (Phase.DELIVERED, "open"))):
        with loop() as run:
            run.fake.add_issue(9, "Prerequisite widget", "The Prerequisite widget the demo plays in.\n")
            run.add_work(2, unquoted_findings=1, **script)
            run.run(mask=failing_on(refused))
            assert run.state(2) == (Phase.WAITING, WAIT_GITHUB, resume, "open")
            assert not [text for text in published(run.fake) if refused(text) or says(end[0])(text)]
            run.run()
            assert run.state(2) == (end[0], None, None, end[1])
            assert len(run.labelled("loop:follow-up")) == 1, "filed once, in whichever run could"


def test_a_progress_comment_that_cannot_be_masked_stops_the_issue_at_its_next_step():
    with loop() as run:
        run.add_work(2)
        run.run(mask=failing_on(lambda text: text.startswith(L.report.PROGRESS_MARKER)))
        assert run.state(2) == (Phase.WAITING, WAIT_GITHUB, "planned", "open")
        assert not [call for call in run.agents.calls if call[0] == "build"]


def test_an_issue_the_owner_ticked_stays_delivered_until_its_last_comment_is_written():
    """It is not closed, and it waits: left delivered, the supervisor would count it settled and stop."""
    with loop() as run:
        run.add_work(2, items=["Runs under a second on a 2020 laptop."], owner_items=("D2",))
        run.run()
        assert run.state(2)[0] == Phase.DELIVERED
        run.fake.issues[2]["body"] = run.fake.issues[2]["body"].replace("- [ ]", "- [x]")
        run.run(mask=failing_on(says(Phase.DONE)))
        assert run.state(2) == (Phase.WAITING, WAIT_GITHUB, "delivered", "open")
        run.run()
        assert run.state(2) == (Phase.DONE, None, None, "closed")


def test_a_failed_issue_waits_until_its_fault_issue_and_its_last_comment_are_written():
    """A failed issue is settled for the supervisor too. It waits as it failed, and is failed again,
    for the same cause and without being worked again, once the next run has published."""
    for refused in (lambda text: text.startswith("implement loop: #2 not delivered"), says(Phase.FAILED)):
        with loop() as run:
            run.add_work(2)
            run.run(mask=failing_on(refused), crashing=(2,), infra_budget=1)
            assert run.state(2) == (Phase.WAITING, WAIT_GITHUB, "failed", "open")
            assert not [text for text in published(run.fake) if refused(text)]
            run.run()
            assert run.state(2) == (Phase.FAILED, None, None, "open") and run.store.issue(2).fail["cause"] == "ceiling"
            assert not [call for call in run.agents.calls if call[0] == "build"]
            assert len(run.labelled("loop:engine-bug")) == 1 and masked() in run.labelled("loop:engine-bug")[0]
            assert [text for text in published(run.fake) if says(Phase.FAILED)(text)]
