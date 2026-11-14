"""The history checks (ADR 0001, decision 4, and its `history` job in CI): a value that was
committed and removed again passes a scan of the tree and stays in the branch. The engine scans
the commits a branch adds to main on every head it verifies and again before every push. A
finding there ends the issue `failed`, on a branch that is never pushed, and the next attempt
starts on a new branch: no round of the builder could take a commit back.

Before that, nothing an agent wrote is committed without the standing checks: the builder's
rounds (test_standing_checks.py) and, here, the examiner's probes.

Like the standing checks, the history checks are off for the rest of the suite (`helpers.py`).

Last, the owner's own look (issue #16): no public pattern can name the real domain, so the owner
keeps such values in a list outside the repository, which does not exist in the sandbox. The
engine, which runs outside it, scans the head and its commits with that list immediately before
each push, with the scanner of its own checkout, and keeps of a finding the path and the rule."""

import argparse
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import HISTORY_CHECKS, REPO_ROOT, SHIP, STANDING_CHECKS, git, issue_body, present
from implement_loop import cli, config, sandbox
from implement_loop import engine as engine_module
from implement_loop.agents import FakeAgents, Probe
from implement_loop.engine import Engine, history_findings, history_places
from implement_loop.model import IssueState, Phase
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)
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
        # and no list of the owner's own values, whatever the machine that runs the suite holds
        unlisted = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.repo.tmp / "no-config")})
        unlisted.start()
        self.addCleanup(unlisted.stop)
        os.environ.pop("HYGIENE_LOCAL_VALUES", None)
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


# ---------------------------------------------------------------- the owner's list (issue #16)

# A value of the owner's list and the line a builder wrote it on, put together at run time.
VALUE = "-".join(("quartz", "lantern", "meridian"))
LINE = f"zephyr harbour {VALUE} velvet anchor"
NOTE = "docs/note.txt"
NOTED = f"{NOTE}: local-value"   # all the engine keeps of the finding
SECRET = (VALUE, "zephyr harbour", "velvet anchor", "[redacted")   # the value, its line, the scanner's excerpt
SCANNER = REPO_ROOT / "tools" / "hygiene"   # of the checkout the engine under test runs from


class TheScanWithTheOwnersListTests(unittest.TestCase):
    def test_its_commands_are_the_engines_own_scanner_over_the_head_and_its_commits(self):
        self.assertEqual(L.config.LOCAL_VALUE_CHECKS, (("local-values", ("--rev", "{head}")),
                                                       ("local-values-history", ("--history", "{base}..{head}"))))
        start = (sys.executable, "-I", str(SCANNER / "scan.py"), "--config", str(SCANNER / "hygiene.toml"))
        with mock.patch.object(L.config, "HISTORY_CHECKS", HISTORY_CHECKS):
            self.assertEqual(L.config.local_value_checks("aaa", "bbb"),
                             (("local-values", (*start, "--rev", "bbb")),
                              ("local-values-history", (*start, "--history", "aaa..bbb"))))
        with mock.patch.object(L.config, "HISTORY_CHECKS", ()):
            self.assertEqual(L.config.local_value_checks("aaa", "bbb"), (), "off with the history checks, as for the suite")

    def test_the_list_is_where_the_scanner_looks_for_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(L.config.local_values_file({"XDG_CONFIG_HOME": tmp}))
            default = Path(tmp) / "research-harness-inference" / "hygiene-values"
            default.parent.mkdir()
            default.write_text("")
            self.assertEqual(L.config.local_values_file({"XDG_CONFIG_HOME": tmp}), default)
            named = str(Path(tmp) / "not-there")   # a file named that way must exist: the scanner says so, not the engine
            self.assertEqual(L.config.local_values_file({"XDG_CONFIG_HOME": tmp, "HYGIENE_LOCAL_VALUES": named}), Path(named))

    def test_of_a_finding_the_engine_keeps_the_path_and_the_rule(self):
        output = (f"{NOTE}:1: local-value: zephyr harbour [redacted local-value] velvet anchor\n"
                  f"{NOTE}@{'ab12' * 3}:7: local-value: zephyr harbour [redacted local-value] velvet anchor\n"
                  "<commit>@ff7ab7681631:1: local-value: Author: A <[redacted local-value]>\n"
                  "checks/x.sh:0: account-id: in the file name\n")
        self.assertEqual(L.engine.local_places(output), [NOTED, "<commit>: local-value", "checks/x.sh: account-id"])
        self.assertEqual(L.engine.local_places("hygiene: 3 files scanned, 1 findings\n"), [])


