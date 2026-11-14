"""Independent checks of the owner fallback and terminal phase for issue 14."""

import pytest
from test_container_mapping_probe import JOB, RUNTIME, TEXT, assert_owner, scenario


@pytest.mark.parametrize("check", [
    None,
    {"id": 8123, "status": "completed", "conclusion": "skipped"},
    {"id": 8123, "status": "completed", "conclusion": "failure"},
    {"id": 8123, "status": "in_progress", "conclusion": None},
], ids=["absent", "skipped", "failure", "pending"])
def test_unsuccessful_job_keeps_owner_obligation(check):
    with scenario(check=check) as run:
        assert run.gh.reads == [(run.pr["head"]["sha"], JOB)]
        assert_owner(run, RUNTIME)
        assert f"- [ ] {TEXT}" in run.body
        assert "D2: needs host:container-runtime" in run.pr["body"]


