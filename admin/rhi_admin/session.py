"""The session switch, which only the admin sets (system design, section 5.6).

One item of the pool-state table says whether a session is on: `rhi_common.records.SessionSwitch`,
under the keys `rhi_common.tables.SESSION_SWITCH_KEY`, which no pool's name can take. The gateway
serves students only while it reads on, and the pipeline's deployment waits while it does. No
deployment writes it, and a missing item reads as off.

The commands:

- `status`: whether the session is on, and who last switched it and when;
- `turn`: the session on or off, with who and when. Switching it to what it already reads writes
  nothing (`rhi_common.records.switched`), so the time an off switch carries is the time the last
  session ended. The write holds only if the switch still reads what was read before it: when two
  admins switch it at once, the first transition keeps its who and when, and the other command
  reads it again and finds it already so.

Who switched it is the caller's identity without its account (`rhi_admin.pools.identity_name`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from rhi_admin.roster import AdminError
from rhi_common.records import RecordError, SessionSwitch, switched
from rhi_common.tables import SESSION_SWITCH_KEY


@dataclass(frozen=True)
class Switch:
    """What the commands act on: the pool-state table (a boto3 table resource) and the name of whoever runs them."""

    table: Any
    caller: Callable[[], str]


def status(switch: Switch) -> SessionSwitch | None:
    """The switch as it is stored; `None` when it has no item, which reads as off."""
    item = switch.table.get_item(Key=SESSION_SWITCH_KEY, ConsistentRead=True).get("Item")
    if item is None:
        return None
    try:
        return SessionSwitch.from_item(item)
    except RecordError as error:
        raise AdminError(f"the session switch does not read: {error}") from None


def _unchanged_since(current: SessionSwitch | None) -> dict[str, Any]:
    """The condition that the switch still reads `current`: a missing item, or the same who and when."""
    if current is None:
        return {"ConditionExpression": "attribute_not_exists(#pool)", "ExpressionAttributeNames": {"#pool": "pool"}}
    return {
        "ConditionExpression": "#on = :on AND #by = :by AND #at = :at",
        "ExpressionAttributeNames": {"#on": "on", "#by": "set_by", "#at": "set_at"},
        "ExpressionAttributeValues": {":on": current.on, ":by": current.set_by, ":at": current.set_at},
    }


def turn(on: bool, *, switch: Switch, now: int) -> tuple[SessionSwitch | None, bool]:
    """The session switched `on` or off; the switch it then reads, and whether anything was written."""
    while True:
        current = status(switch)
        changed = switched(current, on, switch.caller(), now)
        if changed is None:
            return current, False
        try:
            switch.table.put_item(Item=changed.to_item(), **_unchanged_since(current))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue  # another admin switched it between the read and the write: read it again
        return changed, True
