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


def test_trusted_commands_and_allowlist_are_used_even_if_branch_changes_them(case):
    seed = case.repo.tmp / "seed"
    sentinel = case.repo.tmp / "untrusted-executions"
    (seed / "tools/hygiene/scan.py").write_text(
        "import os\nfrom pathlib import Path\n"
        f"with Path({str(sentinel)!r}).open('a') as f:\n"
        "    f.write(os.environ.get('LOOP_LAUNCHED', 'outside') + '\\n')\n"
    )
    (seed / "tools/hygiene/hygiene.toml").write_text('[exclude]\npaths = ["**"]\n')
    h.git("commit", "-qam", "untrusted scanner and configuration", cwd=seed)
    h.git("push", "-q", "origin", "HEAD:main", cwd=seed)
    case.work()
    case.run_engine(h.OwnersValueAgents(case.scripts, rounds=1))
    assert case.ended() == ("failed", "history")
    assert sentinel.read_text().splitlines()
    assert set(sentinel.read_text().splitlines()) == {"inside"}
    assert len(case.outside) == 2
    for command in case.outside:
        assert command[1:5] == ("-I", str(h.SCANNER / "scan.py"), "--config", str(h.SCANNER / "hygiene.toml"))
    assert [a[-2] for a in case.outside] == ["--rev", "--history"]
