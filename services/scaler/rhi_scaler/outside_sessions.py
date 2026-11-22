"""How long each pool has been in service while no session is on (sections 11.3 and 17).

An instance in service outside a session is spend that serves no student, and a CloudWatch alarm
says so once it has lasted an hour by default (`rhi_scaler.cloudwatch`). No session is on while
the session switch reads off or has no record (`rhi_common.records.session_is_on`).

The count is in whole minutes, from the first of the runs in a row that saw the pool with at least
one instance in service while no session was on. A run that sees none in service, or that finds the
switch on, ends the stretch: the next one counts from zero. Where the stretch began is kept in the
pool's memory (`Memory.outside_sessions_since`), so it runs on from one run to the next.

The scaler reads the switch only to report: `decide` never sees it, and what a run decides stays a
function of demand (sections 5.1 and 5.6).
"""

from __future__ import annotations

from dataclasses import replace

from rhi_common.records import SessionSwitch, session_is_on
from rhi_scaler.model import Decision, Inputs


def stretch_since(previous: int | None, in_service: int, switch: SessionSwitch | None, now: int) -> int | None:
    """When the current stretch began, for a run at the time `now` that sees `in_service` instances
    in service; `previous` is where the stretch stood after the last run. `None` when there is none."""
    if in_service == 0 or session_is_on(switch):
        return None
    return now if previous is None else previous


def minutes(since: int | None, now: int) -> int:
    """The whole minutes from the start of the stretch to `now`; 0 when there is no stretch."""
    return 0 if since is None else max(0, now - since) // 60


def carry(inputs: Inputs, decision: Decision, switch: SessionSwitch | None) -> Decision:
    """The decision with each pool's memory holding where its stretch stands after this run."""
    read = {pool.config.name: pool for pool in inputs.pools}
    pools = []
    for pool in decision.pools:
        seen = read[pool.pool]
        since = stretch_since(seen.memory.outside_sessions_since, len(seen.supply.in_service), switch, inputs.now)
        pools.append(replace(pool, memory=replace(pool.memory, outside_sessions_since=since)))
    return replace(decision, pools=tuple(pools))
