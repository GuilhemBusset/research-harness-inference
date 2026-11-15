import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/ci.yml")


def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize('result', ['success', 'failure', 'cancelled', 'skipped', 'missing'])
def test_required_executes_result_check(result):
    required = workflow()['jobs']['required']
    assert required['if'] == 'always()'
    assert 'container-tests' in required['needs']
    step = next(s for s in required['steps'] if 'run' in s)
    assert step['env']['NEEDS'] == '${{ toJSON(needs) }}'
    assert 'if' not in step and 'continue-on-error' not in step
    needs = {name: {'result': 'success'} for name in required['needs']}
    if result == 'missing':
        del needs['container-tests']
    else:
        needs['container-tests']['result'] = result
    run = subprocess.run(
        ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
        env={'PATH': os.defpath, 'NEEDS': json.dumps(needs)},
        capture_output=True, text=True, check=False,
    )
    assert (run.returncode == 0) == (result == 'success'), run.stdout + run.stderr
