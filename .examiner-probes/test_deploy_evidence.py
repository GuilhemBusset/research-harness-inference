import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import L, Phase, loop


def test_each_success_is_in_state_and_published_evidence():
    contexts = ('deploy/examiner', 'verify/examiner')
    with loop(2, contexts=contexts) as run:
        run.fake.posts[2] = ['success']
        run.run()
        st = run.issue(2)
        commit = run.fake.commits[2][0]
        assert st.phase == Phase.DONE
        records = [record for record in st.evidence if record['kind'] == 'deployment']
        assert len(records) == 1 and records[0]['passed']
        assert records[0]['head_sha'] == commit
        assert records[0]['contexts'] == dict.fromkeys(contexts, 'success')
        text = run.comment(st.pr, L.report.EVIDENCE_MARKER)
        assert commit[:12] in text and all(context in text for context in contexts)
