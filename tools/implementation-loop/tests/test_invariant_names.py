"""The safety invariants have one list, `[invariants] names` in loop.toml: the engine takes its names
from there, whether its caller loaded the file or passed a policy without them, and an engine whose
loop.toml gives none refuses to start."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from helpers import LOOP_TOML, REPO_ROOT
from implement_loop.config import load_policy
from test_engine import EngineCase


class InvariantNames(EngineCase):
    def setUp(self):
        super().setUp()
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)

    def test_the_engines_invariants_are_the_names_of_loop_toml(self):
        names = tuple(tomllib.loads(LOOP_TOML.read_text())["invariants"]["names"])
        self.assertTrue(names)
        self.assertEqual(self.engine_for(policy=load_policy(LOOP_TOML)).invariants, names)
        self.assertEqual(self.engine_for(policy={"lacks": []}).invariants, names, "a policy without them")

    def test_an_engine_whose_loop_toml_gives_no_names_refuses_to_start(self):
        for text in ('[host]\nlacks = []\n', '[invariants]\nnames = []\n', '[invariants]\nnames = [""]\n',
                     '[invariants]\nnames = ["secret exposure", " "]\n'):   # a blank name would match any
            with tempfile.TemporaryDirectory() as tmp, self.subTest(text=text):
                path = Path(tmp) / "loop.toml"
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, r"\[invariants\]"):
                    self.engine_for(policy=load_policy(path))

    def test_the_tests_that_need_an_invariant_still_hold_when_loop_toml_renames_one(self):
        """The engine's tests name an invariant through loop.toml: renaming every one keeps them passing."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "tools" / "implementation-loop"   # where the tests find the repository
            (Path(tmp) / "tools").mkdir()
            for shared in ("scripts", ".githooks", "tools/hygiene"):
                (Path(tmp) / shared).symlink_to(REPO_ROOT / shared)
            skip = shutil.ignore_patterns("__pycache__", ".pytest_cache")
            shutil.copytree(LOOP_TOML.parent, engine, ignore=skip)
            text = LOOP_TOML.read_text()
            for name in tomllib.loads(text)["invariants"]["names"]:
                text = text.replace(f'"{name}"', f'"renamed: {name[::-1]}"')
            (engine / "loop.toml").write_text(text)
            self.assertFalse(set(load_policy(engine / "loop.toml")["invariants"]) & set(load_policy(LOOP_TOML)["invariants"]))
            tests = ["test_review_fixes.FindingNew1OwnerFindings", "test_review_fixes.Finding02RevertFindings",
                     "test_examiner_bound"]
            run = subprocess.run([sys.executable, "-m", "unittest", *tests], cwd=engine / "tests",
                                 capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr[-3000:])
