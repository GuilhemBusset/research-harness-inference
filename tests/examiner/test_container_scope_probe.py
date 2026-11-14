"""Independent checks of the owner fallback and terminal phase for issue 14."""

import pytest
from test_container_mapping_probe import RUNTIME, assert_owner, scenario


@pytest.mark.parametrize("path", ["tests/unit/test_service.py", "tests/container/",
                                      "vendor/tests/container/test_service.py"])
def test_success_without_a_container_test_keeps_owner_obligation(path):
    text = f"`{path}` passes (needs host:container-runtime)."
    with scenario(text=text) as run:
        assert run.gh.reads == []
        assert_owner(run, RUNTIME)
        assert f"- [ ] {text}" in run.body


