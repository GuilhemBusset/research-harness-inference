"""The history checks (ADR 0001, decision 4, and its `history` job in CI): a value that was
committed and removed again passes a scan of the tree and stays in the branch. The engine scans
the commits a branch adds to main on every head it verifies and again before every push. A
finding there ends the issue `failed`, on a branch that is never pushed, and the next attempt
starts on a new branch: no round of the builder could take a commit back.

Before that, nothing an agent wrote is committed without the standing checks: the builder's
rounds (test_standing_checks.py) and, here, the examiner's probes.

Like the standing checks, the history checks are off for the rest of the suite (`helpers.py`)."""

import argparse
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import HISTORY_CHECKS, STANDING_CHECKS, git, present
from implement_loop import cli, config, sandbox
from implement_loop import engine as engine_module
from implement_loop.agents import FakeAgents, Probe
from implement_loop.engine import Engine, history_findings, history_places
from implement_loop.model import IssueState, Phase
from test_engine import EPIC, EngineCase
from test_standing_checks import ACCOUNT, LeakAgents, add_scanner

FINDING = f"docs/a.txt@{'ab12' * 10}:2: account-id: deployed to [redacted account-id]\n"


class TheEnginesOwnListTests(unittest.TestCase):
    def test_the_history_check_is_the_scanner_over_the_commits_of_a_range(self):
        self.assertEqual(HISTORY_CHECKS, (("hygiene-history", "python3 tools/hygiene/scan.py --history {range}"),))
        self.assertEqual(config.history_checks("aaa..bbb"), (), "off for the suite")
        with mock.patch.object(config, "HISTORY_CHECKS", HISTORY_CHECKS):
            self.assertEqual(config.history_checks("aaa..bbb"),
                             (("hygiene-history", "python3 tools/hygiene/scan.py --history aaa..bbb"),))

    def test_only_an_exit_status_that_means_findings_abandons_a_branch(self):
        evidence = [{"criterion": "standing:hygiene", "exit_code": 1, "output_tail": "the tree"},
                    {"criterion": "standing:hygiene-history", "exit_code": 2, "output_tail": "could not run"},
                    {"criterion": "D1", "exit_code": 1, "output_tail": "a test"}]
        with mock.patch.object(config, "HISTORY_CHECKS", HISTORY_CHECKS):
            self.assertEqual(history_findings(evidence), [])
            evidence.append({"criterion": "standing:hygiene-history", "exit_code": 1, "output_tail": FINDING})
            self.assertEqual(history_findings(evidence), [FINDING])

    def test_what_is_published_names_the_place_and_the_rule_never_the_excerpt(self):
        output = FINDING + "<commit>@ff7ab7681631:1: email: Author: A <[redacted email]>\n" + FINDING + "hygiene: 2 findings\n"
        self.assertEqual(history_places([output]), ["docs/a.txt@ab12ab12ab12:2: account-id", "<commit>@ff7ab7681631:1: email"])
        self.assertIn("history_refused", history_places(["no line of that shape"])[0])


class HistoryCase(EngineCase):
    """Both kinds of checks on, with this repository's scanner on main, and the engine's git
    pinned as in a run (`cli.cmd_run`): it takes no hook from the repository, so a commit of
    its own is not what stops a change."""

    def setUp(self):
        super().setUp()
        for name, value in (("STANDING_CHECKS", STANDING_CHECKS), ("HISTORY_CHECKS", HISTORY_CHECKS)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pinned = mock.patch.dict(os.environ, sandbox.engine_git_env(os.environ, self.repo.tmp / "no-hooks"))
        pinned.start()
        self.addCleanup(pinned.stop)
        seed = self.repo.tmp / "seed"
        add_scanner(seed)
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "the hygiene scanner", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)

    def work(self, **script) -> None:
        self.add_work(2, "docs/a.txt", "alpha", size="S", **script)
        self.epic(2)

    def remote_branches(self) -> str:
        return git("ls-remote", "--heads", "origin", cwd=self.repo.work)

    def published(self) -> str:
        """Every commit the remote holds, with its changes."""
        git("fetch", "-q", "origin", cwd=self.repo.merger)
        return git("log", "--remotes", "-p", "--format=%H %an <%ae> %s", cwd=self.repo.merger)

    def scans(self):
        """Record every call of the engine's history scan: (where, base, head, findings, problems)."""
        calls = []
        real = Engine._history_scan

        def recorded(engine, n, wt, base, head, where):
            findings, problems = real(engine, n, wt, base, head, where)
            calls.append((where, base, head, findings, problems))
            return findings, problems
        patcher = mock.patch.object(Engine, "_history_scan", recorded)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def retry(self) -> str:
        out = io.StringIO()
        with mock.patch.object(cli, "repo_root", return_value=self.repo.work), redirect_stdout(out):
            cli.cmd_retry(argparse.Namespace(issue=EPIC, items=[]))
        return out.getvalue()


