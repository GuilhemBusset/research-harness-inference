"""Independent checks of failed examination feedback and one-time revival (#317)."""

from helpers import SHIP
from test_verifier_feedback import ISSUE, L, Loop, Phase

REASON = "This additional path is outside the issue's deliverables."


class RequestingAgents(L.agents.FakeAgents):
    """Ask once for an extra path before an examination withholds its certificate."""

    def build(self, vendor, req, plan, worktree, feedback):
        result = super().build(vendor, req, plan, worktree, feedback)
        if self._built[ISSUE] == 1:
            result.requests.append(L.agents.Request("paths", "Include another document", paths=["docs/extra.txt"]))
        return result

    def decide(self, vendor, req, worktree, requests):
        super().decide(vendor, req, worktree, requests)
        return [L.agents.Decision(r["id"], False, REASON) for r in requests]


def test_failed_certificate_keeps_the_request_decision_for_the_next_build():
    run = Loop()
    try:
        agents = RequestingAgents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, no_certify=1)})
        state = run.run(agents)
        builds = [call for call in agents.calls if call[0] == "build"]
        assert len(builds) == 2
        feedback = "\n".join(builds[1][3])
        assert REASON in feedback
        assert "withheld its certificate" in feedback
        assert state.phase == Phase.DONE, run.failures()
    finally:
        run.repo.cleanup()


def test_new_version_revives_a_saved_failure_once_with_its_feedback_and_locks():
    run = Loop()
    try:
        store = L.state.RunStore.for_root(run.repo.work, 1)
        workspace = L.workspace.Workspace(run.repo.work, 1, SHIP,
                                          base_dir=run.repo.loop_dir)
        engine = L.engine.Engine(
            L.graph.build_plan(run.fake, 1, run.repo.work), store, run.fake, workspace,
            L.agents.FakeAgents({}), L.config.Config(repo="test/test"), repo_root=run.repo.work,
            log=lambda message: None, sleep=lambda seconds: None,
        )
        state = store.issue(ISSUE)
        state.phase = Phase.FAILED
        state.plan = {"items": [], "zones": ["docs/a.txt"]}
        state.worktree = str(run.repo.work)
        state.fail = {"cause": "ceiling", "version": "1.2.4", "detail": "infrastructure attempts exhausted"}
        state.feedback = ["The examination left a blocking finding.", REASON, REASON]
        state.locked = {"checks/issue.sh": "locked digest"}
        state.findings = [{"id": "E2-1-1", "status": "open"}]
        expected_feedback = list(dict.fromkeys(state.feedback))
        expected_locks, expected_findings = dict(state.locked), list(state.findings)
        store.change_budget(ISSUE, set_calls=7, set_infra=10)

        engine._maybe_revive(state)
        store.put(state)
        saved = store.issue(ISSUE)
        assert saved.phase == Phase.BUILD
        assert saved.feedback[0].startswith("An earlier attempt failed (ceiling)")
        assert saved.feedback[1:] == expected_feedback
        assert saved.locked == expected_locks
        assert saved.findings == expected_findings
        assert saved.fail["resumed"]
        assert store.budget(ISSUE)["infra"] == 0
        assert tuple(map(int, L.engine.VERSION.split("."))) > (1, 2, 10)

        store.change_budget(ISSUE, set_infra=3)
        saved.phase = Phase.FAILED
        engine._maybe_revive(saved)
        assert saved.phase == Phase.FAILED
        assert store.budget(ISSUE)["infra"] == 3
        assert len([event for event in store.events() if event["kind"] == "revived"]) == 1
    finally:
        run.repo.cleanup()
