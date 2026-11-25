"""How long each pool has been in service outside its session (sections 11.3 and 17).

An instance in service outside a session is spend that serves no student, and a CloudWatch alarm
says so once it has lasted an hour by default (`rhi_scaler.cloudwatch`). Each pool is counted
against its own session, the one the decision reads (`rhi_common.sessions.is_active`): a pool is
outside its session before the session's start, from its end on, and while it has none. Another
pool's session changes nothing for it.

The count is in whole minutes, from where the pool's stretch began. A stretch begins at the first
of the runs in a row that saw the pool with at least one instance in service while its session was
not active, and no earlier than that session's end: when runs were missed while a session came and
went, the stretch counts from the end of that session, not from before it. A run that sees none in
service, or that finds the session active, ends the stretch: the next one counts from zero. Where
the stretch began is kept in the pool's memory (`Memory.outside_sessions_since`), so it runs on
from one run to the next.

The stretch only reports: `decide` neither reads nor sets it.
"""

from __future__ import annotations

from dataclasses import replace

from rhi_common import sessions
from rhi_common.records import Session
from rhi_scaler.model import Decision, Inputs


def stretch_since(
    previous: int | None, in_service: int, session: Session | None, now: int, *, session_max_hours: int
) -> int | None:
    """When the pool's current stretch began, for a run at the time `now` that sees `in_service`
    instances in service; `previous` is where the stretch stood after the last run, and `session`
    is the pool's own, `None` when it has none, read under `session_max_hours`. `None` when there
    is no stretch."""
    if in_service == 0 or sessions.is_active(session, now, session_max_hours=session_max_hours):
        return None
    if previous is None:
        return now
    if session is not None:
        ended = sessions.end(session, session_max_hours=session_max_hours)
        if ended <= now:
            # The session was active until its end: no stretch began before it.
            return max(previous, ended)
    return previous


def minutes(since: int | None, now: int) -> int:
    """The whole minutes from the start of the stretch to `now`; 0 when there is no stretch."""
    return 0 if since is None else max(0, now - since) // 60


def carry(inputs: Inputs, decision: Decision) -> Decision:
    """The decision with each pool's memory holding where its stretch stands after this run, each
    counted against the session the inputs hold for that pool."""
    seen = {pool.config.name: pool for pool in inputs.pools}
    pools = []
    for pool in decision.pools:
        before = seen[pool.pool]
        since = stretch_since(
            before.memory.outside_sessions_since,
            len(before.supply.in_service),
            before.session,
            inputs.now,
            session_max_hours=inputs.session_max_hours,
        )
        pools.append(replace(pool, memory=replace(pool.memory, outside_sessions_since=since)))
    return replace(decision, pools=tuple(pools))
