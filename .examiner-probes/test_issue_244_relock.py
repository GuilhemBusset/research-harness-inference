"""Independent digest equivalence and relock authorization checks using real git blobs."""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

from helpers import SHIP, TempRepo, git
from implement_loop import verify
from implement_loop.workspace import Workspace
from test_unwritable_probe import Engine


def test_relock_accepts_main_change_and_refuses_changed_branch_check():
    repo = TempRepo()
    try:
        tree = repo.merger
        (tree / "checks").mkdir()
        accepted = "checks/from_main.sh"
        refused = "checks/from_branch.sh"
        for rel in (accepted, refused):
            (tree / rel).write_bytes(b"exit 0\n")
        locked = verify.lock(tree, [accepted, refused], ["."])
        (tree / refused).write_bytes(b"exit 1\n")
        git("add", "checks", cwd=tree)
        git("commit", "-qm", "fixture: branch before merge", cwd=tree)
        before = git("rev-parse", "HEAD", cwd=tree)
        (tree / accepted).write_bytes(b"true\n")
        (tree / refused).write_bytes(b"false\n")
        events = []
        state = SimpleNamespace(locked=locked, commands=[{"cwd": "."}])
        engine = Engine.__new__(Engine)
        engine.ws = Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        engine.store = SimpleNamespace(issue=lambda n: state, event=lambda kind, **fields: events.append((kind, fields)))
        assert engine._relock_candidates(2, tree, before) == {accepted: verify.digest(tree / accepted)}
        assert any(kind == "checks_not_relocked" and fields["files"] == [refused] for kind, fields in events)
    finally:
        repo.cleanup()


def test_digest_matches_verifier_for_valid_and_invalid_pytest_configuration():
    repo = TempRepo()
    try:
        tree = repo.merger
        cases = {
            "valid/pyproject.toml": b'[project]\r\nname = "caf\xc3\xa9"\r\n[tool.pytest.ini_options]\r\naddopts = "-q"\r\n',
            "replace/pyproject.toml": b'[project]\nname = "caf\xff"\n[tool.pytest.ini_options]\naddopts = "-x"\n',
            "invalid/pyproject.toml": b"[tool.pytest\n\xff\n",
            "ordinary.sh": b"echo \xff\r\n",
        }
        for rel, data in cases.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes(data)
        git("add", "-A", cwd=tree)
        git("commit", "-qm", "fixture: configurations", cwd=tree)
        engine = Engine.__new__(Engine)
        engine.ws = Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        for rel in cases:
            assert engine._digest_at(tree, "HEAD", rel) == verify.digest(tree / rel)
        original = engine._digest_at(tree, "HEAD", "valid/pyproject.toml")
        (tree / "valid/pyproject.toml").write_text('[project]\nname = "different"\n[tool.pytest.ini_options]\naddopts = "-q"\n')
        assert original == verify.digest(tree / "valid/pyproject.toml")
        (tree / "valid/pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-x"\n')
        assert original != verify.digest(tree / "valid/pyproject.toml")
    finally:
        repo.cleanup()
