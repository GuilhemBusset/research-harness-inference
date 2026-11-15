"""Independent request-review and failed-issue revival probes for issue 274."""

import importlib
import importlib.util
import json
import runpy
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def candidate():
    root = Path(__file__).resolve().parents[2]
    package = root / "tools/implementation-loop/implement_loop"
    name = "issue274_examiner_candidate"
    spec = importlib.util.spec_from_file_location(
        name, package / "__init__.py", submodule_search_locations=[str(package)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return SimpleNamespace(version=module.VERSION, **{
        part: importlib.import_module(f"{name}.{part}")
        for part in ("engine", "model", "state", "live")})


@pytest.mark.parametrize("approve", [True, False], ids=["approved", "refused"])
def test_decision_reads_complete_proposal_and_keeps_sibling_command(candidate, tmp_path, monkeypatch, approve):
    tree = tmp_path / "builder"
    tree.mkdir()
    replacement, deletion = "test_replaced.py", "test_deleted.py"
    original = "VALUE = 1\n"
    for filename in (replacement, deletion):
        (tree / filename).write_text(original)
    proposal = "# more test cases\n" * 8000 + "VALUE = 274\n"
    changes = {replacement: proposal, deletion: None}
    command = {"criterion": "D1", "command": "python test_replaced.py", "author": "builder", "cwd": "."}
    requests = [
        {"id": "R42-1", "kind": "test_change", "status": "pending", "detail": "correct locked checks",
         "paths": list(changes), "content": changes.copy()},
        {"id": "R42-2", "kind": "command_change", "status": "pending", "detail": "run corrected checks",
         "item": "D1", "commands": [command]},
    ]

    class Workspace:
        @contextmanager
        def disposable(self, worktree):
            scratch = tmp_path / "decider"
            shutil.copytree(worktree, scratch)
            try:
                yield scratch
            finally:
                shutil.rmtree(scratch)

        def commit_all(self, *args, **kwargs):
            return "approved-test-change"

    reviewed = []

    def decide(vendor, req, scratch, shown):
        assert (scratch / replacement).read_text() == proposal
        assert runpy.run_path(str(scratch / replacement))["VALUE"] == 274
        assert not (scratch / deletion).exists()
        assert all((tree / p).read_text() == original for p in changes)
        assert shown[0].get("content") is None
        assert shown[0]["proposed"] == {replacement: "replaced", deletion: "deleted"}

        def call(_vendor, _role, cwd, prompt, _issue):
            assert cwd == scratch
            payload = json.loads(prompt.split("<requests>")[1].split("</requests>")[0])
            assert [r["id"] for r in payload] == [r["id"] for r in requests]
            assert payload[1]["commands"] == [command]
            reviewed.append(payload)
            return {"decisions": [
                {"request": r["id"], "approve": approve, "reason": "independent review", "dispositions": []}
                for r in payload]}

        adapter = SimpleNamespace(_issue_vars=lambda *_: {}, call=call)
        return candidate.live.LiveAgents.decide(adapter, vendor, req, scratch, shown)

    engine = candidate.engine.Engine.__new__(candidate.engine.Engine)
    engine.store = candidate.state.RunStore(tmp_path / "run")
    engine.ws, engine.repair, engine.other = Workspace(), False, "codex"
    engine.agents = SimpleNamespace(decide=decide)
    monkeypatch.setattr(engine, "_req", lambda n: SimpleNamespace(issue=SimpleNamespace(number=n)))
    state = candidate.model.IssueState(number=42, requests=requests, tests=list(changes))
    engine.store.put(state)
    engine._decide_requests(42, tree)
    saved = engine.store.issue(42)
    assert len(reviewed) == 1
    assert saved.requests[0]["content"] == changes
    assert [r["status"] for r in saved.requests] == ["approved" if approve else "refused"] * 2
    assert (tree / replacement).read_text() == (proposal if approve else original)
    assert (tree / deletion).exists() is not approve
    if not approve:
        assert (tree / deletion).read_text() == original
    assert saved.commands == ([command] if approve else [])
    assert not (tmp_path / "decider").exists()


def test_new_version_revives_failed_issue_once_with_fresh_ceiling(candidate, tmp_path):
    assert tuple(map(int, candidate.version.split("."))) > (1, 2, 4)
    engine = candidate.engine.Engine.__new__(candidate.engine.Engine)
    engine.store = candidate.state.RunStore(tmp_path / "run")
    state = candidate.model.IssueState(
        number=42, phase=candidate.model.Phase.FAILED, branch="loop/issue-42", worktree="work",
        plan={"items": []}, unproductive=2, replans=1,
        fail={"version": "1.2.4", "cause": "no_progress", "resumed": False})
    engine.store.put(state)
    engine.store.change_budget(42, calls=30, infra=4)
    engine._maybe_revive(state)
    assert state.phase == candidate.model.Phase.BUILD
    assert state.branch == "loop/issue-42" and state.worktree == "work"
    assert state.fail["resumed"] is True
    assert state.unproductive == state.replans == 0
    budget = engine.store.budget(42)
    assert budget["calls"] - budget["bonus"] == 0 and budget["infra"] == 0
    engine._maybe_revive(state)
    assert engine.store.budget(42) == budget
    assert len([e for e in engine.store.events() if e["kind"] == "revived"]) == 1