class EngineHistoryTests(HistoryCase):
    def test_a_clean_issue_lands_with_its_history_scanned_at_every_head_and_before_the_push(self):
        scans = self.scans()
        self.work()
        summary = self.run_engine()
        self.assertEqual(summary["phases"], {"done": 1})
        pr = self.gh.prs[present(self.store.issue(2).pr)]
        head = pr["head"]["sha"]
        verified = [e for e in self.events("verified", 2) if e["ok"]]
        self.assertTrue(verified)
        self.assertEqual([(where, h, findings, problems) for where, _, h, findings, problems in scans], [("push", head, [], [])],
                         "one last scan, of the exact head that was pushed")
        base = scans[0][1]
        self.assertEqual(len(base), 40)
        self.assertTrue(git("merge-base", "--is-ancestor", base, head, cwd=self.repo.merger) == "", "main, as the branch has it")
        self.assertFalse(self.events("history_refused") + self.events("push_withheld"))

    def test_the_history_scan_is_part_of_the_evidence_of_a_verified_head(self):
        self.work()
        seen = []
        real = Engine._step_examine

        def examine(engine, n):
            seen.append([(e["criterion"], e["passed"], e["command"]) for e in engine.store.issue(n).evidence
                         if e["criterion"].startswith("standing:")])
            return real(engine, n)
        with mock.patch.object(Engine, "_step_examine", examine):
            self.run_engine()
        self.assertEqual([c for c, _, _ in seen[0]], ["standing:hygiene", "standing:hygiene-history"])
        self.assertTrue(all(passed for _, passed, _ in seen[0]))
        command = seen[0][1][2]
        self.assertRegex(command, r"^python3 tools/hygiene/scan\.py --history [0-9a-f]{40}\.\.[0-9a-f]{40}$")

    def test_a_value_that_reached_a_commit_ends_the_issue_failed_without_another_round(self):
        self.work()
        agents = LeakAgents(self.scripts, leaks=1)
        # the first layer off: what the scan before each commit would have refused gets committed
        with mock.patch.object(Engine, "_uncommittable", lambda *a: []):
            self.run_engine(agents=agents)
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "history"))
        self.assertEqual(len([c for c in agents.calls if c[0] == "build"]), 1, "the builder is not asked to repair a commit")
        self.assertFalse([c for c in agents.calls if c[0] == "examine"])
        self.assertFalse(self.events("verified", 2), "no verdict on a head that can never be pushed")
        self.assertFalse(self.events("round_failed", 2))
        [refused] = self.events("history_refused", 2)
        self.assertEqual(refused["branch"], "feat/issue-2")
        self.assertTrue(any(f.startswith("docs/a.txt@") and f.endswith(":2: account-id") for f in refused["findings"]),
                        refused["findings"])
        self.assertEqual((st.branch, st.round, st.restarts, st.worktree, st.pr), ("feat/issue-2-r2", 2, 1, None, None))
        self.assertTrue(st.plan, "the plan is kept")
        self.assertEqual((st.locked, st.tests, st.probes, st.verified_sha, st.certified_sha), ({}, [], [], None, None))
        detail = (st.fail or {}).get("detail", "")
        self.assertIn("the commits of `feat/issue-2` carry what the history scan refuses", detail)
        self.assertIn("builds on a new branch from main, `feat/issue-2-r2`", detail)
        self.assertIn("account-id", detail)
        self.assertIn("was abandoned", st.feedback[0])
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.remote_branches(), "nothing of it was pushed")
        self.assertNotIn(ACCOUNT, self.published())
        self.assertTrue(git("branch", "--list", "feat/issue-2", cwd=self.repo.work), "the abandoned branch stays, locally")
        self.assertFalse(self.events("step_crashed"), "not an engine fault")
        [fault] = [i for i in self.gh.issues.values() if {"name": "loop:engine-bug"} in i["labels"]]
        everything = json.dumps([self.store.events(), st.to_dict(), self.gh.issues, self.gh.comments])
        self.assertNotIn(ACCOUNT, everything, "the scanner's excerpts are redacted, and what is published has none")
        self.assertIn("**history**", fault["body"])
        self.assertIn("docs/a.txt@", fault["body"])
        self.assertNotIn("[redacted", fault["body"], "the place and the rule, not the line around the value")

    def test_a_value_committed_and_then_removed_is_stopped_before_the_push(self):
        # The head's tree is clean and an examiner certified it: only its history carries the value.
        # The verifier's own history check is off here, as for a head an older engine verified,
        # so the scan before the push is what stands between the branch and the remote.
        self.work()
        scans = self.scans()
        real = Engine._step_verify

        def verify_without_history(engine, n):
            with mock.patch.object(config, "HISTORY_CHECKS", ()):
                return real(engine, n)
        agents = LeakAgents(self.scripts, leaks=1)
        with mock.patch.object(Engine, "_uncommittable", lambda *a: []), \
                mock.patch.object(Engine, "_step_verify", verify_without_history):
            self.run_engine(agents=agents)
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "history"))
        self.assertEqual(len([c for c in agents.calls if c[0] == "build"]), 2, "the second build removed the value")
        self.assertTrue([c for c in agents.calls if c[0] == "examine"], "and the clean tree was examined and certified")
        [(where, _, head, findings, _)] = scans
        self.assertEqual(where, "push")
        self.assertEqual(git("show", f"{head}:docs/a.txt", cwd=self.repo.work), "alpha", "the tree of that head is clean")
        self.assertIn("account-id", findings[0])
        self.assertIn(ACCOUNT, git("log", "-p", "feat/issue-2", cwd=self.repo.work), "its history is not")
        self.assertEqual(self.events("history_refused", 2)[0]["head"], head)
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.remote_branches())
        self.assertNotIn(ACCOUNT, self.published())

    def test_a_retry_builds_on_a_new_branch_from_main_and_lands(self):
        self.work()
        with mock.patch.object(Engine, "_uncommittable", lambda *a: []):
            self.run_engine(agents=LeakAgents(self.scripts, leaks=1))
        self.assertEqual(self.phase(2), Phase.FAILED)
        self.assertIn("#2 gets a fresh ceiling", self.retry())
        agents = FakeAgents(self.scripts)
        summary = self.run_engine(agents=agents)
        self.assertEqual(summary["phases"], {"done": 1})
        st = self.store.issue(2)
        pr = self.gh.prs[present(st.pr)]
        self.assertEqual((pr["head"]["ref"], pr["merged"]), ("feat/issue-2-r2", True))
        first_build = [c for c in agents.calls if c[0] == "build"][0]
        self.assertTrue(any("`feat/issue-2`, was abandoned" in f and "account-id" in f for f in first_build[3]),
                        "the builder is told why it starts again")
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha")
        self.assertNotIn(ACCOUNT, self.published(), "main and the branch that landed never held the value")
        self.assertNotIn("feat/issue-2\n", self.remote_branches() + "\n", "the abandoned branch was never pushed")
        self.assertEqual(st.restarts, 0, "an accepted round ends the count")

    def test_a_history_check_that_cannot_run_is_a_failed_check_like_any_other(self):
        broken = (("hygiene-history", "python3 tools/hygiene/scan.py --history {range} --no-such-flag"),)
        self.work()
        with mock.patch.object(config, "HISTORY_CHECKS", broken):
            self.run_engine()
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
        self.assertFalse(self.events("history_refused"), "no finding: the branch is not abandoned")
        self.assertEqual(st.branch, "feat/issue-2")
        problems = [p for e in self.events("verified", 2) for p in e["problems"]]
        self.assertTrue(problems and all(p.startswith("standing check `hygiene-history` failed (exit 2)") for p in problems),
                        problems)
        self.assertEqual(self.gh.prs, {}, "and a head whose history was not scanned is not pushed")

    def test_a_push_is_withheld_when_the_last_scan_cannot_run(self):
        self.work()
        real = Engine._history_scan
        failed = []

        def flaky(engine, n, wt, base, head, where):
            if not failed:
                failed.append(head)
                return [], ["standing check `hygiene-history` failed (timed out after 900 s)"]
            return real(engine, n, wt, base, head, where)
        with mock.patch.object(Engine, "_history_scan", flaky):
            summary = self.run_engine()
        [withheld] = self.events("push_withheld", 2)
        self.assertEqual(withheld["head"], failed[0])
        pushed_before = [e for e in self.events("phase", 2) if e["seq"] < withheld["seq"] and e["to"] == "merged"]
        self.assertFalse(pushed_before)
        self.assertEqual(summary["phases"], {"done": 1}, "a round later the scan ran, and the head was pushed")

    def test_a_last_scan_that_never_runs_ends_the_issue_instead_of_going_round(self):
        self.work()
        real = Engine._history_scan

        def never_at_the_push(engine, n, wt, base, head, where):
            if where == "push":
                return [], ["standing check `hygiene-history` failed (timed out after 900 s)"]
            return real(engine, n, wt, base, head, where)
        with mock.patch.object(Engine, "_history_scan", never_at_the_push):
            self.run_engine()
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
        self.assertGreaterEqual(len(self.events("push_withheld", 2)), 2)
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.remote_branches(), "an unscanned history is never pushed")


