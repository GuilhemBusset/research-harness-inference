"""Holds the stage's deployment while a session is active on any pool, or the session switch is on
(system design, sections 5.6 and 8.2).

There is one environment: a deployment that touches a pool refreshes its instances, and a gateway
deployment drains for up to 30 minutes, so a deployment during a class or a measurement would cost
it its warm instances (sections 4.3 and 8.5). The job reads two things from the deployed stage's
pool-state table, and exits 0 once neither holds the deployment:

- the session of every pool `RHI_HOLD_POOLS` names, the item of the pool's name and the record
  `rhi_common.records.SESSION_RECORD`. A session holds the deployment while it is active, as
  `rhi_common.sessions.is_active` reads it at the time of the read, with `RHI_HOLD_SESSION_MAX_HOURS`
  as the longest a session lasts. The job does not ask whose session it is. A pool with no session
  item has no session, and a session that is booked and has not started holds nothing;
- the session switch, the item `rhi_common.tables.SESSION_SWITCH_KEY`, as
  `rhi_common.records.session_is_on` reads it. It is read until its record is removed.

**What the step tells it** (`src/rhi/pipeline/hold.py`), as text. `RHI_HOLD_POOLS_STACK` and
`RHI_HOLD_TABLE_LOGICAL_ID` name the pools stack and the table's logical ID in it: the job finds the
table as the admin CLI finds its tables (`admin/rhi_admin/main.py`),
`cloudformation:DescribeStackResource` gives the table's name. `RHI_HOLD_POOLS` holds the names of
the stage's pools, separated by commas: empty for a stage with no pool, whose deployment then waits
on the switch alone. `RHI_HOLD_SESSION_MAX_HOURS` is the configuration's `session_max_hours`, a
whole number above zero. A variable that is missing, a pool's name that is empty beside others, or
hours that are not such a number fail the job before it reads anything.

**When it passes at once.** When no session is active and the switch reads off, which is also how
a missing item reads, and when the pools stack, or its table, does not exist yet, as before the
stage's first deployment: CloudFormation then answers `ValidationError` saying that the stack or
the resource does not exist, or names the resource with no physical ID (its creation failed before
the table existed), and DynamoDB answers `ResourceNotFoundException`. Any other error, a
`ValidationError` for another cause among them, and an item that is not a session or a switch that
does not read, fail the job: the hold is never released without every pool's session and the
switch read.

**While it holds**, the job reads every item again every `INTERVAL` seconds. A session that ends,
or is stopped, releases the hold at the next read. The step's timeout, CodeBuild's limit for one
build, ends a hold that lasts a day and a half (section 17).

It prints its own lines, never a message of AWS. Run by the pipeline's step `Hold`, with PYTHONPATH
holding the import path of `pyproject.toml`:

    uv run --locked python scripts/pipeline/deploy_hold.py
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from rhi_common.records import SESSION_RECORD, RecordError, Session, SessionSwitch, session_is_on
from rhi_common.sessions import is_active
from rhi_common.tables import SESSION_SWITCH_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

NAME = "deploy_hold"
STACK_VARIABLE = "RHI_HOLD_POOLS_STACK"
LOGICAL_ID_VARIABLE = "RHI_HOLD_TABLE_LOGICAL_ID"
# The names of the stage's pools, separated by commas, and the longest a session lasts, in hours.
POOLS_VARIABLE = "RHI_HOLD_POOLS"
SESSION_MAX_HOURS_VARIABLE = "RHI_HOLD_SESSION_MAX_HOURS"
# Seconds between two reads of the sessions and the switch while one of them holds the deployment.
INTERVAL = 60


class NotDeployed(Exception):
    """The pools stack, or its pool-state table, does not exist yet."""


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _missing_stack_or_resource(error: ClientError) -> bool:
    """Whether CloudFormation's error says that the stack or the resource asked for does not exist:
    a `ValidationError` whose message says so. Any other `ValidationError` is a lookup that failed."""
    message = error.response.get("Error", {}).get("Message", "")
    return _code(error) == "ValidationError" and isinstance(message, str) and "does not exist" in message


def table_name(cloudformation: Any, stack: str, logical_id: str) -> str:
    """The name of the deployed table of that logical ID in the stack; `NotDeployed` when the stack
    or the table does not exist."""
    try:
        answer = cloudformation.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)
    except ClientError as error:
        if _missing_stack_or_resource(error):
            raise NotDeployed from None
        raise
    # A resource whose creation failed before the table existed has no physical ID: no table yet.
    name = answer["StackResourceDetail"].get("PhysicalResourceId")
    if not name:
        raise NotDeployed
    return name


def _item(table: Any, key: Mapping[str, str]) -> Any:
    """The item of that key, `None` when there is none; `NotDeployed` when the table does not exist."""
    try:
        return table.get_item(Key=dict(key), ConsistentRead=True).get("Item")
    except ClientError as error:
        if _code(error) == "ResourceNotFoundException":
            raise NotDeployed from None
        raise


def read_switch(table: Any) -> SessionSwitch | None:
    """The switch as it is stored, `None` when it has no item; `NotDeployed` when the table does not exist."""
    item = _item(table, SESSION_SWITCH_KEY)
    return None if item is None else SessionSwitch.from_item(item)


def read_session(table: Any, pool: str) -> Session | None:
    """The pool's session as it is stored, `None` when the pool has no session item; `NotDeployed`
    when the table does not exist."""
    item = _item(table, {"pool": pool, "record": SESSION_RECORD})
    return None if item is None else Session.from_item(item)


def hold(
    stack: str,
    logical_id: str,
    *,
    pools: Sequence[str],
    session_max_hours: int,
    cloudformation: Any,
    dynamodb: Any,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Returns once the switch reads off and no session of `pools` is active, or when the stack or
    the table does not exist; until then, reads them all again every `INTERVAL` seconds."""
    try:
        table = dynamodb.Table(table_name(cloudformation, stack, logical_id))
        while True:
            on = session_is_on(read_switch(table))
            # Every pool is read, whatever the switch reads: an item that is not a session fails the job.
            sessions = [read_session(table, pool) for pool in pools]
            now = int(clock())
            active = [
                pool
                for pool, session in zip(pools, sessions, strict=True)
                if is_active(session, now, session_max_hours=session_max_hours)
            ]
            if not on and not active:
                break
            held_by = [f"a session is active on {', '.join(active)}"] if active else []
            held_by += ["the session switch reads on"] if on else []
            print(f"{NAME}: {' and '.join(held_by)}: the deployment waits, and reads again in {INTERVAL} s")
            sleep(INTERVAL)
    except NotDeployed:
        print(f"{NAME}: the stack {stack} or its pool-state table does not exist yet: no session can be active")
        return
    print(f"{NAME}: no session is active and the switch reads off: the deployment goes ahead")


