import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import L


def test_latest_context_across_pages():
    context = 'deploy/examiner'
    rows = [{'id': i, 'context': 'unrelated', 'state': 'pending'} for i in range(100)]
    rows[3] = {'id': 900, 'context': context, 'state': 'success', 'description': 'ready'}
    older = {'id': 800, 'context': context, 'state': 'failure', 'description': 'old'}
    calls = []

    def runner(args, **kwargs):
        calls.append(args[-1])
        page = [older] if 'page=2' in args[-1] else rows
        return subprocess.CompletedProcess(args, 0, json.dumps(page), '')

    real = L.github.GhCli('example/repo', runner=runner)
    assert real.deploy_status('commit-one', context) == {'state': 'success', 'description': 'ready'}
    assert len(calls) == 2
    fake = L.github.FakeGitHub()
    fake.set_status('commit-one', context, 'failure', 'old')
    fake.set_status('commit-one', 'unrelated', 'error', 'other')
    fake.set_status('commit-one', context, 'success', 'ready')
    assert fake.deploy_status('commit-one', context) == real.deploy_status('commit-one', context)
    assert fake.deploy_status('commit-two', context) is None
    assert real.deploy_status('commit-one', 'absent') is None
