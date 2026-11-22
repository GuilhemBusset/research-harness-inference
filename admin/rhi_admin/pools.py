"""The pools' operating state, which only the admin changes (system design, sections 5.1, 5.4 and 11.3).

A pool is in one of three modes: `enabled`, `scheduled-only` or `disabled`. Its mode is the record
`mode` of the pool-state table (`rhi_common.records.PoolState`), which the scaler and the gateway
read. No deployment writes it: a pool without one is in its configured mode, the one its `wake`
gives (`rhi_common.records.CONFIGURED_MODE`), and a record written here outlives every deployment.
Disabling a pool is the admin's kill switch, and nothing but these commands sets a pool back.
`parked` is a deployment flag, not a mode (section 11.2): nothing here writes it.

The commands:

- `status`: every deployed pool, its mode, who set it and when (none while the pool is in its
  configured mode), and whether the scaler has suspended its launches;
- `disable`: the mode `disabled`;
- `enable`: the pool's configured mode, `enabled` or `scheduled-only`;
- `clear_suspension`: sets back to false the mark `launch_suspended` of the pool's memory record,
  which the scaler sets after three launches abandoned in a row (section 5.4), and writes who
  cleared it and when beside it; the rest of the scaler's memory is left as it is, and the scaler
  never sets a mark back once it is cleared (`rhi_scaler.aws_io.memory_update`);
- `run_scaler`: asks the scaler for a run now, and gives each pool's decision.

Each change writes who made it, the caller's identity without its account (`identity_name`), and
when.

The deployed pools and their configured modes are read from the scaler's function, from the
settings its construct gave it (`RHI_SCALER_SETTINGS`): that is the configuration the running scaler
and gateway decide from, whereas the configuration's SSM parameters may already hold an edit that
the next deployment has not yet carried.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from rhi_admin.roster import AdminError
from rhi_common.pool_config import PoolConfigError, parse_pools
from rhi_common.records import CONFIGURED_MODE, MODE_RECORD, Mode, PoolState, RecordError
from rhi_common.tables import LAUNCH_SUSPENDED, MEMORY_RECORD

# The environment variable of the scaler's function that holds its settings (`rhi_scaler.settings`).
SCALER_SETTINGS = "RHI_SCALER_SETTINGS"
# What the memory record holds beside the mark, once the admin has cleared it.
CLEARED_BY = "cleared_by"
CLEARED_AT = "cleared_at"


@dataclass(frozen=True)
class Pools:
    """What the commands act on: the pool-state table (a boto3 table resource), a Lambda client, the
    deployed name of the scaler's function, and the name of whoever runs the command."""

    table: Any
    functions: Any
    scaler: str
    caller: Callable[[], str]


@dataclass(frozen=True)
class PoolStatus:
    """One pool as `status` shows it. `set_by` and `set_at` are `None` in the configured mode."""

    pool: str
    mode: Mode
    set_by: str | None
    set_at: int | None
    launch_suspended: bool


def identity_name(arn: str) -> str:
    """Who an identity's ARN names, without the account: `user/admin`, `assumed-role/Admin/session`."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or not parts[5]:
        raise AdminError("the caller's identity is not an ARN")
    return parts[5]


def configured_modes(pools: Pools) -> dict[str, Mode]:
    """Every deployed pool and its configured mode, from the settings of the scaler's function."""
    configuration = pools.functions.get_function_configuration(FunctionName=pools.scaler)
    text = configuration.get("Environment", {}).get("Variables", {}).get(SCALER_SETTINGS)
    try:
        entries = json.loads(text or "")["pools"]
        parsed = parse_pools({name: value["entry"] for name, value in entries.items()})
    except (ValueError, KeyError, TypeError, AttributeError, PoolConfigError):
        raise AdminError("the scaler's function does not hold the pools' settings") from None
    return {pool.name: CONFIGURED_MODE[pool.wake] for pool in parsed}


def _configured_mode(pool: str, pools: Pools) -> Mode:
    modes = configured_modes(pools)
    if pool not in modes:
        raise AdminError(f"no deployed pool is named {pool!r}")
    return modes[pool]


def status(pools: Pools) -> list[PoolStatus]:
    """Every deployed pool's state, by name."""
    found = []
    for pool, configured in sorted(configured_modes(pools).items()):
        answer = pools.table.query(
            KeyConditionExpression="#pool = :pool",
            ExpressionAttributeNames={"#pool": "pool"},
            ExpressionAttributeValues={":pool": pool},
            ConsistentRead=True,
        )
        records = {item.get("record"): item for item in answer.get("Items", [])}
        memory = records.get(MEMORY_RECORD) or {}
        suspended = memory.get(LAUNCH_SUSPENDED) is True
        if MODE_RECORD in records:
            try:
                state = PoolState.from_item(records[MODE_RECORD])
            except RecordError as error:
                raise AdminError(f"the mode record of the pool {pool!r} does not read: {error}") from None
            found.append(PoolStatus(pool, state.mode, state.set_by, state.set_at, suspended))
        else:
            found.append(PoolStatus(pool, configured, None, None, suspended))
    return found


def _set_mode(pool: str, mode: Mode, pools: Pools, now: int) -> PoolState:
    state = PoolState(pool=pool, mode=mode, set_by=pools.caller(), set_at=now)
    pools.table.put_item(Item=state.to_item())
    return state


def disable(pool: str, *, pools: Pools, now: int) -> PoolState:
    """The pool's mode set to `disabled`: never woken, its idle instances released at once."""
    _configured_mode(pool, pools)
    return _set_mode(pool, Mode.DISABLED, pools, now)


def enable(pool: str, *, pools: Pools, now: int) -> PoolState:
    """The pool's mode set back to its configured one."""
    return _set_mode(pool, _configured_mode(pool, pools), pools, now)


def clear_suspension(pool: str, *, pools: Pools, now: int) -> str:
    """The mark `launch_suspended` set back to false, with who cleared it and when; who that was.

    Refused when the pool's launches are not suspended. One conditional update of the memory
    record: no other attribute of it changes.
    """
    _configured_mode(pool, pools)
    who = pools.caller()
    try:
        pools.table.update_item(
            Key={"pool": pool, "record": MEMORY_RECORD},
            UpdateExpression="SET #mark = :cleared, #by = :by, #at = :at",
            ConditionExpression="#mark = :set",
            ExpressionAttributeNames={"#mark": LAUNCH_SUSPENDED, "#by": CLEARED_BY, "#at": CLEARED_AT},
            ExpressionAttributeValues={":cleared": False, ":set": True, ":by": who, ":at": now},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise AdminError(f"the launches of the pool {pool!r} are not suspended") from None
        raise
    return who


def run_scaler(*, pools: Pools) -> list[dict[str, Any]]:
    """One run of the scaler now, and what it decided for each pool: `pool`, `desired`, `reason`."""
    try:
        answer = pools.functions.invoke(FunctionName=pools.scaler, InvocationType="RequestResponse", Payload=b"{}")
    except ClientError as error:
        # Its reserved concurrency is 1: a run under way refuses a second one.
        if error.response.get("Error", {}).get("Code") == "TooManyRequestsException":
            raise AdminError("the scaler is already running; its next run is within a minute") from None
        raise
    if answer.get("FunctionError"):
        raise AdminError("the scaler's run failed; its log group /rhi/scaler says why")
    return json.loads(answer["Payload"].read())["pools"]