def _pools(text: str | None) -> list[str] | None:
    """The pools the variable names: none when it is empty. `None` when it is missing, or names a pool by nothing."""
    if text is None:
        return None
    pools = text.split(",") if text else []
    return pools if all(pools) else None


def _hours(text: str | None) -> int | None:
    """The whole number above zero the variable holds, `None` when it is missing or holds anything else."""
    return int(text) if text is not None and text.isascii() and text.isdigit() and int(text) > 0 else None


def main(
    environ: Mapping[str, str] | None = None,
    *,
    cloudformation: Any = None,
    dynamodb: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    """0 once no session is active and the switch reads off. `cloudformation` is a client and
    `dynamodb` a resource of the AWS SDK; without them, the build's own credentials and region make
    them. `sleep` waits that many seconds, and `clock` gives the time, in seconds since the epoch."""
    env = os.environ if environ is None else environ
    stack, logical_id = env.get(STACK_VARIABLE), env.get(LOGICAL_ID_VARIABLE)
    if not stack or not logical_id:
        print(f"{NAME}: {STACK_VARIABLE} and {LOGICAL_ID_VARIABLE} name the pool-state table", file=sys.stderr)
        return 1
    pools, session_max_hours = _pools(env.get(POOLS_VARIABLE)), _hours(env.get(SESSION_MAX_HOURS_VARIABLE))
    if pools is None or session_max_hours is None:
        print(
            f"{NAME}: {POOLS_VARIABLE} names the stage's pools, separated by commas, and "
            f"{SESSION_MAX_HOURS_VARIABLE} is a whole number of hours above zero",
            file=sys.stderr,
        )
        return 1
    if cloudformation is None or dynamodb is None:
        import boto3

        cloudformation = cloudformation or boto3.client("cloudformation")
        dynamodb = dynamodb or boto3.resource("dynamodb")
    try:
        hold(
            stack,
            logical_id,
            pools=pools,
            session_max_hours=session_max_hours,
            cloudformation=cloudformation,
            dynamodb=dynamodb,
            sleep=sleep,
            clock=clock,
        )
    except RecordError as error:
        print(f"{NAME}: a pool's session or the session switch does not read: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        print(f"{NAME}: a read failed ({_code(error)}): the deployment is not released", file=sys.stderr)
        return 1
    except BotoCoreError:
        print(f"{NAME}: AWS could not be reached: the deployment is not released", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
