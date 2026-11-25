"""The pools' suspended launches, and the scaler's runs (system design, sections 5.1, 5.4 and 11.3).

A pool has no mode: its session is the one operating state (section 5.6, `rhi_admin.session`), and
a pool that should not run is one nobody books. What is left here is the one thing beside the
session that the admin sets back: the mark `launch_suspended` of the pool's memory record in the
pool-state table, which the scaler sets after three launches abandoned in a row (section 5.4).
`parked` is a deployment flag (section 11.2): nothing here writes it.

The commands:

- `status`: every deployed pool, and whether the scaler has suspended its launches;
- `clear_suspension`: sets back to false the mark `launch_suspended` of the pool's memory record,
  and writes who cleared it and when beside it; the rest of the scaler's memory is left as it is,
  and the scaler never sets a mark back once it is cleared (`rhi_scaler.aws_io.memory_update`);
- `run_scaler`: asks the scaler for a run now, and gives each pool's decision. A run under way
  refuses a second one: that refusal is a `ScalerBusy`, which the commands on a pool's session
  (`rhi_admin.session`) report without failing. A request that Lambda throttles for another
  reason fails as any other error does.

A suspension cleared carries who cleared it, the caller's identity without its account
(`identity_name`), and when.

The deployed pools and their `max_instances` are read from the scaler's function, from the settings
its construct gave it (`RHI_SCALER_SETTINGS`): that is the configuration the running scaler and
gateway decide from, whereas the configuration's SSM parameters may already hold an edit that the
next deployment has not yet carried. A parked stage keeps the table and holds no scaler (section
11.2): no pool is deployed then, `status` has no line, and a command that needs a pool or the
scaler is refused.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from rhi_admin.roster import AdminError
from rhi_common.pool_config import Pool, PoolConfigError, parse_pools
from rhi_common.tables import LAUNCH_SUSPENDED, MEMORY_RECORD

# The environment variable of the scaler's function that holds its settings (`rhi_scaler.settings`).
SCALER_SETTINGS = "RHI_SCALER_SETTINGS"
# What the memory record holds beside the mark, once the admin has cleared it.
CLEARED_BY = "cleared_by"
CLEARED_AT = "cleared_at"
# What refuses a command that needs a pool or the scaler when the pools stack holds no scaler.
NO_SCALER = "no pool is deployed: the pools stack holds no scaler, as while the endpoint is parked"


@dataclass(frozen=True)
class Pools:
    """What the commands act on: the pool-state table (a boto3 table resource), a Lambda client, the
    deployed name of the scaler's function, and the name of whoever runs the command. `scaler` is
    `None` when the pools stack holds no scaler, as while the endpoint is parked: no pool is
    deployed then, and the table is still there."""

    table: Any
    functions: Any
    scaler: str | None
    caller: Callable[[], str]


# Why Lambda refuses an invocation while the function's reserved concurrency is all in use. Its
# other reasons for the same error are limits on the rate of requests, not a run under way.
RUN_UNDER_WAY = "ReservedFunctionConcurrentInvocationLimitExceeded"


class ScalerBusy(AdminError):
    """The scaler was asked for a run while one is under way: its next run is within a minute."""


@dataclass(frozen=True)
class PoolStatus:
    """One pool as `status` shows it: whether the scaler has suspended its launches."""

    pool: str
    launch_suspended: bool


def identity_name(arn: str) -> str:
    """Who an identity's ARN names, without the account: `user/admin`, `assumed-role/Admin/session`."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or not parts[5]:
        raise AdminError("the caller's identity is not an ARN")
    return parts[5]


def _deployed(pools: Pools) -> tuple[Pool, ...]:
    """Every deployed pool, from the settings of the scaler's function.

    The deployment checked the prices of a pool that wakes on demand against the configuration's
    limit, which the settings do not carry: they are not checked again here, so that a command
    always reaches a deployed pool. With no scaler deployed there is none.
    """
    if pools.scaler is None:
        return ()
    configuration = pools.functions.get_function_configuration(FunctionName=pools.scaler)
    text = configuration.get("Environment", {}).get("Variables", {}).get(SCALER_SETTINGS)
    try:
        entries = json.loads(text or "")["pools"]
        return parse_pools({name: value["entry"] for name, value in entries.items()}, on_demand_price_limit=math.inf)
    except (ValueError, KeyError, TypeError, AttributeError, PoolConfigError):
        raise AdminError("the scaler's function does not hold the pools' settings") from None


def max_instances(pools: Pools) -> dict[str, int]:
    """Every deployed pool and its `max_instances`, the most a session may book of it, from the
    settings of the scaler's function."""
    return {pool.name: pool.max_instances for pool in _deployed(pools)}


def not_deployed(pool: str, pools: Pools) -> AdminError:
    """The refusal of a command on a pool that is not deployed, which says so when none is."""
    if pools.scaler is None:
        return AdminError(NO_SCALER)
    return AdminError(f"no deployed pool is named {pool!r}")


def status(pools: Pools) -> list[PoolStatus]:
    """Every deployed pool, by name, and whether its launches are suspended: the mark of its memory
    record, which reads as not suspended while the scaler has written none."""
    found = []
    for pool in sorted(max_instances(pools)):
        memory = pools.table.get_item(Key={"pool": pool, "record": MEMORY_RECORD}, ConsistentRead=True).get("Item")
        found.append(PoolStatus(pool, (memory or {}).get(LAUNCH_SUSPENDED) is True))
    return found


def clear_suspension(pool: str, *, pools: Pools, now: int) -> str:
    """The mark `launch_suspended` set back to false, with who cleared it and when; who that was.

    Refused when the pool's launches are not suspended. One conditional update of the memory
    record: no other attribute of it changes.
    """
    if pool not in max_instances(pools):
        raise not_deployed(pool, pools)
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
    if pools.scaler is None:
        raise AdminError(NO_SCALER)
    try:
        answer = pools.functions.invoke(FunctionName=pools.scaler, InvocationType="RequestResponse", Payload=b"{}")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TooManyRequestsException":
            raise
        # Its reserved concurrency is 1: a run under way refuses a second one, and Lambda names that
        # limit as the reason. Any other reason (the rate of the requests) says nothing of a run.
        if error.response.get("Reason") == RUN_UNDER_WAY:
            raise ScalerBusy("the scaler is already running; its next run is within a minute") from None
        raise AdminError("Lambda throttled the request for a run of the scaler, which did not run for it; its next run is within a minute") from None
    if answer.get("FunctionError"):
        raise AdminError("the scaler's run failed; its log group /rhi/scaler says why")
    return json.loads(answer["Payload"].read())["pools"]
