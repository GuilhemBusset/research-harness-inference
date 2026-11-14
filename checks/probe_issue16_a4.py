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


@pytest.mark.parametrize("bad_list", [False, True])
def test_absent_list_skips_outside_scan_but_unreadable_list_withholds_push(case, bad_list):
    if bad_list:
        case.values.write_bytes(b"\xff")
    else:
        os.environ.pop("HYGIENE_LOCAL_VALUES")
    case.work()
    result = case.run_engine()
    if bad_list:
        assert case.events("push_withheld", 2)
        assert not case.events("history_refused", 2)
        assert not case.gh.prs
        assert "feat/issue-2" not in case.remote_branches()
    else:
        assert result["phases"] == {"done": 1}
        assert not case.outside
        assert "feat/issue-2" in case.remote_branches()
