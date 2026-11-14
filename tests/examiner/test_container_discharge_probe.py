import pytest

from test_container_mapping_probe import JOB, TEXT, scenario


@pytest.mark.parametrize("prefix", ["", "./"], ids=["plain-path", "dot-relative-path"])
def test_success_proves_a_test_under_container_folder(prefix):
    text = TEXT.replace("`tests/", f"`{prefix}tests/")
    with scenario(text=text) as run:
        assert run.gh.reads == [(run.pr["head"]["sha"], JOB)]
        records = [e for e in run.state.evidence if e["criterion"] == "D2"]
        assert len(records) == 1
        assert records[0]["passed"] is True
        assert records[0]["head_sha"] == run.pr["head"]["sha"]
        address = "https://github.test/runs/8123"
        assert records[0]["address"] == address
        assert f"([evidence]({address}))" in run.body
        assert not run.state.obligations and not run.state.human_tasks
        assert run.summary["owner_obligations"] == {}
        assert "D2: needs" not in run.pr["body"]
