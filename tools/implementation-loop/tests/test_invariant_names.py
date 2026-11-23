"""The safety invariants have one list, `[invariants] names` in loop.toml: the engine takes its names
from there, whether its caller loaded the file or passed a policy without them, and an engine whose
loop.toml gives none refuses to start."""

from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path

from helpers import HERE
from implement_loop.config import load_policy
from test_engine import EngineCase

LOOP_TOML = HERE.parent / "loop.toml"


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
        for text in ('[host]\nlacks = []\n', '[invariants]\nnames = []\n'):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(text=text):
                path = Path(tmp) / "loop.toml"
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, r"\[invariants\]"):
                    self.engine_for(policy=load_policy(path))
