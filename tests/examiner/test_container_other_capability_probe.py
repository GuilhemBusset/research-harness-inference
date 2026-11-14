"""Independent checks of the owner fallback and terminal phase for issue 14."""

import pytest

from test_container_mapping_probe import RUNTIME, TEXT, assert_owner, scenario


@pytest.mark.parametrize("check", [
    {"id": 8123, "status": "completed", "conclusion": "success"},
    None,
    {"id": 8123, "status": "completed", "conclusion": "skipped"},
], ids=["success", "absent", "skipped"])
def test_aws_capability_stays_with_owner(check):
    capability = "aws:deployed-environment"
    text = TEXT.replace(RUNTIME, capability)
    with scenario(text=text, capability=capability, check=check) as run:
        assert run.gh.reads == []
        assert_owner(run, capability)
        assert f"- [ ] {text}" in run.body


