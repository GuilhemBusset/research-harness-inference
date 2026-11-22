"""The session switch, which only the admin sets (system design, section 5.6).

One item of the pool-state table says whether a session is on: `rhi_common.records.SessionSwitch`,
under the keys `rhi_common.tables.SESSION_SWITCH_KEY`, which no pool's name can take. The gateway
serves students only while it reads on, and the pipeline's deployment waits while it does. No
deployment writes it, and a missing item reads as off.

The commands:

- `status`: whether the session is on, and who last switched it and when;
- `switch`: the session on or off, with who and when. Switching it to what it already reads writes
  nothing (`rhi_common.records.switched`), so the time an off switch carries is the time the last
  session ended.

Who switched it is the caller's identity without its account (`rhi_admin.pools.identity_name`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


def turn(on: bool, *, switch: Switch, now: int) -> tuple[SessionSwitch | None, bool]:
    """The session switched `on` or off; the switch it then reads, and whether anything was written."""
    current = status(switch)
    changed = switched(current, on, switch.caller(), now)
    if changed is None:
        return current, False
    switch.table.put_item(Item=changed.to_item())
    return changed, True
