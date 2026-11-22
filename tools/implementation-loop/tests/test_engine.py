"""End-to-end runs of the builder/examiner engine (ADR 0012): real git, the real ship.sh, the real
verifier and real squash merges into a local bare remote. Only the agents and GitHub are fakes."""

import re
import unittest
from concurrent.futures import Future
from unittest import mock

from helpers import SHIP, TempRepo, issue_body, present
from implement_loop.agents import FakeAgents, Script
from implement_loop.config import Caps, Config, Deploy
from implement_loop.engine import Engine
from implement_loop.github import FakeGitHub
from implement_loop.graph import build_plan
from implement_loop.model import WRITING_PHASES, Phase
from implement_loop.state import RepoClaims, RunStore
from implement_loop.workspace import Workspace

EPIC = 1
LACKS = ["hardware:laptop", "human:rehearsal"]


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.gh = FakeGitHub(on_merge=self.repo.merge)
        self.scripts: dict[int, Script] = {}
        self.policy = {"lacks": LACKS}

    def tearDown(self):
        self.repo.cleanup()

    def add_work(self, n, path, content, blocked_by=(), size="M", items=None, **script):
        deliverables = items or [f"`{path}` contains `{content}`."]
        self.gh.add_issue(n, f"Issue {n}", issue_body(f"feat/issue-{n}", deliverables, ["The test passes on main."]),
                          labels=(f"size:{size}",), blocked_by=blocked_by)
        self.scripts[n] = Script(files={path: content}, **script)

    def epic(self, *children):
        self.gh.add_issue(EPIC, "Epic", "An epic.\n", subs=children)

    def failing_deployments(self, n, context="deploy/test"):
        """The pipeline's verdict on each merge commit: the deployment of a merge of issue `n` fails,
        every other one (a revert's included) succeeds. A merge of `n` then takes the broken-`main`
        path: one fix-forward, then a revert."""
        merge = self.gh.on_merge
        assert merge is not None

        def deployed(branch, sha):
            commit = merge(branch, sha)
            broke = re.search(rf"issue-{n}(-r\d+)?$", branch) is not None
            self.gh.set_status(commit, context, "failure" if broke else "success")
            return commit
        self.gh.on_merge = deployed
        self.policy = {**self.policy, "deploy": Deploy(contexts=(context,))}

    def engine_for(self, agents=None, caps=None, fresh=False, policy=None):
        plan = build_plan(self.gh, EPIC, self.repo.work)
        store = RunStore.for_root(self.repo.work, EPIC, fresh=fresh)
        ws = Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir)
        self.agents = agents or FakeAgents(self.scripts)
        cfg = Config(repo="test/test", caps=caps or Caps(ci_poll_s=0, queue_poll_s=0))
        engine = Engine(plan, store, self.gh, ws, self.agents, cfg, repo_root=self.repo.work,
                        log=lambda m: None, sleep=lambda s: None, environment=getattr(self, "environment", None),
                        policy=policy or self.policy)
        self.engine, self.store = engine, store
        return engine

    def run_engine(self, agents=None, caps=None, fresh=False, policy=None):
        return self.engine_for(agents, caps, fresh, policy).run()

    def phase(self, n):
        return self.store.issue(n).phase

    def events(self, kind=None, issue=None):
        return [e for e in self.store.events() if (kind is None or e["kind"] == kind) and (issue is None or e.get("issue") == issue)]

    def roles(self, kind):
        return {c[1] for c in self.agents.calls if c[0] == kind}


