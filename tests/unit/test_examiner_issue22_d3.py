from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")


def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_tests_node_and_timeout():
    jobs = workflow()['jobs']
    synth = next(s for s in jobs['synth']['steps'] if s.get('uses', '').startswith('actions/setup-node@'))
    tests = jobs['tests']
    assert tests['timeout-minutes'] == '40'
    steps = tests['steps']
    index, node = next((i, s) for i, s in enumerate(steps) if s.get('uses', '').startswith('actions/setup-node@'))
    assert node['uses'] == synth['uses']
    assert node['with']['node-version'] == '24'
    assert 'if' not in node and 'continue-on-error' not in node
    gates = [i for i, s in enumerate(steps) if 'scripts/check.sh' in s.get('run', '')]
    assert gates and index < min(gates)
