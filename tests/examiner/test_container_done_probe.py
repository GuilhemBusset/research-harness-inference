"""Independent checks of the owner fallback and terminal phase for issue 14."""

import pytest

from test_container_mapping_probe import JOB, RUNTIME, TEXT, L, assert_owner, scenario


def test_discharging_last_obligation_closes_issue_as_done():
    with scenario() as run:
        assert run.state.phase == L.model.Phase.DONE
        assert run.gh.issues[2]["state"] == "closed"
        assert run.summary["phases"] == {"done": 1}
        assert run.state.obligations == []
        assert run.state.human_tasks == []
        assert run.summary["owner_obligations"] == {}
        assert "- [ ]" not in run.body
