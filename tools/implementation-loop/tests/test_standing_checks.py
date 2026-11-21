"""The standing checks (ADR 0001, decision 4): the public-repository hygiene scan runs on every
head the verifier verifies, before the issue's own commands. A finding goes back to the builder
with the scanner's output, a tree without the scanner fails closed, and a branch that carries a
leak is never examined and never pushed.

The rest of the suite runs with the standing checks off (`helpers.py`): its throwaway
repositories hold no scanner. Here they are on, with this repository's real scanner."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO_ROOT, STANDING_CHECKS, git, identify
from implement_loop import config, sandbox, verify
from implement_loop.agents import FakeAgents
from implement_loop.engine import Engine
from implement_loop.model import CheckSpec, Phase
from implement_loop.workspace import GitError
from test_engine import EngineCase

SCANNER = REPO_ROOT / "tools" / "hygiene"
# An account ID that is not one of the documentation placeholders, never written out in a file.
ACCOUNT = "9" * 4 + "0" * 4 + "7" * 4
LEAK = f"owner {ACCOUNT}"


def add_scanner(tree: Path) -> None:
    target = tree / "tools" / "hygiene"
    target.mkdir(parents=True)
    for name in ("scan.py", "hygiene.toml"):
        shutil.copyfile(SCANNER / name, target / name)


class TheEnginesOwnListTests(unittest.TestCase):
    def test_the_standing_check_is_the_hygiene_scan(self):
        self.assertEqual(STANDING_CHECKS, (("hygiene", "python3 tools/hygiene/scan.py"),))
        self.assertTrue((SCANNER / "scan.py").is_file(), "the command names a file this repository has")

    def test_the_engine_reads_the_list_when_it_verifies(self):
        self.assertEqual(config.standing_checks(), (), "off for the suite")
        with mock.patch.object(config, "STANDING_CHECKS", STANDING_CHECKS):
            self.assertEqual(config.standing_checks(), STANDING_CHECKS)


class StandingCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-standing-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "tree"
        git("init", "-q", "-b", "main", str(self.root), cwd=self.tmp)
        identify(self.root)
        add_scanner(self.root)
        (self.root / "README.md").write_text("clean\n")
        self.commit("seed")
        self.own = CheckSpec(criterion="D1", command="echo own")

    def commit(self, message: str) -> str:
        git("add", "-A", cwd=self.root)
        git("commit", "-q", "-m", message, cwd=self.root)
        return git("rev-parse", "HEAD", cwd=self.root)

    def verify(self, standing=STANDING_CHECKS):
        head = git("rev-parse", "HEAD", cwd=self.root)
        return verify.verify(self.root, [self.own], head, self.tmp / "work", standing=standing)

    def test_a_clean_head_passes_and_the_scan_is_recorded_first(self):
        ok, evidence, problems = self.verify()
        self.assertTrue(ok, problems)
        self.assertEqual([e["criterion"] for e in evidence], ["standing:hygiene", "D1"], "before the issue's own commands")
        scan = evidence[0]
        self.assertEqual((scan["command"], scan["cwd"], scan["exit_code"], scan["passed"]),
                         ("python3 tools/hygiene/scan.py", ".", 0, True))
        self.assertEqual(scan["head_sha"], git("rev-parse", "HEAD", cwd=self.root), "bound to the exact head")

    def test_a_leak_is_a_verification_problem_that_carries_the_scanners_output(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "notes.md").write_text(f"The account is {ACCOUNT}.\n")
        self.commit("a leak")
        ok, evidence, problems = self.verify()
        self.assertFalse(ok)
        [problem] = problems
        self.assertTrue(problem.startswith("standing check `hygiene` failed (exit 1)"), problem)
        self.assertIn("docs/notes.md:1: account-id", problem, "where, and which rule: enough to fix it")
        self.assertNotIn(ACCOUNT, problem, "the scanner's excerpt is redacted, so the value is not passed on")
        self.assertEqual([(e["criterion"], e["passed"]) for e in evidence], [("standing:hygiene", False), ("D1", True)],
                         "the issue's own commands still run: one round tells the builder everything")

    def test_a_file_git_would_add_is_scanned_too(self):
        (self.root / "new.txt").write_text(f"{ACCOUNT}\n")
        ok, _, problems = self.verify()
        self.assertFalse(ok)
        self.assertIn("new.txt:1: account-id", problems[0])

    def test_a_tree_without_the_scanner_fails_closed(self):
        shutil.rmtree(self.root / "tools")
        self.commit("the scanner is gone")
        ok, evidence, problems = self.verify()
        self.assertFalse(ok)
        self.assertTrue(problems[0].startswith("standing check `hygiene` failed"), problems[0])
        self.assertIn("scan.py", problems[0])
        self.assertFalse(evidence[0]["passed"])

    def test_a_scanner_that_cannot_read_the_tree_is_a_problem_never_an_exception(self):
        plain = self.tmp / "not-a-repository"
        add_scanner(plain)
        with mock.patch.dict("os.environ", {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            try:
                ok, _, problems = verify.verify(plain, [], "a" * 40, self.tmp / "work", standing=STANDING_CHECKS)
            except GitError as e:   # pragma: no cover - the failure this test exists to rule out
                self.fail(f"the standing check surfaced as a GitError: {e}")
        self.assertFalse(ok)
        self.assertTrue(problems[0].startswith("standing check `hygiene` failed"), problems[0])

    def test_a_usage_error_of_the_command_fails_as_well(self):
        ok, _, problems = self.verify(standing=(("hygiene", "python3 tools/hygiene/scan.py --no-such-flag"),))
        self.assertFalse(ok)
        self.assertIn("failed (exit 2)", problems[0])

    def test_without_standing_checks_only_the_issues_commands_run(self):
        (self.root / "new.txt").write_text(f"{ACCOUNT}\n")
        ok, evidence, _ = self.verify(standing=())
        self.assertTrue(ok, "the verifier's default")
        self.assertEqual([e["criterion"] for e in evidence], ["D1"])


class LeakAgents(FakeAgents):
    """Builds what the issue asks for, and the first `leaks` times also a note with an account ID."""

    def __init__(self, scripts, leaks: int):
        super().__init__(scripts)
        self.leaks = leaks

    def build(self, vendor, req, plan, worktree, feedback):
        result = super().build(vendor, req, plan, worktree, feedback)
        note = worktree / "docs" / "a.txt"
        if self._built.get(req.issue.number, 0) <= self.leaks:
            note.write_text(note.read_text() + f"deployed to {ACCOUNT}\n")
        return result


class EngineStandingCheckTests(EngineCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "STANDING_CHECKS", STANDING_CHECKS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_scanner(self) -> None:
        seed = self.repo.tmp / "seed"
        add_scanner(seed)
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "the hygiene scanner", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)

    def work(self, **script) -> None:
        self.add_work(2, "docs/a.txt", "alpha", size="S", **script)
        self.epic(2)

    def branches(self) -> str:
        return git("ls-remote", "--heads", "origin", cwd=self.repo.work)

    def test_a_clean_issue_lands_with_the_scan_in_its_evidence(self):
        self.seed_scanner()
        self.work()
        summary = self.run_engine()
        self.assertEqual(summary["phases"], {"done": 1})
        verified = self.events("verified", 2)
        self.assertTrue(verified and all(e["ok"] for e in verified))
        scans = [e for e in self.store.issue(2).evidence if e["criterion"] == "standing:hygiene"]
        self.assertEqual([(e["passed"], e["command"]) for e in scans], [(True, "python3 tools/hygiene/scan.py")])
        self.assertFalse(self.events("build_not_committed", 2))

    def history(self) -> str:
        """Every commit any ref of the repository holds, with its changes."""
        return git("log", "--all", "-p", "--format=%H %s", cwd=self.repo.work)

    def test_a_change_the_scan_refuses_is_never_committed_examined_or_pushed(self):
        self.seed_scanner()
        self.work()
        agents = LeakAgents(self.scripts, leaks=99)
        self.run_engine(agents=agents)
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
        refused = self.events("build_not_committed", 2)
        self.assertEqual(len(refused), len([c for c in agents.calls if c[0] == "build"]), "every build was refused")
        for event in refused:
            self.assertTrue(any(p.startswith("standing check `hygiene` failed") and "docs/a.txt:2: account-id" in p
                                for p in event["problems"]), event["problems"])
        self.assertFalse(self.events("built", 2), "nothing was committed")
        self.assertNotIn(ACCOUNT, self.history(), "no commit of any branch carries the value")
        self.assertFalse(self.events("verified", 2))
        self.assertFalse([c for c in agents.calls if c[0] == "examine"], "the examiner never saw it")
        self.assertEqual(self.gh.prs, {}, "no pull request")
        self.assertNotIn("feat/issue-2", self.branches(), "the branch never reached the remote")
        self.assertFalse(self.events("step_crashed"), "the builder's problem, never an engine fault or a GitError")

    def test_the_builder_is_told_what_the_scanner_found_and_can_fix_it(self):
        self.seed_scanner()
        self.work()
        agents = LeakAgents(self.scripts, leaks=1)
        summary = self.run_engine(agents=agents)
        self.assertEqual(summary["phases"], {"done": 1})
        builds = [c for c in agents.calls if c[0] == "build" and c[2] == 2]
        self.assertEqual(len(builds), 2)
        feedback = "\n".join(builds[1][3])
        self.assertIn("standing check `hygiene` failed", feedback)
        self.assertIn("docs/a.txt:2: account-id", feedback)
        self.assertNotIn(ACCOUNT, feedback)
        self.assertEqual(len(self.events("build_not_committed", 2)), 1)
        self.assertTrue(all(e["ok"] for e in self.events("verified", 2)), "only the fixed work was ever a head")
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha")
        self.assertNotIn(ACCOUNT, self.history(), "the first attempt is in no commit, on any branch")

    def test_refused_work_is_not_checkpointed_when_main_moves_under_it(self):
        self.seed_scanner()
        self.work()
        repo = self.repo

        class MovingMain(LeakAgents):
            def build(self, vendor, req, plan, worktree, feedback):
                result = super().build(vendor, req, plan, worktree, feedback)
                if self._built[req.issue.number] == 1:   # someone else lands on main during the first build
                    seed = repo.tmp / "seed"
                    (seed / "other.txt").write_text("landed meanwhile\n")
                    git("add", "other.txt", cwd=seed)
                    git("commit", "-q", "-m", "another change", cwd=seed)
                    git("push", "-q", "origin", "HEAD:main", cwd=seed)
                return result
        summary = self.run_engine(agents=MovingMain(self.scripts, leaks=1))
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(len(self.events("update_postponed", 2)), 1, "main was not merged over work that cannot be committed")
        self.assertFalse(self.events("checkpoint_before_update", 2), "and that work was not checkpointed")
        self.assertTrue(self.events("updated_from_main", 2), "main was merged in once the work was clean")
        self.assertNotIn(ACCOUNT, self.history())
        self.assertEqual((self.repo.main_file("docs/a.txt"), self.repo.main_file("other.txt")), ("alpha", "landed meanwhile"))

    def test_a_head_that_fails_the_scan_is_stopped_by_the_verifier(self):
        # the second layer: content that reached a commit by another way than a build (a merge of
        # main, an adopted probe, an approved test change) is scanned on the exact head
        self.seed_scanner()
        self.work()
        agents = LeakAgents(self.scripts, leaks=99)
        # as in a run (cli.cmd_run): the engine's own git takes no hook from the repository, so its
        # commit is not what stops the change; without that, this repository's pre-commit hook
        # refuses the commit, and a refused commit is an engine fault instead of a builder's round
        pinned = sandbox.engine_git_env(os.environ, self.repo.tmp / "no-hooks")
        with mock.patch.dict(os.environ, pinned), mock.patch.object(Engine, "_uncommittable", lambda *a: []):
            self.run_engine(agents=agents)
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
        verified = self.events("verified", 2)
        self.assertTrue(verified and not any(e["ok"] for e in verified))
        for event in verified:
            self.assertTrue(any(p.startswith("standing check `hygiene` failed") and "docs/a.txt:2: account-id" in p
                                for p in event["problems"]), event["problems"])
        self.assertFalse([c for c in agents.calls if c[0] == "examine"])
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.branches(), "a head that fails the scan is never pushed")
        self.assertFalse(self.events("step_crashed"))

    def test_a_repository_without_the_scanner_delivers_nothing(self):
        self.work()   # no scanner on main: the scan fails closed, before any commit
        agents = FakeAgents(self.scripts)
        self.run_engine(agents=agents)
        self.assertEqual(self.phase(2), Phase.FAILED)
        refused = self.events("build_not_committed", 2)
        self.assertTrue(refused and all("standing check `hygiene` failed" in "\n".join(e["problems"]) for e in refused))
        self.assertFalse([c for c in agents.calls if c[0] == "examine"])
        self.assertEqual(self.gh.prs, {})

    def test_a_revert_passes_the_standing_checks_before_it_is_pushed(self):
        self.seed_scanner()
        self.add_work(2, "docs/a.txt", "alpha")
        self.add_work(3, "docs/a.txt", "broken", blocked_by=(2,))
        self.epic(2, 3)
        self.failing_deployments(3)
        ran = []
        real = verify.standing_checks

        def recorded(root, checks, head_sha, work_dir, *args, **kwargs):
            evidence, problems = real(root, checks, head_sha, work_dir, *args, **kwargs)
            ran.append((Path(work_dir).name, head_sha, list(checks), problems))
            return evidence, problems
        with mock.patch.object(verify, "standing_checks", recorded):
            self.run_engine()
        reverted = self.events("reverted", 3)
        self.assertTrue(reverted, "the breaking merge was reverted")
        head = self.gh.prs[reverted[0]["pr"]]["head"]["sha"]
        on_revert = [r for r in ran if r[0] == "revert" and r[1] == head]
        self.assertEqual([(r[2], r[3]) for r in on_revert], [(list(STANDING_CHECKS), [])],
                         "the revert's exact head was scanned, and passed")
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha")

    def test_a_revert_that_fails_the_standing_checks_is_not_pushed(self):
        self.seed_scanner()
        self.add_work(2, "docs/a.txt", "alpha")
        self.add_work(3, "docs/a.txt", "broken", blocked_by=(2,))
        self.epic(2, 3)
        self.failing_deployments(3)
        real = verify.standing_checks

        def failing_on_the_revert(root, checks, head_sha, work_dir, *args, **kwargs):
            if Path(work_dir).name == "revert":
                return [], ["standing check `hygiene` failed (exit 1): a finding"]
            return real(root, checks, head_sha, work_dir, *args, **kwargs)
        with mock.patch.object(verify, "standing_checks", failing_on_the_revert):
            self.run_engine()
        st = self.store.issue(3)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "main_broken"))
        self.assertIn("the revert's head fails the standing checks", (st.fail or {}).get("detail", ""))
        self.assertFalse([p for p in self.gh.prs.values() if p["head"]["ref"].endswith("-revert")])
        self.assertNotIn("-revert", self.branches())
        self.assertFalse(self.events("reverted", 3))


if __name__ == "__main__":
    unittest.main()
