"""The scaler's settings: what its construct gives it in one environment variable.

The function's construct writes one JSON object to `RHI_SCALER_SETTINGS` (`SETTINGS_VARIABLE`):

```json
{
  "pools": {
    "<pool>": {"entry": {...}, "group": "<group name>", "target_group": "<target group ARN>"}
  },
  "heartbeat_table": "<table name>",
  "pool_state_table": "<table name>",
  "workspace_url": "https://.../api/v1/remote_write",
  "session_max_hours": 12
}
```

`entry` is the pool's entry as the configuration gives it, parsed again here with
`rhi_common.pool_config.parse_pools`, so the scaler holds the same typed pool as every other
reader. `workspace_url` is the Managed Prometheus workspace's remote-write address. The region is
the one Lambda sets in `AWS_REGION`. `session_max_hours` is the configuration's: the longest a
session may last, in hours, a whole number from 1 up, which is the bound every pool's session is
read under (`rhi_common.sessions`). Settings that hold none read as 12, the configuration's default.

A pool's mode when the pool-state table holds no mode record follows its `wake`: `enabled` for an
`on-demand` pool, `scheduled-only` for a `scheduled-only` one (section 11.3).

A refusal is a `SettingsError` that names the field, never its value. A key this reader does not
know is ignored: later issues add fields, each with a default, so that an older construct's
settings still read.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from rhi_common.pool_config import PoolConfigError, parse_pools
from rhi_common.records import CONFIGURED_MODE
from rhi_scaler.aws_io import PoolSettings, TableNames
from rhi_scaler.model import SESSION_MAX_HOURS

SETTINGS_VARIABLE = "RHI_SCALER_SETTINGS"
REGION_VARIABLE = "AWS_REGION"


class SettingsError(ValueError):
    """The settings were refused. `field` names what is wrong; the message never quotes a value."""

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(f"{SETTINGS_VARIABLE}: field {field!r} {problem}")
        self.field = field


@dataclass(frozen=True)
class Settings:
    """One run's settings: every pool, the two tables, the workspace, the region, and the longest
    a session may last, in hours."""

    pools: tuple[PoolSettings, ...]
    tables: TableNames
    workspace_url: str
    region: str
    session_max_hours: int = SESSION_MAX_HOURS


def _text(data: Mapping[str, object], field: str, where: str = "") -> str:
    name = f"{where}{field}"
    if field not in data:
        raise SettingsError(name, "is missing")
    value = data[field]
    if not isinstance(value, str) or not value:
        raise SettingsError(name, "must be a string that is not empty")
    return value


def _session_max_hours(data: Mapping[str, object]) -> int:
    value = data.get("session_max_hours", SESSION_MAX_HOURS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SettingsError("session_max_hours", "must be a whole number from 1 up")
    return value


def _pool(name: str, value: object) -> PoolSettings:
    where = f"pools.{name}."
    if not isinstance(value, Mapping):
        raise SettingsError(f"pools.{name}", "must be an object")
    entry = value.get("entry")
    if not isinstance(entry, Mapping):
        raise SettingsError(f"{where}entry", "is missing" if entry is None else "must be an object")
    try:
        # The price limit is the configuration's, checked when the stage loaded it: the settings do
        # not carry it, and the default would refuse a pool the deployment accepted.
        (config,) = parse_pools({name: entry}, on_demand_price_limit=math.inf)
    except PoolConfigError as error:
        raise SettingsError(f"{where}entry", f"does not parse: {error}") from None
    return PoolSettings(
        config=config,
        group=_text(value, "group", where),
        target_group=_text(value, "target_group", where),
        configured_mode=CONFIGURED_MODE[config.wake],
    )


def parse_settings(text: str, region: str) -> Settings:
    """The settings of a JSON text, for the region given."""
    try:
        data = json.loads(text)
    except ValueError:
        raise SettingsError(SETTINGS_VARIABLE, "is not JSON") from None
    if not isinstance(data, Mapping):
        raise SettingsError(SETTINGS_VARIABLE, "must be a JSON object")
    pools = data.get("pools")
    if not isinstance(pools, Mapping):
        raise SettingsError("pools", "is missing" if pools is None else "must be an object")
    tables = TableNames(heartbeats=_text(data, "heartbeat_table"), pool_state=_text(data, "pool_state_table"))
    workspace_url = _text(data, "workspace_url")
    if not workspace_url.startswith("https://"):
        raise SettingsError("workspace_url", "must be an https address")
    return Settings(
        pools=tuple(_pool(str(name), value) for name, value in pools.items()),
        tables=tables,
        workspace_url=workspace_url,
        region=region,
        session_max_hours=_session_max_hours(data),
    )


def read_settings(environ: Mapping[str, str]) -> Settings:
    """The settings of `RHI_SCALER_SETTINGS`, in the region of `AWS_REGION`."""
    text = environ.get(SETTINGS_VARIABLE)
    if not text:
        raise SettingsError(SETTINGS_VARIABLE, "is not set")
    region = environ.get(REGION_VARIABLE)
    if not region:
        raise SettingsError(REGION_VARIABLE, "is not set")
    return parse_settings(text, region)
