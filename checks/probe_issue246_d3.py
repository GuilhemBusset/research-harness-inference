"""A version newer than the previous head releases engine-version waits."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

from helpers import TempRepo
from test_deploying import L


def test_version_bump_releases_prior_engine_wait():
    module = ast.parse((ROOT / "tools/implementation-loop/implement_loop/__init__.py").read_text())
    version = [ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)][0]
    assert tuple(map(int, version.split("."))) > (1, 2, 2)
    assert L.engine.VERSION == version
    repo = TempRepo()
    try:
        engine = L.engine.Engine.__new__(L.engine.Engine)
        engine.store = L.state.RunStore.for_root(repo.work, 1)
        state = engine.store.issue(2)
        state.phase = L.model.Phase.WAITING
        state.wait = {"kind": L.model.WAIT_ENGINE, "version": "1.2.2", "resume": "pending"}
        engine._maybe_resume(state)
        assert state.phase == L.model.Phase.PENDING
        assert state.wait is None
        assert any(e["kind"] == "resumed" and e["issue"] == 2 for e in engine.store.events())
    finally:
        repo.cleanup()
