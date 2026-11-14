import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


def test_another_root_cannot_land_until_the_verdict():
    context = 'deploy/examiner'
    with loop(2, contexts=(context,), timeout_s=0) as run:
        run.add_work(3, epic=10)
        run.fake.posts[3] = ['success']
        run.run()
        commit = run.fake.commits[2][0]
        run.run(root=10, queue_wait_s=0)
        assert run.waits(3, root=10) == (Phase.WAITING, 'merge_queue', 'land')
        assert len(run.fake.merges()) == 1
        run.fake.set_status(commit, context, 'success')
        run.run()
        run.run(root=10)
        assert run.issue(2).phase == Phase.DONE
        assert run.issue(3, root=10).phase == Phase.DONE
        assert len(run.fake.merges()) == 2
