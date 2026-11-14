import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


def test_polling_is_between_merge_and_acceptance_and_holds_claim():
    with loop(2, contexts=('deploy/examiner',), timeout_s=1) as run:
        snapshots = []
        run.fake.before_read = lambda: snapshots.append((run.issue(2).phase, run.queue_claimed()))
        run.run()
        assert snapshots and all(phase == Phase.DEPLOYING and held for phase, held in snapshots)
        assert run.waits(2) == (Phase.WAITING, 'deploy', 'deploying')
        assert run.awaited() == run.fake.commits[2]
        assert run.fake.issues[2]['state'] == 'open'
        assert not any(e['kind'] == 'deployment' for e in run.issue(2).evidence)
        assert not any(e.get('to') == 'done' for e in run.events('phase', 2))
