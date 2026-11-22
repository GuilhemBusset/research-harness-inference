"""Holds the stage's deployment while a session is on (system design, sections 5.6 and 8.2).

There is one environment: a deployment that touches a pool refreshes its instances, and a gateway
deployment drains for up to 30 minutes, so a deployment during a class would cost the students
their warm instances (sections 4.3 and 8.5). The job reads the session switch, the item
`rhi_common.tables.SESSION_SWITCH_KEY` of the deployed stage's pool-state table, as
`rhi_common.records.session_is_on` reads it, and exits 0 once it reads off.

**How it finds the table.** By the table's logical ID in the pools stack, as the admin CLI finds its
tables (`admin/rhi_admin/main.py`): `cloudformation:DescribeStackResource` gives the table's name.
The stack's name and the logical ID come from `RHI_HOLD_POOLS_STACK` and
`RHI_HOLD_TABLE_LOGICAL_ID`, which the step takes from the stage as text (`src/rhi/pipeline/hold.py`).

**When it passes at once.** When the switch reads off, when its item is missing (which reads off),
and when the pools stack, or its table, does not exist yet, as before the stage's first deployment:
CloudFormation then answers `ValidationError` saying that the stack or the resource does not
exist, or names the resource with no physical ID (its creation failed before the table existed),
and DynamoDB answers `ResourceNotFoundException`. Any other error, a `ValidationError` for another
cause among them, and a switch whose item does not read, fail the job: the hold is never released
without the switch read off.

**While it reads on**, the job reads it again every `INTERVAL` seconds, until it reads off. The
step's timeout, CodeBuild's limit for one build, ends a hold on a session left on for a day and a
half (section 17).

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

from rhi_common.records import RecordError, SessionSwitch, session_is_on
from rhi_common.tables import SESSION_SWITCH_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

NAME = "deploy_hold"
STACK_VARIABLE = "RHI_HOLD_POOLS_STACK"
LOGICAL_ID_VARIABLE = "RHI_HOLD_TABLE_LOGICAL_ID"
# Seconds between two reads of a switch that reads on.
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


def read_switch(table: Any) -> SessionSwitch | None:
    """The switch as it is stored, `None` when it has no item; `NotDeployed` when the table does not exist."""
    try:
        item = table.get_item(Key=SESSION_SWITCH_KEY, ConsistentRead=True).get("Item")
    except ClientError as error:
        if _code(error) == "ResourceNotFoundException":
            raise NotDeployed from None
        raise
    return None if item is None else SessionSwitch.from_item(item)


def hold(
    stack: str,
    logical_id: str,
    *,
    cloudformation: Any,
    dynamodb: Any,
    sleep: Callable[[float], None],
) -> None:
    """Returns once the switch reads off, its item is missing, or the stack or the table does not
    exist; while it reads on, reads it again every `INTERVAL` seconds."""
    try:
        table = dynamodb.Table(table_name(cloudformation, stack, logical_id))
        while session_is_on(read_switch(table)):
            print(f"{NAME}: a session is on: the deployment waits, and the switch is read again in {INTERVAL} s")
            sleep(INTERVAL)
    except NotDeployed:
        print(f"{NAME}: the stack {stack} or its pool-state table does not exist yet: no session can be on")
        return
    print(f"{NAME}: no session is on: the deployment goes ahead")


def main(
    environ: Mapping[str, str] | None = None,
    *,
    cloudformation: Any = None,
    dynamodb: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """0 once no session is on. `cloudformation` is a client and `dynamodb` a resource of the AWS
    SDK; without them, the build's own credentials and region make them. `sleep` waits that many
    seconds."""
    env = os.environ if environ is None else environ
    stack, logical_id = env.get(STACK_VARIABLE), env.get(LOGICAL_ID_VARIABLE)
    if not stack or not logical_id:
        print(f"{NAME}: {STACK_VARIABLE} and {LOGICAL_ID_VARIABLE} name the pool-state table", file=sys.stderr)
        return 1
    if cloudformation is None or dynamodb is None:
        import boto3

        cloudformation = cloudformation or boto3.client("cloudformation")
        dynamodb = dynamodb or boto3.resource("dynamodb")
    try:
        hold(stack, logical_id, cloudformation=cloudformation, dynamodb=dynamodb, sleep=sleep)
    except RecordError as error:
        print(f"{NAME}: the session switch does not read: {error}", file=sys.stderr)
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
