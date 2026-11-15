"""Independent checks for the two examination loops reported in issue 275."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))

from helpers import SHIP, TempRepo, issue_body
from test_deploying import L


class Responses(L.agents.FakeAgents):
    def __init__(self, owner_verdict):
        super().__init__({2: L.agents.Script(files={"docs/result.txt": "ready"}, owner_items=("D2",))})
        self.owner_verdict = owner_verdict
        self.examinations = 0

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        result = super().examine(vendor, req, plan, worktree, base_sha, head_sha,
                                 evidence, memory, requests, since)
        self.examinations += 1
        criterion = "D1, A1; D2 agent portion" if self.examinations == 1 else "D1 and A1"
        result.probes = [L.agents.Probe(
            "checks/joint.sh", criterion, "bash checks/joint.sh",
            content="#!/usr/bin/env bash\nset -eu\ntest \"$(cat docs/result.txt)\" = ready\n",
        )]
        owner = next(p for p in plan["items"] if p["id"] == "D2")
        if self.examinations == 1 and self.owner_verdict != "met":
            quote = ("The owner measures performance" if self.owner_verdict == "owner"
                     else owner["agent_part"])
            result.verdicts = [{"item": "D2", "verdict": "unmet", "quote": quote,
                                "note": "owner evidence pending" if self.owner_verdict == "owner"
                                else "the agent part is missing"}]
        else:
            result.verdicts = [{"item": "D2", "verdict": "met", "quote": owner["agent_part"],
                                "note": "agent part delivered"}]
        return result


@pytest.mark.parametrize("owner_verdict", ["met", "owner", "agent"])
def test_grouped_probe_and_owner_verdict_converge(owner_verdict):
    repo = TempRepo()
    try:
        github = L.github.FakeGitHub(on_merge=repo.merge)
        github.add_issue(1, "Epic", "An epic.\n", subs=(2,))
        github.add_issue(2, "Examination convergence", issue_body(
            "feat/examination-check",
            ["`docs/result.txt` contains `ready`.", "The owner measures performance on a laptop."],
            ["The result is checked by a test."],
        ), labels=("size:M",))
        agents = Responses(owner_verdict)
        store = L.state.RunStore.for_root(repo.work, 1)
        engine = L.engine.Engine(
            L.graph.build_plan(github, 1, repo.work), store, github,
            L.workspace.Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir), agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=repo.work, log=lambda message: None, sleep=lambda seconds: None,
            policy={"lacks": ["hardware:laptop"]},
        )
        engine.run()
        state = store.issue(2)
        events = store.events()
        failed = [e for e in events if e["kind"] == "round_failed"]
        assert state.phase == L.model.Phase.DELIVERED
        assert len(failed) == (1 if owner_verdict == "agent" else 0)
        assert len([c for c in agents.calls if c[0] == "build"]) == (2 if owner_verdict == "agent" else 1)
        assert not [c for c in agents.calls if c[0] == "arbitrate"]
        assert not [e for e in events if e["kind"] == "examination_incomplete" and e["unprobed"]]
        adopted = [p for e in events if e["kind"] == "tests_locked" for p in e["probes"]]
        assert adopted == ["checks/joint.sh"]
        assert agents.examinations <= 3
        assert state.certified_sha
        assert repo.main_file("docs/result.txt").strip() == "ready"
    finally:
        repo.cleanup()


def test_existing_grouped_probe_is_recognized_without_rewriting_saved_commands():
    engine = L.engine.Engine.__new__(L.engine.Engine)
    commands = [{"author": "examiner", "criterion": "D1, D2, D3, A1, A2; A3 agent portion",
                 "command": "bash checks/already-adopted.sh"}]
    state = SimpleNamespace(
        plan={"items": [{"id": i} for i in ("D1", "D2", "D3", "A1", "A2", "A3")]},
        commands=commands,
    )
    assert {"D1", "D2", "D3", "A1", "A2", "A3"} <= engine._probed(state)
    assert commands[0]["criterion"] == "D1, D2, D3, A1, A2; A3 agent portion"
