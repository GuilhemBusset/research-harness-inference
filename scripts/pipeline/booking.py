"""Books a pool for a check, and stops the booking it made (system design, section 8.2, step 6).

A check of the pipeline needs an instance of a pool in service: the verification on the mock pool
(`verify.py`), and after it the canary and the spike jobs. Each books a session, of one instance
unless it asks for more, tests, and stops the session. They book through this module, which fixes
neither the pool nor the name nor the length of a booking: its caller gives them.

**The table.** A pool's session is one item of the deployed stage's pool-state table: the pool's
name and the record `rhi_common.records.SESSION_RECORD`. `table_name` finds the table by its
logical ID in the pools stack, as the deployment hold does (`deploy_hold.py`). `book` and `stop`
take the table as a boto3 table resource.

**Booking.** `book` reads the pool's session and writes one with the rules of
`rhi_common.sessions.started`: one instance, from now, for the minutes it is given, under the name
it is given. A caller that needs more instances gives their number and the pool's `max_instances`.
It books only a pool that has no session booked or active: on any other it writes nothing and
returns `None`, and its caller tests the instances that are there. The write holds only if the
item still is what was read, no item or the same ended session, so a session someone books between
the read and the write is not written over: that too is `None`. A booking outside the bounds of a
session, longer than `session_max_hours` or of more instances than `max_instances`, is a
`SessionError`.

**Changing.** `changed` takes the session `book` returned and gives it another number of
instances, by `rhi_common.sessions.changed`: the same start, end and name, written now. It reads
the item again and writes only while the item still reads as that booking: the same instances,
start, end, who and when. The write carries that as its condition. It returns the session it
wrote, which is the booking from then on: the one to change again, or to stop. A number outside
1 to `max_instances` is a `SessionError`.

**Stopping.** `stop` takes the session `book` or `changed` returned. It reads the item again and
ends it now, by `rhi_common.sessions.stopped`, under the same condition.

An item someone changed, stopped or replaced meanwhile is theirs: `changed` and `stop` leave it
as it is and return `None`, as they do for a booking whose end has already come.

Nothing here prints, waits or retries. An item that is not a session is a
`rhi_common.records.RecordError`, and an error of AWS other than a failed condition is raised.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from rhi_common import sessions
from rhi_common.records import SESSION_RECORD, Session

# The instances a check books unless it asks for more.
INSTANCES = 1
MINUTE_SECONDS = 60

# What compares an item with the session that was read: each attribute of the record but its key.
_COMPARED = {"#instances": "instances", "#starts": "starts_at", "#ends": "ends_at", "#by": "set_by", "#at": "set_at"}


def table_name(cloudformation: Any, stack: str, logical_id: str) -> str:
    """The name of the deployed table of that logical ID in the stack, read with `cloudformation`,
    a CloudFormation client of the AWS SDK."""
    answer = cloudformation.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)
    return answer["StackResourceDetail"]["PhysicalResourceId"]


def read(table: Any, pool: str) -> Session | None:
    """The pool's session as it is stored, `None` when the pool has no session item."""
    item = table.get_item(Key={"pool": pool, "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


def _still(session: Session | None) -> dict[str, Any]:
    """The condition of a write that the item still is `session`: no item, or one that reads the same."""
    if session is None:
        return {"ConditionExpression": "attribute_not_exists(#pool)", "ExpressionAttributeNames": {"#pool": "pool"}}
    item = session.to_item()
    return {
        "ConditionExpression": " AND ".join(f"{name} = :{attribute}" for name, attribute in _COMPARED.items()),
        "ExpressionAttributeNames": dict(_COMPARED),
        "ExpressionAttributeValues": {f":{attribute}": item[attribute] for attribute in _COMPARED.values()},
    }


def _written(table: Any, session: Session, current: Session | None) -> bool:
    """Writes the session while the item still is `current`; whether it was written."""
    try:
        table.put_item(Item=session.to_item(), **_still(current))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False
    return True


def book(
    table: Any,
    pool: str,
    *,
    minutes: int,
    set_by: str,
    now: int,
    session_max_hours: int,
    instances: int = INSTANCES,
    # One instance is within the bound of every pool: a pool's `max_instances` is at least 1. A
    # caller that books more gives the pool's.
    max_instances: int = INSTANCES,
) -> Session | None:
    """Books `instances` instances on the pool, one unless given, for `minutes` from the time `now`,
    under the name `set_by`: the session written, or `None` when the pool has a session booked or
    active, which is left as it is. A booking outside the bounds of a session, `max_instances` and
    `session_max_hours`, is a `rhi_common.sessions.SessionError`."""
    current = read(table, pool)
    # `stopped` has something to write exactly when the pool has a session booked or active.
    if sessions.stopped(current, set_by=set_by, now=now, session_max_hours=session_max_hours) is not None:
        return None
    session = sessions.started(
        current,
        pool=pool,
        instances=instances,
        starts_at=now,
        ends_at=now + minutes * MINUTE_SECONDS,
        set_by=set_by,
        now=now,
        max_instances=max_instances,
        session_max_hours=session_max_hours,
    )
    return session if _written(table, session, current) else None


def changed(
    table: Any, booking: Session, *, instances: int, max_instances: int, now: int, session_max_hours: int
) -> Session | None:
    """Gives the session `book` returned another number of instances, at the time `now`: the
    session written, which is the booking from then on, or `None` when nothing was, because the
    item no longer reads as that booking or its end has already come. A number outside 1 to
    `max_instances` is a `rhi_common.sessions.SessionError`."""
    if read(table, booking.pool) != booking or now >= sessions.end(booking, session_max_hours=session_max_hours):
        return None
    wanted = sessions.changed(
        booking,
        set_by=booking.set_by,
        now=now,
        max_instances=max_instances,
        session_max_hours=session_max_hours,
        instances=instances,
    )
    return wanted if _written(table, wanted, booking) else None


def stop(table: Any, booking: Session, *, now: int, session_max_hours: int) -> Session | None:
    """Stops the session `book` or `changed` returned, at the time `now`: the session written, or
    `None` when nothing was, because the item no longer reads as that booking or its end has
    already come."""
    if read(table, booking.pool) != booking:
        return None
    ended = sessions.stopped(booking, set_by=booking.set_by, now=now, session_max_hours=session_max_hours)
    if ended is None:
        return None
    return ended if _written(table, ended, booking) else None