class Inside(L.sandbox.PassThrough):
    """A launch as the sandbox makes it, in the one respect these tests are about: the owner's
    list does not exist in there. A marker tells a command that it was started this way."""

    def __init__(self, nothing: Path):
        self.nothing = nothing

    def command(self, argv, cwd, vendor=None, mounts=None):
        return ["env", "-u", "HYGIENE_LOCAL_VALUES", f"XDG_CONFIG_HOME={self.nothing}", "LOOP_LAUNCHED=inside",
                *super().command(argv, cwd, vendor, mounts)]


class OwnersValueAgents(L.agents.FakeAgents):
    """Builds what the issue asks for and, in its first `rounds` builds, a note with a value of
    the owner's list on it. Keeps everything an agent is given, for a test to search."""

    def __init__(self, scripts, rounds: int):
        super().__init__(scripts)
        self.rounds = rounds
        self.given: list[str] = []

    def plan(self, vendor, req, worktree):
        result = super().plan(vendor, req, worktree)
        result.zones.append(NOTE)
        return result

    def build(self, vendor, req, plan, worktree, feedback):
        self.given.append(repr((req, plan, feedback)))
        result = super().build(vendor, req, plan, worktree, feedback)
        if self._built[req.issue.number] <= self.rounds:
            (worktree / NOTE).write_text(LINE + "\n")
        else:
            (worktree / NOTE).unlink(missing_ok=True)
        return result

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        self.given.append(repr((req, plan, evidence, memory, requests)))
        return super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)


class OwnersListCase(HistoryCase):
    """The repository and the checks of `HistoryCase`, a list of the owner's values outside the
    sandbox, and the engine beside these tests (`L`): in an xdist worker under the verifier,
    `implement_loop` is the engine that runs the loop, which has no such scan while this is built."""

    def setUp(self):
        super().setUp()
        self.gh = L.github.FakeGitHub(on_merge=self.repo.merge)
        self.values = self.repo.tmp / "owner" / "hygiene-values"
        self.values.parent.mkdir()
        self.values.write_text(f"# what no public pattern can name\n{VALUE}\n")
        self.outside: list[tuple[str, ...]] = []   # every command the engine started outside the sandbox
        real = L.engine.Engine._outside

        def recorded(engine, argv, env):
            self.outside.append(tuple(argv))
            return real(engine, argv, env)
        for patcher in (mock.patch.object(L.config, "STANDING_CHECKS", STANDING_CHECKS),
                        mock.patch.object(L.config, "HISTORY_CHECKS", HISTORY_CHECKS),
                        mock.patch.dict(os.environ, {"HYGIENE_LOCAL_VALUES": str(self.values)}),
                        mock.patch.object(L.engine.Engine, "_outside", recorded)):
            patcher.start()
            self.addCleanup(patcher.stop)
        L.sandbox.use(Inside(self.repo.tmp / "no-config"))
        self.addCleanup(L.sandbox.use, L.sandbox.PassThrough())
        self.logged: list[str] = []

    def issue(self, n: int, content: str, blocked_by=(), **script) -> None:
        body = issue_body(f"feat/issue-{n}", [f"`docs/a.txt` contains `{content}`."], ["The test passes on main."])
        self.gh.add_issue(n, f"Issue {n}", body, labels=("size:S",), blocked_by=blocked_by)
        self.scripts[n] = L.agents.Script(files={"docs/a.txt": content}, **script)

    def work(self, **script) -> None:
        self.issue(2, "alpha", **script)
        self.epic(2)

    def run_engine(self, agents=None, caps=None, fresh=False, policy=None):
        self.agents = agents or L.agents.FakeAgents(self.scripts)
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC, fresh=fresh)
        engine = L.engine.Engine(
            L.graph.build_plan(self.gh, EPIC, self.repo.work), self.store, self.gh,
            L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir), self.agents,
            L.config.Config(repo="test/test", caps=caps or L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=self.logged.append, sleep=lambda s: None, policy=policy or self.policy)
        return engine.run()

    def retry(self) -> str:
        cli_beside = importlib.import_module(f"{L.engine.__package__}.cli")
        with mock.patch.object(cli_beside, "repo_root", return_value=self.repo.work), redirect_stdout(io.StringIO()) as out:
            cli_beside.cmd_retry(argparse.Namespace(issue=EPIC, items=[]))
        return out.getvalue()

    def ended(self, n: int = 2) -> tuple[str, str | None]:
        st = self.store.issue(n)
        return st.phase.value, (st.fail or {}).get("cause")

    def kept(self) -> str:
        """Everything the engine sent to GitHub, gave an agent, logged or wrote into the run's records."""
        gh = self.gh
        files = [p.read_text(errors="replace") for p in sorted(self.store.dir.rglob("*")) if p.is_file()]
        return "\n".join([json.dumps([gh.issues, gh.comments, gh.prs, gh.statuses, gh.calls], default=repr),
                          *getattr(self.agents, "given", []), *self.logged, *files])

    def hooks(self) -> list[Path]:
        return list((self.repo.tmp / "no-hooks").iterdir())


