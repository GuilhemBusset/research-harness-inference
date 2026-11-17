"""The runner, in a git repository built here: it applies a patch at the recorded commit, refuses one
that does not apply, and leaves no commit, branch, worktree or push behind."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import run
from implement_loop.agents import ExamResult
from implement_loop.model import Finding

# A committer for the repository built here, and no configuration of the machine's (signing, hooks).
GIT_ENV = os.environ | {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Pilot",
    "GIT_AUTHOR_EMAIL": "pilot@example.com",
    "GIT_COMMITTER_NAME": "Pilot",
    "GIT_COMMITTER_EMAIL": "pilot@example.com",
}
RECORDED = "RETAIN = True\nPORT = 443\n"
LATER = "RETAIN = True\nPORT = 8443\n"
PATCHED = "RETAIN = False\nPORT = 443\n"
APPLIES = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-RETAIN = True
+RETAIN = False
 PORT = 443
"""
DOES_NOT_APPLY = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 RETAIN = True
-PORT = 80
+PORT = 0
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repository whose `main` has moved on from the commit the corpus records, pushed to a bare
    `origin`: a patch that applies at the recorded commit does not apply to its tree."""
    origin, source = tmp_path / "origin.git", tmp_path / "source"
    git("init", "--quiet", "--bare", str(origin), cwd=tmp_path)
    git("init", "--quiet", "--initial-branch=main", str(source), cwd=tmp_path)
    (source / "app.py").write_text(RECORDED)
    git("add", "app.py", cwd=source)
    git("commit", "--quiet", "-m", "recorded", cwd=source)
    recorded = git("rev-parse", "HEAD", cwd=source).strip()
    (source / "app.py").write_text(LATER)
    git("commit", "--quiet", "-am", "later", cwd=source)
    git("remote", "add", "origin", str(origin), cwd=source)
    git("push", "--quiet", "origin", "main", cwd=source)
    return source, origin, recorded


def corpus(folder: Path, commit: str, patches: dict[str, str]) -> run.Manifest:
    folder.mkdir()
    entries = "".join(
        f'\n[[defect]]\nid = "{name}"\nclass = "retention-removed"\nfile = "app.py"\n'
        f'description = "The table is no longer retained."\ntask = "Tidy the settings."\n'
        f'match = ["retain"]\ncaught_by_tests = false\n'
        for name in patches
    )
    (folder / "manifest.toml").write_text(
        f'commit = "{commit}"\n\n[classes.retention-removed]\ntitle = "Retention removed"\n'
        f'invariants = ["data loss"]\n{entries}'
    )
    for name, text in patches.items():
        (folder / f"{name}.patch").write_text(text)
    return run.load_manifest(folder / "manifest.toml")


def everything_git_keeps(source: Path, origin: Path) -> dict[str, str]:
    refs = ["for-each-ref", "--format=%(refname) %(objectname)"]
    return {
        "refs": git(*refs, cwd=source),
        "head": git("rev-parse", "HEAD", cwd=source),
        "objects": git("cat-file", "--batch-all-objects", "--batch-check", cwd=source),
        "worktrees": git("worktree", "list", "--porcelain", cwd=source),
        "status": git("status", "--porcelain", "--untracked-files=all", cwd=source),
        "stash": git("stash", "list", cwd=source),
        "origin refs": git(*refs, cwd=origin),
        "origin objects": git("cat-file", "--batch-all-objects", "--batch-check", cwd=origin),
    }


class FakeExaminer:
    """Stands in for the engine's agents: records what it was shown, and answers with one finding."""

    operator, other, meter = "claude", "codex", None

    def __init__(self) -> None:
        self.seen: list[dict] = []

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        self.seen.append({
            "vendor": vendor,
            "worktree": worktree,
            "head": git("rev-parse", "HEAD", cwd=worktree).strip(),
            "base": base_sha,
            "app.py": (worktree / "app.py").read_text(),
            "task": req.issue.title,
            "committed": git("status", "--porcelain", cwd=worktree),
        })
        finding = Finding(id="", title="The table is deleted with its stack", file="app.py", invariant="data loss")
        return ExamResult(verdicts=[], findings=[finding], probes=[], decisions=[], certify=False)


def test_examines_the_patch_at_the_recorded_commit_and_leaves_nothing_behind(
    repo: tuple[Path, Path, str], tmp_path: Path
) -> None:
    source, origin, recorded = repo
    manifest = corpus(tmp_path / "corpus", recorded, {"retention-dropped": APPLIES})
    before = everything_git_keeps(source, origin)
    examiner = FakeExaminer()

    outcomes = run.run(source, manifest, lambda tree: examiner, "codex", {})  # ty: ignore[invalid-argument-type] - a test double

    (seen,) = examiner.seen
    assert seen["head"] == recorded == seen["base"]
    assert seen["app.py"] == PATCHED
    assert seen["committed"] == " M app.py\n"   # the patch is in the worktree, not in a commit
    assert seen["task"] == "Tidy the settings." and seen["vendor"] == "codex"
    assert not seen["worktree"].exists()
    assert [(o.defect, o.caught, o.certified) for o in outcomes] == [("retention-dropped", True, False)]
    assert everything_git_keeps(source, origin) == before
    assert (source / "app.py").read_text() == LATER


def test_refuses_a_patch_that_does_not_apply_before_any_examination(
    repo: tuple[Path, Path, str], tmp_path: Path
) -> None:
    source, origin, recorded = repo
    manifest = corpus(tmp_path / "corpus", recorded, {"applies": APPLIES, "stale": DOES_NOT_APPLY})
    before = everything_git_keeps(source, origin)
    examiner = FakeExaminer()

    with pytest.raises(run.Refused, match="stale: does not apply to the recorded commit"):
        run.run(source, manifest, lambda tree: examiner, "codex", {})  # ty: ignore[invalid-argument-type] - a test double

    assert examiner.seen == []
    assert everything_git_keeps(source, origin) == before


def test_the_live_examiner_is_shown_the_change_that_is_not_committed(
    repo: tuple[Path, Path, str], tmp_path: Path
) -> None:
    source, _, recorded = repo
    manifest = corpus(tmp_path / "corpus", recorded, {"retention-dropped": APPLIES})
    with run.scratch_worktree(source, recorded) as tree:
        run.git("apply", str(manifest.defects[0].patch), cwd=tree)
        agents = run.live_agents(source, tmp_path / "calls", "codex")(tree)
        with agents.ws.disposable(tree) as where:
            assert where == tree
        diff = agents._diff(tree, recorded, recorded)
    assert "-RETAIN = True\n+RETAIN = False" in diff
    assert agents.other == "codex"