class RevertHistoryTests(HistoryCase):
    def breaking_pair(self) -> None:
        self.add_work(2, "docs/a.txt", "alpha")
        self.add_work(3, "docs/a.txt", "broken", blocked_by=(2,))   # breaks #2's check on main
        self.epic(2, 3)

    def test_the_commits_of_a_revert_are_scanned_before_it_is_pushed(self):
        self.breaking_pair()
        scans = self.scans()
        self.run_engine()
        reverted = self.events("reverted", 3)[0]
        head = self.gh.prs[reverted["pr"]]["head"]["sha"]
        on_revert = [s for s in scans if s[0] == "revert" and s[2] == head]
        self.assertEqual([(findings, problems) for _, _, _, findings, problems in on_revert], [([], [])])
        git("fetch", "-q", "origin", cwd=self.repo.merger)
        self.assertEqual(git("rev-list", "--count", f"{on_revert[0][1]}..{head}", cwd=self.repo.merger), "1",
                         "from main as it was: the revert commit itself")
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha")

    def test_a_revert_whose_commits_fail_the_scan_is_not_pushed(self):
        self.breaking_pair()
        real = Engine._history_scan

        def failing_on_the_revert(engine, n, wt, base, head, where):
            if where == "revert":
                return ["<commit>@ff7ab7681631:1: email: Author: A <[redacted email]>\n"], []
            return real(engine, n, wt, base, head, where)
        with mock.patch.object(Engine, "_history_scan", failing_on_the_revert):
            self.run_engine()
        st = self.store.issue(3)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "main_broken"))
        self.assertIn("the revert's commits fail the history checks", (st.fail or {}).get("detail", ""))
        self.assertIn("<commit>@ff7ab7681631:1: email", (st.fail or {}).get("detail", ""))
        self.assertNotIn("[redacted", (st.fail or {}).get("detail", ""))
        self.assertNotIn("-revert", self.remote_branches())
        self.assertFalse(self.events("reverted", 3))

    def test_a_revert_looks_past_a_round_that_was_abandoned_unmerged(self):
        # #3 merges and breaks main; its fix-forward round is abandoned for its history; the retry's
        # round repairs nothing either. The revert must still find the pull request that broke main,
        # two rounds back, although only one fix-forward was counted.
        self.breaking_pair()
        real = Engine._step_verify
        abandoned = []

        def verify(engine, n):
            st = engine.store.issue(n)
            if n == 3 and st.round == 2 and not abandoned:
                abandoned.append(st.branch)
                return engine._history_refused(n, engine.ws.head(Path(present(st.worktree))), [FINDING])
            return real(engine, n)
        with mock.patch.object(Engine, "_step_verify", verify):
            self.run_engine()
            st = self.store.issue(3)
            self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "history"))
            self.assertEqual((abandoned, st.branch, st.fix_forwards, st.restarts), (["feat/issue-3-r2"], "feat/issue-3-r3", 1, 1))
            self.assertFalse(self.events("reverted", 3))
            self.retry()
            self.run_engine()
        reverted = self.events("reverted", 3)[0]
        broke = present(self.gh.find_pr("feat/issue-3"))
        self.assertEqual(reverted["of"], [broke["number"]], "the round-one merge, past the abandoned round two")
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha", "main works again")

    def test_an_owners_edit_forgets_the_abandoned_rounds(self):
        self.breaking_pair()
        engine = Engine.__new__(Engine)
        engine.store = mock.Mock()
        st = IssueState(number=3, fix_forwards=1, reverts=1, restarts=2)
        engine._owner_edit(st)
        self.assertEqual((st.fix_forwards, st.reverts, st.restarts), (0, 0, 0))


