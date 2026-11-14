"""Independent checks of issue #23's status reader and fix-forward feedback."""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import DEPLOY, VERIFY, L, Phase, loop


def test_status_read_preserves_history_across_pages():
    rows = [
        {'id': i, 'context': DEPLOY, 'state': 'pending',
         'description': f'FACT_{i}', 'target_url': 'https://example.com'}
        for i in range(105, 0, -1)
    ]
    reads = []

    def runner(args, **kwargs):
        url = urlsplit(args[-1])
        assert url.path == 'repos/owner/repo/commits/abc123/statuses'
        query = parse_qs(url.query)
        page, size = int(query['page'][0]), int(query['per_page'][0])
        reads.append(page)
        return subprocess.CompletedProcess(args, 0, json.dumps(rows[(page - 1) * size:page * size]), '')

    expected = [{k: row[k] for k in ('context', 'state', 'description')} for row in reversed(rows)]
    assert L.github.GhCli('owner/repo', runner=runner).commit_statuses('abc123') == expected
    fake = L.github.FakeGitHub()
    for row in expected:
        fake.set_status('abc123', **row)
    assert fake.commit_statuses('abc123') == expected
    assert fake.commit_statuses('def456') == []
    assert len(reads) > 1


def repair_feedback(run, statuses):
    run.run()
    [sha] = run.fake.commits[2]
    assert run.waits(2) == (Phase.WAITING, 'deploy', 'deploying')
    run.fake.add_commit_comment(sha, 'COMMENT_TEXT_EXCLUDED')
    for context, state, description in statuses:
        run.fake.set_status(sha, context, state, description)
    run.fake.posts[2] = ['success']
    run.run()
    assert run.issue(2).phase == Phase.DONE
    assert len(run.events('fix_forward', 2)) == 1
    assert len(run.fake.merges(2)) == 2
    return run.feedback(2)[0]


def test_reporter_facts_and_root_descriptions_reach_fix_forward():
    with loop(2, contexts=(DEPLOY, VERIFY), timeout_s=0) as run:
        resource = f'{DEPLOY}/resource/WorkerService'
        smoke = f'{VERIFY}/smoke/test_streaming'
        told = repair_feedback(run, [
            (resource, 'pending', 'CREATE_IN_PROGRESS'),
            (resource, 'failure', 'CREATE_FAILED'),
            (smoke, 'failure', 'SMOKE_FAILED'),
            (DEPLOY, 'error', 'DEPLOYMENT_ERROR'),
            (VERIFY, 'success', 'VERIFICATION_PASSED'),
        ])
        for context, state, description in (
            (resource, 'failure', 'CREATE_FAILED'),
            (smoke, 'failure', 'SMOKE_FAILED'),
            (DEPLOY, 'error', 'DEPLOYMENT_ERROR'),
            (VERIFY, 'success', 'VERIFICATION_PASSED'),
        ):
            assert f'`{context}` is `{state}`' in told
            assert description in told
        assert 'CREATE_IN_PROGRESS' not in told
        assert 'COMMENT_TEXT_EXCLUDED' not in json.dumps(run.agents.calls, default=str)


def test_summary_filters_contexts_and_merge_commit():
    with loop(2, contexts=(DEPLOY,), timeout_s=0) as run:
        run.fake.set_status('def456', f'{DEPLOY}/resource/OtherCommit', 'failure', 'FOREIGN_COMMIT')
        excluded = [
            ('measure/beta/latency', 'failure', 'MEASUREMENT_ONLY'),
            (VERIFY, 'failure', 'OTHER_STAGE'),
            (f'{VERIFY}/smoke/test_login', 'failure', 'OTHER_STAGE_FACT'),
            (f'{DEPLOY}-canary', 'failure', 'SIBLING_STAGE'),
        ]
        told = repair_feedback(run, [
            *excluded,
            (DEPLOY, 'failure', 'DEPLOYMENT_FAILED'),
            (f'{DEPLOY}/resource/WorkerService', 'failure', 'UPDATE_FAILED'),
        ])
        assert 'WorkerService' in told and 'UPDATE_FAILED' in told
        for context, _, description in excluded:
            assert context not in told
            assert description not in told
        assert 'OtherCommit' not in told and 'FOREIGN_COMMIT' not in told
