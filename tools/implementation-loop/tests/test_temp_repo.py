"""The suite's throwaway repositories (`helpers.TempRepo`): git starts no maintenance in them, so
nothing of git's is at work in a repository while a test's next command runs. What that maintenance
did to an engine run is told with `helpers.NO_MAINTENANCE`."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from helpers import TempRepo, git


@contextmanager
def traced(repo: TempRepo):
    """Every git process started inside the block writes git's own trace of what it did, the
    remote's side of a push and a hook's commands included. Yields a list that is filled, once the
    block ends, with the commands by which one of them started maintenance."""
    trace = Path(tempfile.mkdtemp(dir=repo.tmp, prefix="trace-"))
    started: list[list[str]] = []
    with mock.patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
        yield started
    for file in trace.iterdir():
        # A process that maintenance left running may still be writing its last line: whole lines only.
        for line in file.read_text().split("\n")[:-1]:
            event = json.loads(line)
            if event.get("event") == "child_start" and event["argv"][1:2] in (["maintenance"], ["gc"]):
                started.append(event["argv"])


def test_no_command_in_a_test_repository_starts_git_maintenance():
    repo = TempRepo()
    try:
        linked = repo.tmp / "linked"   # a linked worktree, as the engine's are: it has the repository's configuration
        git("worktree", "add", "--quiet", "-b", "feat/x", str(linked), "origin/main", cwd=repo.work)
        (linked / "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=linked)
        with traced(repo) as started:   # the commands after which git starts it: a commit, a fetch, a merge, a received push
            git("commit", "--quiet", "-m", "x", cwd=linked)
            git("push", "--quiet", "origin", "feat/x", cwd=linked)
            repo.merge("feat/x", git("rev-parse", "HEAD", cwd=linked))
            git("fetch", "--quiet", "origin", cwd=repo.work)
            git("merge", "--quiet", "--no-edit", "origin/main", cwd=linked)
        assert started == []

        with traced(repo) as started:   # and the trace does show one where git is told to start it
            git("-c", "maintenance.auto=true", "commit", "--quiet", "--allow-empty", "-m", "again", cwd=linked)
        assert started
    finally:
        repo.cleanup()
