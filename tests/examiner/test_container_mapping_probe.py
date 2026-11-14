"""Independent examiner scaffolding and configuration probe for issue 14."""
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))
from helpers import SHIP, TempRepo, issue_body
from test_deploying import L

RUNTIME = "host:container-runtime"
JOB = "container-tests"
SUCCESS = {"id": 8123, "status": "completed", "conclusion": "success"}
TEXT = "`tests/container/test_service.py::test_ready` passes (needs host:container-runtime)."


@contextmanager
def scenario(text=TEXT, capability=RUNTIME, check=SUCCESS):
    repo = TempRepo()
    class CI(L.github.FakeGitHub):
        def __init__(self):
            super().__init__(on_merge=repo.merge)
            self.reads = []
        def required_check_run(self, sha, name):
            if name == "required":
                return super().required_check_run(sha, name)
            self.reads.append((sha, name))
            return dict(check) if check is not None else None
    class Agents(L.agents.FakeAgents):
        def plan(self, vendor, req, worktree):
            plan = super().plan(vendor, req, worktree)
            for item in plan.items:
                if item.id == "D2":
                    item.capability = capability
            return plan
    try:
        gh = CI()
        gh.add_issue(1, "Epic", "Test epic.", subs=(2,))
        gh.add_issue(2, "Work", issue_body("feat/container-probe", ["`docs/result.txt` exists.", text],
                                         ["The local check passes."]), labels=("size:M",))
        store = L.state.RunStore.for_root(repo.work, 1)
        agents = Agents({2: L.agents.Script(files={"docs/result.txt": "result"}, owner_items=("D2",))})
        policy = L.config.load_policy(ROOT / "tools/implementation-loop/loop.toml")
        engine = L.engine.Engine(L.graph.build_plan(gh, 1, repo.work), store, gh,
            L.workspace.Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir), agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=repo.work, policy=policy, log=lambda message: None, sleep=lambda seconds: None)
        summary = engine.run()
        yield SimpleNamespace(state=store.issue(2), gh=gh, summary=summary,
                              pr=next(iter(gh.prs.values())), body=gh.issues[2]["body"])
    finally:
        repo.cleanup()


def assert_owner(run, capability):
    assert run.state.phase == L.model.Phase.DELIVERED
    assert run.state.obligations == [{"item": "D2", "kind": "capability", "detail": capability,
                                      "agent_part": "time it on this host"}]
    assert run.gh.issues[2]["state"] == "open"
    assert len(run.state.human_tasks) == 1
    assert run.summary["owner_obligations"] == {2: run.state.human_tasks}
    assert not any(e["criterion"] == "D2" for e in run.state.evidence)


def test_mapping_is_loaded_from_host_table(tmp_path):
    policy = L.config.load_policy(ROOT / "tools/implementation-loop/loop.toml")
    assert RUNTIME in policy["lacks"]
    assert policy["ci_jobs"] == {RUNTIME: JOB}
    config = tmp_path / "loop.toml"
    config.write_text('[host]\nlacks = ["host:container-runtime"]\nci_jobs = { "host:container-runtime" = "custom-job" }\n')
    assert L.config.load_policy(config)["ci_jobs"] == {RUNTIME: "custom-job"}