class HappyPathTests(EngineCase):
    def test_dependent_issues_land_in_order_with_evidence(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.add_work(3, "docs/b.txt", "beta", blocked_by=(2,))
        self.epic(2, 3)
        summary = self.run_engine()

        self.assertEqual(summary["phases"], {"done": 2})
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha")
        self.assertEqual(self.repo.main_file("docs/b.txt"), "beta")
        self.assertTrue(present(self.repo.main_file("checks/issue_2.sh")).startswith("#!/usr/bin/env bash"))
        self.assertIsNone(self.repo.main_file("checks/probe_2.sh"), "a passing probe of an approved item is not kept")
        self.assertEqual([e["issue"] for e in self.events("merged")], [2, 3])
        first_b = min(e["seq"] for e in self.events("phase", 3) if e["to"] == "plan")
        a_done = min(e["seq"] for e in self.events("phase", 2) if e["to"] == "done")
        self.assertLess(a_done, first_b, "#3 waits until #2 is delivered")
        pr = self.gh.prs[present(self.store.issue(2).pr)]
        self.assertTrue(pr["merged"])
        self.assertTrue(pr["body"].startswith("Refs #2"))
        self.assertIn("## Decisions", pr["body"])
        self.assertIn("examined, probed and certified", pr["body"])
        body = self.gh.issues[2]["body"]
        self.assertIn("- [x] The test passes on main. ([evidence](", body)
        self.assertEqual(self.gh.issues[2]["state"], "closed")
        self.assertEqual(self.gh.issues[EPIC]["state"], "closed")

    def test_two_keys_one_per_vendor(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        self.run_engine()
        self.assertEqual(self.roles("plan"), {"claude"})
        self.assertEqual(self.roles("build"), {"claude"})
        self.assertEqual(self.roles("triage"), {"codex"})
        self.assertEqual(self.roles("examine"), {"codex"})
        smooth = [c for c in self.agents.calls if c[0] != "explore"]
        self.assertLessEqual(len(smooth), 5, f"a smooth issue costs about five calls: {smooth}")

    def test_operator_codex_swaps_every_role(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        self.run_engine(agents=FakeAgents(self.scripts, operator="codex", other="claude"))
        self.assertEqual((self.roles("build"), self.roles("examine")), ({"codex"}, {"claude"}))

    def test_independent_disjoint_issues_overlap_in_time(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.add_work(3, "docs/b.txt", "beta")
        self.epic(2, 3)
        self.run_engine(caps=Caps(ci_poll_s=0, queue_poll_s=0, max_parallel_writers=2))
        building = {n: [e for e in self.events("phase", n) if e["to"] == "build"][0]["seq"] for n in (2, 3)}
        merged = {e["issue"]: e["seq"] for e in self.events("merged")}
        self.assertLess(max(building.values()), min(merged.values()))
        self.assertEqual((self.phase(2), self.phase(3)), (Phase.DONE, Phase.DONE))

    def test_with_one_writer_one_issue_is_built_at_a_time(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.add_work(3, "docs/b.txt", "beta")
        self.epic(2, 3)
        self.run_engine(caps=Caps(ci_poll_s=0, queue_poll_s=0, max_parallel_writers=1))
        building = {n: [e for e in self.events("phase", n) if e["to"] == "build"][0]["seq"] for n in (2, 3)}
        merged = {e["issue"]: e["seq"] for e in self.events("merged")}
        first, second = sorted((2, 3), key=lambda n: building[n])
        self.assertLess(merged[first], building[second])
        self.assertEqual((self.phase(2), self.phase(3)), (Phase.DONE, Phase.DONE))

    def test_unquoted_triage_objections_are_dropped(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        self.run_engine()
        planned = self.events("planned", 2)[0]
        self.assertEqual((planned["objections"], planned["kept"]), (1, 0))


class RoundTests(EngineCase):
    def test_a_finding_tied_to_no_item_goes_to_a_follow_up_issue(self):
        self.add_work(2, "docs/a.txt", "alpha", unquoted_findings=1)
        self.epic(2)
        self.run_engine()
        self.assertEqual(self.phase(2), Phase.DONE)
        followup = self.store.issue(2).followup
        assert followup is not None
        self.assertIn({"name": "loop:follow-up"}, self.gh.issues[followup]["labels"])
        self.assertEqual(len([c for c in self.agents.calls if c[0] == "build"]), 1, "it never blocked")

    def test_a_probe_that_fails_unconditionally_is_rejected(self):
        self.add_work(2, "docs/a.txt", "alpha", probe="unconditional", findings=1)
        self.epic(2)
        agents = FakeAgents(self.scripts)
        examine = agents.examine

        def naming_its_probe(*args):   # only a probe a finding names is looked at
            result = examine(*args)
            for f in result.findings:
                f.probe = agents.probe_file(2)
            return result
        agents.examine = naming_its_probe  # ty: ignore[invalid-assignment]  # test double
        self.run_engine(agents=agents)
        self.assertEqual(self.phase(2), Phase.DONE)
        self.assertIsNone(self.repo.main_file("checks/probe_2.sh"))
        self.assertTrue(self.events("examined", 2)[0]["rejected"])

    def test_review_items_block_until_met(self):
        self.add_work(2, "docs/a.txt", "alpha", items=["`docs/a.txt` contains `alpha`.",
                                                        "The page reads clearly to a student."],
                      review_items=("D2",), unmet_review=1)
        self.epic(2)
        self.run_engine()
        self.assertEqual(self.phase(2), Phase.DONE)
        self.assertEqual(self.store.issue(2).verdicts["D2"]["verdict"], "met")
        self.assertIn("- [x] The page reads clearly", self.gh.issues[2]["body"])


class ObligationTests(EngineCase):
    def test_an_owner_claim_for_a_capability_the_host_has_is_demoted(self):
        self.add_work(2, "docs/a.txt", "alpha", items=["`docs/a.txt` contains `alpha`.", "It fetches the page online."],
                      bad_owner_items=("D2",))
        self.epic(2)
        self.run_engine()
        plan = present(self.store.issue(2).plan)
        self.assertEqual(next(i for i in plan["items"] if i["id"] == "D2")["proof"], "review")
        self.assertFalse(self.store.issue(2).obligations)

    def test_an_item_deferred_to_an_issue_outside_the_run_lands_the_rest(self):
        self.gh.add_issue(9, "Prerequisite widget", "The Prerequisite widget the demo plays in.\n")
        self.add_work(2, "docs/a.txt", "alpha", items=["`docs/a.txt` contains `alpha`.", "It plays in the widget."],
                      deferred=(("D2", 9),))
        self.epic(2)
        self.run_engine()
        self.assertEqual(self.phase(2), Phase.DELIVERED)
        ob = self.store.issue(2).obligations
        self.assertEqual((ob[0]["kind"], ob[0]["issue"]), ("deferred", 9))
        self.assertIn({"name": "loop:deferred"}, self.gh.issues[2]["labels"])
        self.assertTrue(any("implement-loop:deferred" in c["body"] for c in self.gh.comments[2]))


class WaitAndFailTests(EngineCase):
    def test_timeouts_spend_the_infrastructure_budget_then_fail(self):
        self.add_work(2, "docs/a.txt", "alpha", crashes=99)
        self.epic(2)
        self.run_engine(caps=Caps(ci_poll_s=0, queue_poll_s=0, infra_budget=3))
        self.assertEqual(self.phase(2), Phase.FAILED)
        self.assertEqual(present(self.store.issue(2).fail)["cause"], "ceiling")
        self.assertEqual(self.store.issue(2).infra, 3)

    def test_a_check_with_no_verdict_waits(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        self.gh.check_script = [None, None]
        self.run_engine(caps=Caps(ci_poll_s=1, ci_timeout_s=1, queue_poll_s=0))
        self.assertEqual(self.phase(2), Phase.WAITING)
        self.assertEqual(present(self.store.issue(2).wait)["kind"], "ci")

    def test_a_merge_queue_another_run_holds_is_polled_not_abandoned(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        claims = RepoClaims(self.repo.work)
        claims.acquire("merge-queue", "run-other")
        released = []

        def sleep(_):
            if not released:
                claims.release("merge-queue", "run-other")
                released.append(True)
        plan = build_plan(self.gh, EPIC, self.repo.work)
        store = RunStore.for_root(self.repo.work, EPIC)
        ws = Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir)
        self.agents = FakeAgents(self.scripts)
        Engine(plan, store, self.gh, ws, self.agents, Config(repo="t/t", caps=Caps(ci_poll_s=0, queue_poll_s=0)),
               repo_root=self.repo.work, log=lambda m: None, sleep=sleep, policy=self.policy).run()
        self.store = store
        self.assertEqual(self.phase(2), Phase.DONE)

    def test_an_issue_another_run_holds_waits(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        RepoClaims(self.repo.work).acquire("issue-2", "run-other")
        self.run_engine(caps=Caps(ci_poll_s=0, queue_poll_s=0, queue_wait_s=0))
        self.assertEqual(self.phase(2), Phase.WAITING)
        self.assertEqual(present(self.store.issue(2).wait)["kind"], "claim")


class RequirementTests(EngineCase):
    def test_criteria_edited_before_landing_send_the_issue_back_to_plan(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        original = self.gh.get_issue
        edited = []

        def get_issue(n):
            data = original(n)
            if n == 2 and not edited and any(e["kind"] == "examined" for e in self.store_events()):
                edited.append(True)
                self.gh.issues[2]["body"] = self.gh.issues[2]["body"].replace("The test passes on main.",
                                                                             "The test passes on main, twice.")
                data = original(n)
            return data
        self.store_events = lambda: RunStore.for_root(self.repo.work, EPIC).events()
        with mock.patch.object(self.gh, "get_issue", get_issue):
            self.run_engine()
        self.assertEqual(self.phase(2), Phase.DONE)
        self.assertTrue(self.events("requirements_changed", 2))


class RecordingPool:
    """The dispatcher's executor, as a log of what it submits: no step runs, so the test moves
    each issue on itself, one dispatch event at a time."""

    def __init__(self):
        self.submitted: list[tuple[str, int]] = []

    def submit(self, fn, n):
        self.submitted.append((fn.__name__.removeprefix("_step_"), n))
        return Future()


class DispatchTests(EngineCase):
    """First plans are admitted for the writer slots they could fill, plus one standby."""

    def dispatcher(self, issues, writers=2, fresh=True):
        for n, path in issues.items():
            self.add_work(n, path, "x")
        self.epic(*sorted(self.scripts))
        engine = self.engine_for(caps=Caps(ci_poll_s=0, queue_poll_s=0, max_parallel_writers=writers), fresh=fresh)
        self.pool, self.running = RecordingPool(), {}
        return engine

    def dispatch(self):
        self.engine._dispatch(self.pool, self.running)  # ty: ignore[invalid-argument-type]  # test double
        return list(self.pool.submitted)

    def put(self, n, phase, plan=True, store=None, **changes):
        """Issue `n` reaches `phase` as a step would leave it; its step, if any, has ended."""
        store = store or self.store
        st = store.issue(n)
        st.phase = phase
        if plan:
            st.plan = {"items": [], "notes": "saved"}
        for k, v in changes.items():
            setattr(st, k, v)
        store.put(st)
        self.running.pop(n, None)

    def plans(self):
        return [n for step, n in self.pool.submitted if step == "plan"]

    def writing(self):
        return [n for n in self.engine.plan.work if self.store.issue(n).phase in WRITING_PHASES]

    def test_two_writers_admit_three_first_plans_until_a_writer_releases_its_slot(self):
        self.dispatcher({n: f"docs/{n}.txt" for n in range(2, 8)})
        for _ in range(3):
            self.dispatch()
        self.assertEqual(self.plans(), [2, 3, 4], "two slots and one standby, however often it dispatches")
        self.put(2, Phase.PLANNED)          # a completed plan builds at once: the slot is taken, not released
        self.dispatch()
        self.put(3, Phase.PLANNED)
        self.dispatch()
        self.put(4, Phase.PLANNED)          # a completed plan awaiting its build still counts
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.plans(), [2, 3, 4])
        self.assertEqual(self.writing(), [2, 3])
        self.assertEqual(self.store.issue(4).phase, Phase.PLANNED)
        self.put(2, Phase.DONE)             # the writer releases its slot
        self.dispatch()
        self.assertEqual(self.pool.submitted[-2:], [("build", 4), ("plan", 5)])
        self.assertEqual(self.writing(), [3, 4])

    def test_admission_scans_past_a_path_conflict_and_an_owner_wait(self):
        self.dispatcher({2: "docs/a.txt", 3: "docs/a.txt", 4: "docs/d.txt", 5: "docs/a.txt", 6: "docs/f.txt",
                         7: "docs/g.txt"})
        self.put(2, Phase.BUILD)
        self.running[2] = Future()          # one writer, on `docs/a.txt`
        self.put(4, Phase.WAITING, wait={"kind": "owner_review", "detail": "the owner reviews it"})
        self.dispatch()
        self.assertEqual(self.plans(), [3, 6], "#3 can only be the standby; #6 fills the free slot, #5 and #7 wait")

    def test_a_ready_build_goes_before_a_fresh_standby_plan(self):
        self.dispatcher({2: "docs/a.txt", 3: "docs/b.txt", 4: "docs/c.txt", 5: "docs/d.txt"})
        self.put(3, Phase.PLANNED)
        self.put(4, Phase.PLANNED)
        self.assertEqual(self.dispatch(), [("build", 3), ("build", 4), ("plan", 2)])

    def test_a_prerequisite_found_while_planning_frees_its_place_and_keeps_its_plan(self):
        self.add_work(2, "docs/a.txt", "x", items=["`docs/a.txt` contains `x`.", "It uses the widget of #4."],
                      deferred=(("D2", 4),))
        self.add_work(4, "docs/d.txt", "x")
        self.gh.issues[4]["title"] = "Prerequisite widget"   # what the fake plan quotes of the issue it needs
        self.dispatcher({3: "docs/c.txt", 5: "docs/e.txt"}, writers=1)
        self.dispatch()
        self.assertEqual(self.plans(), [2, 3], "one slot and one standby")
        del self.running[2]
        self.engine._step_plan(2)          # the plan finds that #2 needs #4, in this run
        self.assertEqual((self.store.issue(2).phase, self.engine.plan.deps[2]), (Phase.PENDING, {4}))
        self.dispatch()
        self.assertEqual(self.plans(), [2, 3, 4], "#2 no longer holds a place: #4 takes it")
        self.put(4, Phase.DONE)
        self.put(3, Phase.DONE)
        self.dispatch()
        self.assertEqual(self.pool.submitted[-2:], [("build", 2), ("plan", 5)])
        self.assertEqual([c for c in self.agents.calls if c[0] == "plan" and c[2] == 2], [("plan", "claude", 2)],
                         "the saved plan is reused, not made again")

    def test_a_resumed_run_builds_its_saved_plans_under_the_writer_cap(self):
        issues = {n: f"docs/{n}.txt" for n in range(2, 8)}
        self.dispatcher(issues)
        for n in (2, 3):
            self.put(n, Phase.PLANNED)
        for n in (4, 5):                     # as a stopped run leaves them: planned, not yet moved on
            self.put(n, Phase.PENDING)
        self.engine_for(caps=Caps(ci_poll_s=0, queue_poll_s=0, max_parallel_writers=2))   # the run resumes
        self.assertEqual(self.dispatch(), [("build", 2), ("build", 3)])
        self.assertEqual([self.store.issue(n).phase for n in (4, 5)], [Phase.PLANNED, Phase.PLANNED])
        self.put(2, Phase.DONE)
        self.dispatch()
        self.put(3, Phase.DONE)
        self.dispatch()
        self.assertEqual(self.pool.submitted, [("build", 2), ("build", 3), ("build", 4), ("build", 5), ("plan", 6)])
        self.assertEqual(self.writing(), [4, 5])


if __name__ == "__main__":
    unittest.main()
