"""Regression for the engine fault d80596ed7c (#244): landing merged main, main had changed a locked
test, and the relock (`Engine._relock_candidates`, called by `_step_land`) raised `OSError` in
`_digest_at`, where it wrote the file as it was before the merge to a temporary directory. The digest
of a file at a commit is now taken from its bytes, with no file written."""

import errno
from pathlib import Path
from unittest import mock

import test_engine
from helpers import SHIP, git, present
from implement_loop import verify
from implement_loop.agents import FakeAgents
from implement_loop.model import Phase
from implement_loop.workspace import Workspace
from test_engine import EPIC, EngineCase
from test_unwritable_probe import Engine  # the `engine.py` beside these tests, also under the verifier

LOCKED = "checks/issue_2.sh"
FROM_MAIN = "# a requirement that arrived from main\n"


class MainChangesTheLockedTestBeforeLanding(FakeAgents):
    """The fake agents. While the head is examined, which is where its test locks and after the
    build merged main, main changes that test: landing is the step that meets the change."""

    def __init__(self, scripts, move_main):
        super().__init__(scripts)
        self.move_main = move_main

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        result = super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)
        if since is None:
            self.move_main()
        return result


class RelockDigestTests(EngineCase):
    def seed_main(self, files: dict[str, str]) -> None:
        merger = self.repo.merger
        git("fetch", "-q", "origin", cwd=merger)
        git("checkout", "-q", "-B", "main", "origin/main", cwd=merger)
        for rel, content in files.items():
            (merger / rel).parent.mkdir(parents=True, exist_ok=True)
            (merger / rel).write_text(content)
        git("add", "-A", cwd=merger)
        git("commit", "-qm", "fixture: move main", cwd=merger)
        git("push", "-q", "origin", "HEAD:main", cwd=merger)

    def test_landing_relocks_a_test_main_changed_on_a_host_that_refuses_the_temporary_file(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        agents = FakeAgents(self.scripts)
        test = agents._test_script(2)   # already on main, so the branch never changes it and main's change merges
        self.seed_main({LOCKED: test})
        agents = MainChangesTheLockedTestBeforeLanding(self.scripts, lambda: self.seed_main({LOCKED: test + FROM_MAIN}))
        relock = Engine._relock_candidates
        relocks = []

        def on_the_faulting_host(engine, n, wt, before):
            relocks.append(engine.store.issue(n).phase)
            refusal = OSError(errno.ENOSPC, "the host refuses the file")
            with mock.patch.object(Path, "write_bytes", side_effect=refusal):
                return relock(engine, n, wt, before)

        with mock.patch.object(test_engine, "Engine", Engine), \
                mock.patch.object(Engine, "_relock_candidates", on_the_faulting_host):
            self.run_engine(agents=agents)

        self.assertEqual(relocks, [Phase.LAND], "the relock ran once, in the land step")
        self.assertEqual(self.events("step_crashed"), [], "a file the host refuses to write is not an engine fault")
        self.assertTrue(any(e.get("phase") == "land" for e in self.events("updated_from_main", 2)))
        self.assertTrue(any(LOCKED in e["files"] and e["source"] == "main" for e in self.events("checks_relocked", 2)),
                        "main's version of the test is the locked one")
        self.assertEqual(self.phase(2), Phase.DONE)
        self.assertIn(FROM_MAIN.strip(), present(self.repo.main_file(LOCKED)))
        self.assertEqual(self.repo.main_file("docs/a.txt"), "alpha")

    def test_the_digest_at_a_commit_is_the_verifiers_digest_of_those_bytes(self):
        # what the relock compares: `verify.digest` of the working tree's file with this, of the commit's
        files = {
            "checks/plain.sh": b"#!/usr/bin/env bash\r\ntest -s docs/a.txt\n\xff\n",
            "pyproject.toml": b'[project]\r\nname = "caf\xe9"\r\n\r\n[tool.pytest]\r\naddopts = "-q"\r\n',
            "sub/pyproject.toml": b"[tool.pytest\nnot TOML \xff\n",
        }
        tree = self.repo.merger
        for rel, data in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes(data)
        git("add", "-A", cwd=tree)
        git("commit", "-qm", "fixture: files to digest", cwd=tree)
        engine = Engine.__new__(Engine)
        engine.ws = Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir)
        for rel in files:
            with self.subTest(file=rel):
                self.assertEqual(engine._digest_at(tree, "HEAD", rel), verify.digest(tree / rel))
        self.assertNotEqual(engine._digest_at(tree, "HEAD", "pyproject.toml"), verify.NO_PYTEST_CONFIG,
                            "a pyproject.toml is still digested by its pytest table")
        self.assertIsNone(engine._digest_at(tree, "HEAD", "checks/absent.sh"), "not a file at that commit")
