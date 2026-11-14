import json
import runpy
from pathlib import Path


def test_current_definition_is_accepted():
    module = runpy.run_path("tests/policy/test_ruleset.py")
    expected = Path("setup/pr-only-main.json").resolve()
    assert module["RULESET"] == expected
    assert module["violations"](json.loads(expected.read_text())) == []
    module["test_the_ruleset_definition_holds"]()
