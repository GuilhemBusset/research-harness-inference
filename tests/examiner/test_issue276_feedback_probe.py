"""Independent feedback and failed-issue revival checks for issue 276."""

import json
from dataclasses import replace

from helpers import SHIP, TempRepo, issue_body
from test_deploying import L


def test_failed_verification_preserves_the_run_and_request_decision():
    marker = "examiner diagnostic from verifier stderr"
    refusal = "retain the already locked proof command"

    class Agents(L.agents.FakeAgents):
        def build(self, vendor, req, plan, worktree, feedback):
            result = super().build(vendor, req, plan, worktree, feedback)
            count = self._built[2]
            if count == 2:
                result.commands = [replace(c, command=c.command + " && true") for c in result.commands]
            if count == 3:
                said = "\n".join(feedback)
                assert marker in said
                assert refusal in said
                assert "exit 9" in said
                result.requests.append(L.agents.Request(
                    "dispute", "the reproduction fails independently of this issue", finding="E2-1-1"))
            return result

        def examine(self, *args):
            result = super().examine(*args)
            for finding in result.findings:
                finding.reproduction = f"printf '%s\\n' '{marker}' >&2; exit 9"
            return result

        def decide(self, vendor, req, worktree, requests):
            super().decide(vendor, req, worktree, requests)
            return [L.agents.Decision(r["id"], False, refusal) for r in requests]

        def arbitrate(self, vendor, req, dispute, worktree=None):
            super().arbitrate(vendor, req, dispute, worktree)
            return L.agents.Ruling(False, "the item does not require that reproduction to pass")

    repo = TempRepo()
    try:
        github = L.github.FakeGitHub(on_merge=repo.merge)
        github.add_issue(2, "Feedback check", issue_body(
            "feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["The test passes on main."]),
            labels=("size:M",))
        github.add_issue(1, "Epic", "Epic.\n", subs=(2,))
        agents = Agents({2: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)})
        store = L.state.RunStore.for_root(repo.work, 1)
        workspace = L.workspace.Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        L.engine.Engine(
            L.graph.build_plan(github, 1, repo.work), store, github, workspace, agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=repo.work, log=lambda m: None, sleep=lambda s: None).run()
        state = store.issue(2)
        assert state.phase == L.model.Phase.DONE
        builds = [call for call in agents.calls if call[0] == "build"]
        assert len(builds) == 3
        assert marker in "\n".join(builds[1][3])
        assert len([call for call in agents.calls if call[0] == "decide"]) == 1
        assert all("verifier_run" not in f for f in state.findings if f["status"] == "closed")
        published = json.dumps([github.calls, github.comments, github.issues, github.prs], default=str)
        assert marker not in published
        assert marker not in json.dumps([e for e in store.events() if e["kind"] == "round_failed"])
    finally:
        repo.cleanup()


def test_new_version_restores_the_spent_call_ceiling_once(tmp_path):
    assert tuple(map(int, L.engine.VERSION.split("."))) > (1, 2, 5)
    engine = L.engine.Engine.__new__(L.engine.Engine)
    engine.store = L.state.RunStore(tmp_path / "run")
    state = L.model.IssueState(
        number=32, phase=L.model.Phase.FAILED, branch="loop/issue-32", worktree="work",
        plan={"items": []}, fail={"version": "1.2.1", "cause": "ceiling", "resumed": False})
    engine.store.put(state)
    engine.store.change_budget(32, calls=30)
    engine._maybe_revive(state)
    assert state.phase == L.model.Phase.BUILD
    assert state.branch == "loop/issue-32" and state.worktree == "work"
    budget = engine.store.budget(32)
    assert budget["calls"] - budget["bonus"] == 0
    engine._maybe_revive(state)
    assert engine.store.budget(32) == budget
    assert len([e for e in engine.store.events() if e["kind"] == "revived"]) == 1