class OwnersListTests(OwnersListCase):
    def test_a_value_in_the_tree_of_the_head_ends_the_issue_and_only_its_place_is_passed_on(self):
        self.work()
        agents = OwnersValueAgents(self.scripts, rounds=1)
        self.run_engine(agents)
        st = self.store.issue(2)
        self.assertEqual(self.ended(), ("failed", "history"))
        [refused] = self.events("history_refused", 2)
        self.assertEqual((refused["branch"], refused["findings"], refused["output"]), ("feat/issue-2", [NOTED], []))
        self.assertEqual(git("show", f"{refused['head']}:{NOTE}", cwd=self.repo.work), LINE, "in the tree of that head")
        self.assertTrue([c for c in agents.calls if c[0] == "examine"], "which every check in the sandbox passed")
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.remote_branches(), "nothing of it was pushed")
        self.assertNotIn(VALUE, self.published())
        self.assertEqual((st.branch, st.round, st.worktree), ("feat/issue-2-r2", 2, None))
        self.assertTrue(git("branch", "--list", "feat/issue-2", cwd=self.repo.work), "the abandoned branch stays, locally")
        [fault] = [i for i in self.gh.issues.values() if {"name": "loop:engine-bug"} in i["labels"]]
        detail = (st.fail or {}).get("detail", "")
        self.assertIn("the scan with the owner's local values", detail)
        for text in (detail, fault["body"], st.feedback[0]):
            self.assertIn(NOTED, text)
        for secret in SECRET:
            self.assertNotIn(secret, self.kept())
        self.assertEqual(self.hooks(), [])

        self.assertIn("#2 gets a fresh ceiling", self.retry())
        summary = self.run_engine(agents)
        self.assertEqual(summary["phases"], {"done": 1})
        pr = self.gh.prs[present(self.store.issue(2).pr)]
        self.assertEqual((pr["head"]["ref"], pr["merged"]), ("feat/issue-2-r2", True), "the retry built on a new branch")
        told = next(c for c in agents.calls[::-1] if c[0] == "build")[3]
        self.assertTrue(any("`feat/issue-2`, was abandoned" in f and NOTED in f for f in told), told)
        self.assertNotIn("feat/issue-2\n", self.remote_branches() + "\n", "the abandoned branch was never pushed")
        self.assertNotIn(VALUE, self.published())
        for secret in SECRET:
            self.assertNotIn(secret, self.kept())

    def test_a_value_only_in_an_earlier_commit_of_the_branch_ends_the_issue_as_well(self):
        self.work(wrong_attempts=1)   # the second build repairs the file and takes the note away
        agents = OwnersValueAgents(self.scripts, rounds=1)
        self.run_engine(agents)
        self.assertEqual(self.ended(), ("failed", "history"))
        [refused] = self.events("history_refused", 2)
        self.assertEqual(refused["findings"], [NOTED])
        self.assertEqual(len([c for c in agents.calls if c[0] == "build"]), 2)
        self.assertNotIn(NOTE, git("ls-tree", "-r", "--name-only", refused["head"], cwd=self.repo.work), "the head is clean")
        self.assertIn(VALUE, git("log", "-p", "feat/issue-2", cwd=self.repo.work), "its history is not")
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.remote_branches())
        self.assertNotIn(VALUE, self.published())
        self.assertEqual(self.store.issue(2).branch, "feat/issue-2-r2")

    def test_a_branch_whose_own_scanner_accepts_everything_is_still_refused(self):
        ran = self.repo.tmp / "branch-scanner.log"   # the copy notes each of its runs, and where it was started
        seed = self.repo.tmp / "seed"
        (seed / "tools" / "hygiene" / "scan.py").write_text(
            f"import os\nwith open({str(ran)!r}, 'a') as f:\n    f.write(os.environ.get('LOOP_LAUNCHED', 'outside') + '\\n')\n")
        git("commit", "-q", "-am", "a scanner that accepts everything", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.work()
        self.run_engine(OwnersValueAgents(self.scripts, rounds=1))
        self.assertEqual(self.ended(), ("failed", "history"))
        self.assertEqual(self.events("history_refused", 2)[0]["findings"], [NOTED])
        self.assertEqual(set(ran.read_text().split()), {"inside"}, "the branch's copy ran, and only in the sandbox")
        self.assertEqual(len(self.outside), 2)
        for argv in self.outside:
            self.assertEqual(argv[1:5], ("-I", str(SCANNER / "scan.py"), "--config", str(SCANNER / "hygiene.toml")))
            self.assertFalse([a for a in argv if str(self.repo.tmp) in a], "no file of the branch or of its repository")
        self.assertNotIn("feat/issue-2", self.remote_branches())

    def test_with_no_list_nothing_runs_outside_the_sandbox_and_the_push_goes_ahead(self):
        del os.environ["HYGIENE_LOCAL_VALUES"]   # and the configuration directory holds none (`HistoryCase`)
        self.work()
        summary = self.run_engine(OwnersValueAgents(self.scripts, rounds=1))
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(self.outside, [])
        self.assertFalse(self.events("history_refused") + self.events("push_withheld"))
        self.assertEqual(self.repo.main_file(NOTE), LINE, "what only the owner's list could have stopped")
        self.assertEqual(self.hooks(), [], "and git was given no hook")

    def test_a_list_that_cannot_be_read_withholds_the_push(self):
        self.values.write_bytes(b"\xff\xfe\xff")   # not text: no user can read it as a list
        self.work()
        self.run_engine()
        self.assertEqual(self.ended(), ("failed", "no_progress"))
        withheld = self.events("push_withheld", 2)
        self.assertTrue(withheld)
        for event in withheld:
            self.assertEqual(len(event["problems"]), 2, "the tree and the commits: neither was scanned")
            self.assertTrue(all("could not run (exit 2)" in p for p in event["problems"]), event["problems"])
        self.assertTrue(self.outside)
        self.assertFalse(self.events("history_refused"), "no finding: the branch is not abandoned")
        self.assertEqual(self.gh.prs, {})
        self.assertNotIn("feat/issue-2", self.remote_branches(), "a head the owner's list was not held against is not pushed")

    def test_without_a_hooks_directory_of_its_own_the_engine_scans_nothing_and_pushes_nothing(self):
        # the engine's git is pinned in every run (`cli.cmd_run`); were it not, a scanned push could not be guarded
        self.work()
        with mock.patch.object(L.engine.Engine, "_own_hooks", lambda engine, wt: None):
            self.run_engine()
        self.assertEqual(self.ended(), ("failed", "no_progress"))
        self.assertTrue(self.events("push_withheld", 2))
        self.assertEqual((self.outside, self.gh.prs), ([], {}))
        self.assertNotIn("feat/issue-2", self.remote_branches())

    def test_a_head_that_shipping_made_after_the_scan_is_not_pushed_before_it_is_scanned(self):
        # ship.sh fetches main again before it pushes. Here main moves between the scan and that
        # fetch, so shipping rebases the branch: a head and commits the scan never saw.
        self.work()
        quick = self.repo.tmp / "bin"   # ship.sh waits between its attempts to push
        quick.mkdir()
        (quick / "sleep").write_text("#!/bin/sh\n")
        (quick / "sleep").chmod(0o755)
        scans, pushes = [], []
        scan, push = L.engine.Engine._local_scan, L.engine.Engine._push

        def scan_then_main_moves(engine, wt, base, head):
            result = scan(engine, wt, base, head)
            scans.append((head, result))
            if len(scans) == 1:
                seed = self.repo.tmp / "seed"
                (seed / "elsewhere.txt").write_text("another change lands\n")
                git("add", "-A", cwd=seed)
                git("commit", "-q", "-m", "main moves", cwd=seed)
                git("push", "-q", "origin", "HEAD:main", cwd=seed)
            return result

        def push_and_look(engine, wt, scanned):
            pushed = push(engine, wt, scanned)
            pushes.append((scanned, pushed, self.remote_branches()))
            return pushed
        with mock.patch.dict(os.environ, {"PATH": f"{quick}{os.pathsep}{os.environ['PATH']}"}), \
                mock.patch.object(L.engine.Engine, "_local_scan", scan_then_main_moves), \
                mock.patch.object(L.engine.Engine, "_push", push_and_look):
            summary = self.run_engine()
        (first, pushed, remote), (second, again, _) = pushes
        self.assertEqual((pushed, again), (False, True))
        self.assertNotIn("feat/issue-2", remote, "nothing left the machine the first time")
        [withheld] = self.events("push_withheld", 2)
        self.assertEqual(withheld["head"], first)
        self.assertNotIn(withheld["shipped"], [first, None], "shipping had made another head")
        self.assertEqual(summary["phases"], {"done": 1})
        head = self.gh.prs[present(self.store.issue(2).pr)]["head"]["sha"]
        self.assertEqual((second, scans[-1]), (head, (head, ([], []))), "the head that was pushed is one the scan passed")
        self.assertNotEqual(head, first)
        self.assertEqual(self.hooks(), [], "the guard of a push does not outlive it")


class OwnersListRevertTests(OwnersListCase):
    def test_a_revert_with_a_value_of_the_owners_list_is_not_pushed(self):
        # The words git puts into a revert commit's message are on the list: no earlier push holds them.
        self.values.write_text("This reverts commit\n")
        self.issue(2, "alpha")
        self.issue(3, "broken", blocked_by=(2,))   # breaks #2's check on main
        self.epic(2, 3)
        self.run_engine()
        st = self.store.issue(3)
        self.assertEqual(self.ended(3), ("failed", "main_broken"))
        detail = (st.fail or {}).get("detail", "")
        self.assertIn("the revert fails the scan with the owner's local values, so it is not pushed: <commit>: local-value", detail)
        self.assertNotIn("[redacted", detail)
        self.assertNotIn("-revert", self.remote_branches())
        self.assertFalse(self.events("reverted", 3))


if __name__ == "__main__":
    unittest.main()
