"""Independent issue #8 probes; subprocesses import this checkout's engine."""

import subprocess
import sys
import textwrap
from pathlib import Path

PREAMBLE = r'''
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path('tools/implementation-loop').resolve()))
from implement_loop import config, gate
from implement_loop.agents import ItemPlan, PlanResult
from implement_loop.engine import Engine
from implement_loop.model import IssueState, Phase
from implement_loop.state import RunStore

assert Path(config.__file__).resolve() == Path('tools/implementation-loop/implement_loop/config.py').resolve()
TARGETS = ['src/rhi/pipeline/stack.py', 'setup/bootstrap/policies.yaml']
FOLDERS = ['src/rhi/pipeline/', 'setup/bootstrap/']

def plan_grants(named, repair=False):
    body = '## Deliverables\n' + '\n'.join('- [ ] `' + p + '` changes.' for p in named)
    body += '\n## Acceptance criteria\n- [ ] A review confirms the change.\n'
    item = ItemPlan(id='D1', reading='Make the change.', quote='Make the change.', proof='review')
    agents = SimpleNamespace(
        plan=lambda *args: PlanResult(items=[item], zones=TARGETS, decisions=[]),
        triage=lambda *args: [],
    )
    with tempfile.TemporaryDirectory(prefix='examiner-zones-') as tmp:
        engine = Engine.__new__(Engine)
        engine.store = RunStore(Path(tmp) / 'run')
        engine.caps = config.Caps()
        engine.repair, engine.other, engine.op = repair, 'codex', 'claude'
        engine.lacks, engine._lookup_issue = [], lambda n: None
        engine.plan = SimpleNamespace(
            specs={8: SimpleNamespace(ledger=[SimpleNamespace(id='D1', text='Make the change.')], declared_paths=[])},
            issues={8: SimpleNamespace(body=body)}, work=[8],
        )
        engine.agents = agents
        engine.store.put(IssueState(number=8, phase=Phase.PLAN))
        with patch.object(Engine, '_req'), patch.object(Engine, '_set_phase'), \
                patch('implement_loop.engine.scheduler.prerequisites_met', return_value=True):
            engine._step_plan(8)
        return engine.store.issue(8).grants, engine.store.events()
'''


def run_real(source):
    result = subprocess.run(
        [sys.executable, '-c', PREAMBLE + '\n' + textwrap.dedent(source)],
        cwd=Path.cwd(), capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a1_plan_withholds_unnamed_and_grants_named_paths():
    run_real('''
        grants, events = plan_grants(['src/rhi/stage.py'])
        assert not grants, grants
        withheld = [e for e in events if e['kind'] == 'protected_zones_withheld']
        assert len(withheld) == 1
        assert set(withheld[0]['zones']) == set(TARGETS)
        assert gate.zone_problems(TARGETS, grants, [], False)
        for names in [FOLDERS, TARGETS]:
            grants, events = plan_grants(names)
            assert {g['path']: g['protected'] for g in grants} == dict.fromkeys(TARGETS, True)
            assert gate.zone_problems(TARGETS, grants, [], False) == []
    ''')


def test_a1_not_safety_surface_and_repair_grants_work():
    run_real('''
        for path in FOLDERS + TARGETS:
            assert not config.is_safety_surface(path), path
        grants, events = plan_grants(FOLDERS, repair=True)
        assert {g['path']: g['protected'] for g in grants} == dict.fromkeys(TARGETS, True)
        assert gate.zone_problems(TARGETS, grants, [], True) == []
    ''')
