import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


@pytest.mark.parametrize('state', ['failure', 'error'])
def test_final_bad_status_is_sent_to_fix_forward(state):
    context = 'deploy/examiner'
    with loop(2, contexts=(context,)) as run:
        run.fake.posts[2] = [(state, 'STATUS_DESCRIPTION'), 'success']
        run.run()
        assert len(run.events('fix_forward', 2)) == 1
        assert len(run.fake.merges(2)) == 2
        feedback = run.feedback(2)[1]
        assert context in feedback and state in feedback
        assert 'STATUS_DESCRIPTION' in feedback   # issue #23: a configured context's description is in the summary
        assert run.issue(2).phase == Phase.DONE


def test_unsuccessful_fix_forward_uses_a_revert_pr():
    with loop(2, contexts=('deploy/examiner',)) as run:
        run.repairs = False
        run.fake.posts[2] = ['failure', 'success']
        run.run()
        assert len(run.events('fix_forward', 2)) == 1
        assert len(run.events('deployment_failed', 2)) == 2
        events = run.events('reverted', 2)
        assert events and run.fake.prs[events[0]['pr']]['merged']
