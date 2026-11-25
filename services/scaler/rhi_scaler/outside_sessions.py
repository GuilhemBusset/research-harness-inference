"""How long each pool has been in service while no session is on (sections 11.3 and 17).

An instance in service outside a session is spend that serves no student, and a CloudWatch alarm
says so once it has lasted an hour by default (`rhi_scaler.cloudwatch`). No session is on while
the session switch reads off or has no record (`rhi_common.records.session_is_on`).

The count is in whole minutes, from the first of the runs in a row that saw the pool with at least
one instance in service while no session was on. A run that sees none in service, or that finds the
switch on, ends the stretch: the next one counts from zero. Where the stretch began is kept in the
pool's memory (`Memory.outside_sessions_since`), so it runs on from one run to the next.

A switch that reads off has been off since it was set (`set_at` is when it last changed, and
setting it to what it already reads writes nothing: `rhi_common.records.switched`). So when runs
were missed while a session came and went, the stretch counts from the end of that session, not
from before it, and an admin who sets an off switch off again leaves the count as it was.

A run that could not read the switch still sees the pool's instances: one that sees none in service
ends the stretch all the same, and one that sees some leaves the stretch where it stood.

The scaler reads the switch only to report: `decide` never sees it, and what a run decides stays a
function of demand and of each pool's own session (sections 5.1 and 5.6).
"""

from __future__ import annotations

from dataclasses import replace

from rhi_common.records import SessionSwitch, session_is_on
from rhi_scaler.model import Decision, Inputs


def stretch_since(
    previous: int | None, in_service: int, switch: SessionSwitch | None, now: int, *, read: bool = True
) -> int | None:
    """When the current stretch began, for a run at the time `now` that sees `in_service` instances
    in service; `previous` is where the stretch stood after the last run, and `read` is false when
    the run could not read the switch. `None` when there is no stretch."""
    if in_service == 0:
        return None
    if not read:
        return previous
    if session_is_on(switch):
        return None
    if previous is None:
        return now
    if switch is not None:
        # No session has been on since the switch was set off.
        return max(previous, min(switch.set_at, now))
    return previous


def minutes(since: int | None, now: int) -> int:
    """The whole minutes from the start of the stretch to `now`; 0 when there is no stretch."""
    return 0 if since is None else max(0, now - since) // 60


def carry(inputs: Inputs, decision: Decision, switch: SessionSwitch | None, *, read: bool = True) -> Decision:
    """The decision with each pool's memory holding where its stretch stands after this run.

    `read` is false when the run could not read the switch (`stretch_since`)."""
    seen = {pool.config.name: pool for pool in inputs.pools}
    pools = []
    for pool in decision.pools:
        before = seen[pool.pool]
        previous = before.memory.outside_sessions_since
        since = stretch_since(previous, len(before.supply.in_service), switch, inputs.now, read=read)
        pools.append(replace(pool, memory=replace(pool.memory, outside_sessions_since=since)))
    return replace(decision, pools=tuple(pools))
