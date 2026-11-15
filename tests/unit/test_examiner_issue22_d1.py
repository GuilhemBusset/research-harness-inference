from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")


def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_container_job():
    w = workflow()
    job = w['jobs']['container-tests']
    assert job['name'] == 'container-tests'
    assert job['runs-on'] == 'ubuntu-24.04'
    assert 'if' not in job and 'continue-on-error' not in job
    assert w['permissions'] == {'contents': 'read'}
    assert 'permissions' not in job
    text = yaml.safe_dump(job).lower()
    for forbidden in ('secrets.', 'secrets[', 'id-token', 'aws-actions/', 'aws_'):
        assert forbidden not in text
    steps = job['steps']
    checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
    assert checkout['with']['persist-credentials'] == 'false'
    assert any(s.get('uses', '').startswith('astral-sh/setup-uv@') for s in steps)
    runtime = next(i for i, s in enumerate(steps) if s.get('run') == 'docker info')
    gate = next(i for i, s in enumerate(steps) if s.get('run') == 'bash scripts/check.sh container-tests')
    assert runtime < gate
    for i in (runtime, gate):
        assert 'if' not in steps[i] and 'continue-on-error' not in steps[i]
