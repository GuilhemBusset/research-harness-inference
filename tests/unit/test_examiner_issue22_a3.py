from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")


def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_pull_request_runs_container_job():
    w = workflow()
    assert 'pull_request' in w['on']
    assert not w['on']['pull_request']
    assert 'if' not in w['jobs']['container-tests']
