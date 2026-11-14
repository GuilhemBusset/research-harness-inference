import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


@pytest.mark.parametrize('posted', [None, ('pending', 'ordinary deployment')])
def test_timeout_resumes_the_original_commit(posted):
    context = 'deploy/examiner'
    with loop(2, contexts=(context,), timeout_s=0, approval='approval') as run:
        run.fake.posts[2] = [posted]
        run.run()
        assert run.waits(2) == (Phase.WAITING, 'deploy', 'deploying')
        commit = run.fake.commits[2][0]
        assert run.fake.issues[2]['state'] == 'open'
        run.fake.set_status(commit, context, 'success')
        run.run()
        assert run.issue(2).phase == Phase.DONE
        assert set(run.fake.reads) == {(commit, context)}
        assert len(run.fake.merges(2)) == 1
