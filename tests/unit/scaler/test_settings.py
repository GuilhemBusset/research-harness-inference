"""The scaler's settings reader (`rhi_scaler.settings`)."""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_scaler.aws_io import PoolSettings
from rhi_scaler.settings import SettingsError, read_settings

# A configuration's pools table, with the mock pool.
CONFIGURATION = tomllib.loads(
    """
[pools.mock]
engine = "mock"
instance_types = ["t4g.small"]
prices = { "t4g.small" = 0.0168 }
max_instances = 1
warm_instances = 0
wake = "on-demand"
target_concurrency = 4
context_length = 32768
largest_output = 4096
"""
)
GROUP = "mock-group"
TARGET_GROUP = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/0123456789abcdef"
WORKSPACE = "https://aps.example.com/workspaces/ws-example/api/v1/remote_write"


def settings(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "pools": {
            name: {"entry": entry, "group": GROUP, "target_group": TARGET_GROUP}
            for name, entry in CONFIGURATION["pools"].items()
        },
        "heartbeat_table": "heartbeats-table",
        "pool_state_table": "pool-state-table",
        "workspace_url": WORKSPACE,
        # A field of a later issue: ignored here.
        "time_zone": "Etc/UTC",
    }
    data.update(changes)
    return {name: value for name, value in data.items() if value is not None}


def read(data: dict[str, Any]) -> Any:
    return read_settings({"RHI_SCALER_SETTINGS": json.dumps(data), "AWS_REGION": "us-east-1"})


def test_the_mock_pool_reads_back() -> None:
    result = read(settings())
    (pool,) = result.pools
    # The pool's configuration, its group and its target group, and nothing else: no mode.
    assert pool == PoolSettings(config=parse_pools(CONFIGURATION["pools"])[0], group=GROUP, target_group=TARGET_GROUP)
    assert (result.tables.heartbeats, result.tables.pool_state) == ("heartbeats-table", "pool-state-table")
    assert result.workspace_url == WORKSPACE
    assert result.region == "us-east-1"


@pytest.mark.parametrize("field", ["pool_state_table", "heartbeat_table", "workspace_url"])
def test_a_missing_field_is_refused_by_name(field: str) -> None:
    with pytest.raises(SettingsError, match=field) as refused:
        read(settings(**{field: None}))
    assert refused.value.field == field


def test_a_pool_whose_entry_does_not_parse_is_refused_naming_it() -> None:
    entry = {**CONFIGURATION["pools"]["mock"], "max_instances": "many"}
    with pytest.raises(SettingsError) as refused:
        read(settings(pools={"mock": {"entry": entry, "group": GROUP, "target_group": TARGET_GROUP}}))
    assert refused.value.field == "pools.mock.entry"
    assert "max_instances" in str(refused.value) and "many" not in str(refused.value)
    with pytest.raises(SettingsError) as refused:
        read(settings(pools={"mock": {"entry": CONFIGURATION["pools"]["mock"], "group": GROUP}}))
    assert refused.value.field == "pools.mock.target_group"


def test_the_longest_session_is_the_one_given_and_12_when_the_settings_hold_none() -> None:
    assert read(settings(session_max_hours=6)).session_max_hours == 6
    assert read(settings()).session_max_hours == 12
    for value in ("6", 0, 1.5, True):
        with pytest.raises(SettingsError) as refused:
            read(settings(session_max_hours=value))
        assert refused.value.field == "session_max_hours"


def test_no_settings_is_refused() -> None:
    with pytest.raises(SettingsError, match="RHI_SCALER_SETTINGS"):
        read_settings({"AWS_REGION": "us-east-1"})
