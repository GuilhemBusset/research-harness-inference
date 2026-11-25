"""Books a pool for a check, and stops the booking it made (system design, section 8.2, step 6).

A check of the pipeline needs an instance of a pool in service: the verification on the mock pool
(`verify.py`), and after it the canary and the spike jobs. Each books a session of one instance,
tests, and stops the session. They book through this module, which fixes neither the pool nor the
name nor the length of a booking: its caller gives them.

**The table.** A pool's session is one item of the deployed stage's pool-state table: the pool's
name and the record `rhi_common.records.SESSION_RECORD`. `table_name` finds the table by its
logical ID in the pools stack, as the deployment hold does (`deploy_hold.py`). `book` and `stop`
take the table as a boto3 table resource.

**Booking.** `book` reads the pool's session and writes one with the rules of
`rhi_common.sessions.started`: one instance, from now, for the minutes it is given, under the name
it is given. It books only a pool that has no session booked or active: on any other it writes
nothing and returns `None`, and its caller tests the instances that are there. The write holds
only if the item still is what was read, no item or the same ended session, so a session someone
books between the read and the write is not written over: that too is `None`. A booking outside
the bounds of a session, longer than `session_max_hours`, is a `SessionError`.

**Stopping.** `stop` takes the session `book` returned. It reads the item again and ends it now,
by `rhi_common.sessions.stopped`, only while the item still reads as that booking: the same
instances, start, end, who and when. The write carries that as its condition. An item someone
changed, stopped or replaced meanwhile is theirs: it is left as it is, and so is a booking whose
end has already come.

Nothing here prints, waits or retries. An item that is not a session is a
`rhi_common.records.RecordError`, and an error of AWS other than a failed condition is raised.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from rhi_common.records import SESSION_RECORD, Session
from rhi_common.sessions import started, stopped

# The instances a check books.
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


def book(table: Any, pool: str, *, minutes: int, set_by: str, now: int, session_max_hours: int) -> Session | None:
    """Books one instance on the pool for `minutes` from the time `now`, under the name `set_by`:
    the session written, or `None` when the pool has a session booked or active, which is left as
    it is. A booking outside the bounds of a session is a `rhi_common.sessions.SessionError`."""
    current = read(table, pool)
    # `stopped` has something to write exactly when the pool has a session booked or active.
    if stopped(current, set_by=set_by, now=now, session_max_hours=session_max_hours) is not None:
        return None
    session = started(
        current,
        pool=pool,
        instances=INSTANCES,
        starts_at=now,
        ends_at=now + minutes * MINUTE_SECONDS,
        set_by=set_by,
        now=now,
        # One instance is within the bound of every pool: a pool's `max_instances` is at least 1.
        max_instances=INSTANCES,
        session_max_hours=session_max_hours,
    )
    return session if _written(table, session, current) else None


def stop(table: Any, booking: Session, *, now: int, session_max_hours: int) -> Session | None:
    """Stops the session `book` returned, at the time `now`: the session written, or `None` when
    nothing was, because the item no longer reads as that booking or its end has already come."""
    if read(table, booking.pool) != booking:
        return None
    ended = stopped(booking, set_by=booking.set_by, now=now, session_max_hours=session_max_hours)
    if ended is None:
        return None
    return ended if _written(table, ended, booking) else None
