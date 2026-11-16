"""The configuration fetch (scripts/pipeline/fetch_config.py): the configuration in SSM is written
to the file RHI_CONFIG_FILE names, which the `file` source reads back, and no value is printed.
A stand-in for the SSM client replaces AWS."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import fetch_config
from rhi import config

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

PLANTED = dataclasses.replace(
    config.load({"RHI_CONFIG": "example"}),
    domain="planted-fetch.example.com",
    hosted_zone_id="Z0PLANTED0FETCH0ZONE",
    admin_email="planted-fetch@example.com",
    instructor_phone="+15555550198",
    login_client_id="Iv1.0000planted0fetch",
)


class StandIn:
    """An SSM client as far as the loader and its writer use one: one page, every parameter."""

    def __init__(self) -> None:
        self.parameters: dict[str, dict[str, Any]] = {}

    def put_parameter(self, **request: Any) -> None:
        self.parameters[request["Name"]] = {"Name": request["Name"], "Type": request["Type"], "Value": request["Value"]}

    def get_parameters_by_path(self, **request: Any) -> dict[str, Any]:
        assert request["Path"] == config.SSM_PREFIX
        return {"Parameters": list(self.parameters.values())}


def test_the_configuration_is_written_to_the_file_and_no_value_is_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ssm = StandIn()
    config.write_ssm(PLANTED, client=ssm)
    path = tmp_path / "build" / "config.toml"

    assert fetch_config.main({"RHI_CONFIG_FILE": str(path)}, client=ssm) == 0

    assert config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(path)}) == PLANTED
    printed = "".join(capsys.readouterr())
    for key in config.KEYS:
        value = getattr(PLANTED, key)
        if isinstance(value, str):
            assert value not in printed, key


def test_no_file_named_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert fetch_config.main({}, client=StandIn()) != 0
    assert "RHI_CONFIG_FILE" in capsys.readouterr().err