class DirtyProbeAgents(FakeAgents):
    """The examiner's first probe for D1 carries an account ID. Asked again, it writes a clean one."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.asked: list[list[str]] = []

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        self.asked.append(list(req.feedback))
        result = super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)
        first = self.probe_file(req.issue.number)
        if len(self.asked) == 1:
            result.probes = [Probe(p.file, p.criterion, p.command, p.cwd, p.content + f"# the account is {ACCOUNT}\n")
                             if p.file == first else p for p in result.probes]
        elif any("were not kept" in f for f in req.feedback):
            result.probes = [Probe(first, "D1", f"bash {first}", content="#!/usr/bin/env bash\ntest -s docs/a.txt\n")]
        return result


class ProbesAreScannedBeforeTheyAreCommittedTests(HistoryCase):
    def test_a_probe_the_scan_refuses_is_not_adopted_and_the_examiner_is_told_why(self):
        self.work()
        agents = DirtyProbeAgents(self.scripts)
        summary = self.run_engine(agents=agents)
        self.assertEqual(summary["phases"], {"done": 1})
        first, second = self.events("examined", 2)[:2]
        [rejected] = first["rejected"]
        self.assertEqual(rejected["file"], "checks/probe_2.sh")
        self.assertTrue(rejected["reason"].startswith("the standing checks refuse it: standing check `hygiene` failed"))
        self.assertIn("checks/probe_2.sh:4: account-id", rejected["reason"])
        self.assertEqual(first["adopted"], ["checks/probe_2_A1.sh"], "the clean probe of the same examination is kept")
        told = "\n".join(agents.asked[1])
        self.assertIn("These probes were not kept: `checks/probe_2.sh`", told)
        self.assertIn("account-id", told)
        self.assertNotIn(ACCOUNT, told)
        self.assertEqual(second["adopted"], ["checks/probe_2.sh"], "its clean replacement is adopted")
        self.assertFalse(self.events("history_refused"), "so the branch's history never held the value")
        self.assertNotIn(ACCOUNT, git("log", "--all", "-p", cwd=self.repo.work))
        self.assertNotIn(ACCOUNT, present(self.repo.main_file("checks/probe_2.sh")))

    def test_without_that_scan_the_probes_commit_would_have_ended_the_issue(self):
        # what the scan before adoption saves: the same examination, committed as it came
        self.work()
        agents = DirtyProbeAgents(self.scripts)
        with mock.patch.object(Engine, "_unclean_probes", lambda *a: {}):
            self.run_engine(agents=agents)
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "history"))
        self.assertTrue(any(f.startswith("checks/probe_2.sh@") for f in self.events("history_refused", 2)[0]["findings"]))
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn(ACCOUNT, self.published())

    def test_clean_probes_cost_one_scan_and_are_all_kept(self):
        self.work()
        calls = []
        real = engine_module.verify.standing_checks

        def recorded(root, checks, head_sha, work_dir, *args, **kwargs):
            calls.append(work_dir.name)
            return real(root, checks, head_sha, work_dir, *args, **kwargs)
        with mock.patch.object(engine_module.verify, "standing_checks", recorded):
            summary = self.run_engine()
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(calls.count("probes"), 1, "all of an examination's probes in one scan")
        self.assertEqual(self.events("examined", 2)[0]["adopted"], ["checks/probe_2.sh", "checks/probe_2_A1.sh"])


if __name__ == "__main__":
    unittest.main()
