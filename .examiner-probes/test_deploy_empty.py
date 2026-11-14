import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


def test_empty_contexts_do_not_read_or_wait():
    with loop(2, contexts=(), timeout_s=0) as run:
        run.run()
        assert run.issue(2).phase == Phase.DONE
        assert not run.fake.reads and not run.events('waiting')
        assert not run.awaited() and not run.queue_claimed()
        assert len(run.fake.merges(2)) == 1
