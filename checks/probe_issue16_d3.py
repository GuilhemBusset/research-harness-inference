import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))
import test_history_checks as h


@pytest.fixture
def case():
    value = h.OwnersListCase(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()
        value.tearDown()


def test_config_commands_name_both_sources(case):
    commands = h.L.config.local_value_checks("base", "head")
    assert commands[0][1][-2:] == ("--rev", "head")
    assert commands[1][1][-2:] == ("--history", "base..head")
    assert h.L.config.local_values_file(os.environ) == case.values
