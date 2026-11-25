"""The scaler's decision: the size every pool should have (system design, section 5.1).

`decide` is a function of its inputs alone. It calls nothing and reads no clock: the time is
`Inputs.now`. Every rule is therefore tested without AWS, and a run that acts is judged on what
that run read, which is the re-check "at the moment of acting".

The scaler does not scale by load. It reconciles: each run sets every pool to the size its session
asks for, and to zero when it has none:

| Case | Rule |
| --- | --- |
| A session is active | The size is `requested`: the session's number, capped at `max_instances`. All of it is asked for at once, with the reason `provision` |
| No session, or its end has passed | The size is zero |
| The size goes down | Capacity that has not arrived is no longer asked for. Then the instances in service beyond `requested` are released, with the reason `release`: those that report nothing running and nothing waiting first, then those with the fewest requests |
| rebalance | An instance in service that has reported waiting requests for 60 seconds, while another instance in service runs fewer requests than half of `target_concurrency`: the pool's rebalance number goes up by one, at most once every `rebalance_interval_seconds`. The size does not change |
| launch loop | Three launches abandoned in a row: suspend the group's launches and set the mark; the pool is not raised until the admin clears it |

What a pool's session asks for at the time of the run is `requested`, by
`rhi_common.sessions.requested` and nothing else: the session's number of instances, at most
`max_instances`, from its start to its end, and 0 otherwise (section 5.6). The desired capacity of
every run is that number, whatever the group held: 0 to 3 is one decision, not three. Suspended
launches are the one exception, below.

No rule reads the students' demand. The decision reads neither the gateways' heartbeats nor the
pool's mode, which the inputs still carry, and the instances' load never sizes a pool: a held
request starts no instance, a full pool does not grow, a quiet one keeps its session's number, and
a gateway that does not answer is an alarm, not a reason to give back instances that took long to
get. The memory's attributes of load, of idleness and of the gateway are left at their empty
values.

**Release.** When more instances are in service than `requested`, the decision names that many of
them, the ones whose scale-in protection to lift, and its reason is `release`. In service, for
this rule, is what the group itself holds in service, since that is where scale-in protection
starts: the instances that serve (`Supply.in_service`), and those whose target is not healthy
(`Supply.not_serving`), which the supply lists as starting or as draining. They are chosen in this
order:

1. those whose fresh heartbeat reports nothing running and nothing waiting;
2. those with the fewest requests running and waiting, by their fresh heartbeat;
3. those that serve with no fresh heartbeat, whose load is not known.

What an instance reports comes before the health of its target: one that reports nothing running
and nothing waiting leaves before one that reports requests, whichever of the two serves. An
instance that does not serve and reports nothing fresh counts in the second place as one with no
request, after those that report none: the load balancer sends it nothing.

Within each, by name. An instance still starting, one the group has not put in service, is never
named: it has no protection to lift. Lowering the capacity is what stops asking for it. If the
group keeps it all the same, it enters service and the run that then sees one instance too many
releases whichever comes first in the order above. An instance the group is already terminating is
not named either. A run that lowers the capacity and has no instance to name, since what it gives
up had not arrived, has the reason `release` too.

A raise comes first among the reasons. The capacity the run read can be below `requested` while
more instances than `requested` are still in service, when an earlier run lowered it and the
instances it named have not left yet. That run asks for `requested`, names the instances beyond it,
and its reason is `provision`, or `launch-suspended` when the pool is not raised.

**Rebalance.** The rule sets no size: it stands apart from the others. For each new number the
gateway moves a share of the pool's students to the emptiest instance (section 5.5), a few at a
time: each move costs the instance that receives it a prefill. The rule keeps its own count of how
long each instance in service has reported waiting requests: a pool whose instances arrive one
after another is lopsided from the first run that sees a new instance in service, since the 60
seconds went by while it started. The other instance has room when its fresh heartbeat reports
fewer requests running than half of `target_concurrency`, whatever it reports waiting. The number
goes up only in a run that sees the pool lopsided, and only when the pool's
`rebalance_interval_seconds` have passed since the run that last raised it, a time the memory
keeps. A decision names one reason: it is `rebalance` when no other rule gives the run one, and
that rule's when one does, in which case the number goes up all the same.

**Launch loop.** A launch that fails every time would loop: launch, wait, abandon, relaunch. When
the input counts three launches abandoned in a row, the decision suspends the group's launches and
sets the mark `launch_suspended` in the memory. While it stands, every run keeps the launches
suspended and the pool is not raised: a session that asks for more than the group was asked for
leaves the capacity where the run read it, with the reason `launch-suspended`. A size that goes
down still does, and the instances beyond `requested` are named in either case. The admin clears the mark by setting it back to false. The run that finds it so
resumes the launches and keeps its own time as the start of the count; it ignores the abandons it
was given, which the group's history still holds, and decides as for any pool, so the session
raises it. Only three abandons counted from that time suspend it again.
"""

from __future__ import annotations

from rhi_common.records import is_fresh
from rhi_common.sessions import requested
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason

# How long an instance must have reported waiting requests before the pool is lopsided.
REBALANCE_WAITING_SECONDS = 60
# How many launches abandoned in a row suspend the group's launches.
ABANDONED_LAUNCHES_LIMIT = 3


def _older_than(time: int | None, seconds: int, now: int) -> bool:
    """Whether a time is at least `seconds` before `now`. A time nobody knows is."""
    return time is None or now - time >= seconds


def _lasted(since: int | None, seconds: int, now: int) -> bool:
    """Whether what began at `since` has lasted `seconds`. What has not begun has not."""
    return since is not None and now - since >= seconds


def _decide_pool(pool: PoolInputs, now: int, session_max_hours: int) -> PoolDecision:
    # The launch loop. A mark that is false while the memory keeps when it was set: the admin
    # cleared it. That run starts the count again and ignores the abandons it was given, which the
    # edge counted from the old start.
    before = pool.memory
    cleared = not before.launch_suspended and before.suspended_at is not None
    abandons_counted_from = now if cleared else before.abandons_counted_from
    if before.launch_suspended:
        suspended, suspended_at = True, now if before.suspended_at is None else before.suspended_at
    elif not cleared and pool.abandoned_launches >= ABANDONED_LAUNCHES_LIMIT:
        suspended, suspended_at = True, now
    else:
        suspended, suspended_at = False, None

    supply = pool.supply
    target = pool.config.target_concurrency
    # What the instances in service report themselves, in a fresh heartbeat.
    serving = [
        heartbeat
        for heartbeat in pool.instance_heartbeats
        if heartbeat.instance in supply.in_service and is_fresh(heartbeat, now)
    ]

    # The size: what the session asks for, and nothing else.
    asked = requested(pool.session, now, max_instances=pool.config.max_instances, session_max_hours=session_max_hours)
    desired, reason = asked, Reason.UNCHANGED
    if asked > supply.desired:
        if suspended:
            # A suspended pool is not raised: the capacity stays what the run read.
            desired, reason = supply.desired, Reason.LAUNCH_SUSPENDED
        else:
            reason = Reason.PROVISION
    # The release: as many of the instances the group holds in service as there are beyond what
    # the session asks for; an instance the group has not put in service is not among them. What
    # each reports in a fresh heartbeat, whether or not its target is healthy.
    held = {*supply.in_service, *supply.not_serving}
    load = {
        heartbeat.instance: heartbeat.running + heartbeat.waiting
        for heartbeat in pool.instance_heartbeats
        if heartbeat.instance in held and is_fresh(heartbeat, now)
    }

    def release_order(name: str) -> tuple[bool, bool, int, str]:
        # Those that report nothing running and nothing waiting, then the fewest requests, where
        # one that does not serve and reports nothing counts as having none, then those that
        # serve and report nothing fresh.
        return load.get(name) != 0, name not in load and name in supply.in_service, load.get(name, 0), name

    lift = tuple(sorted(sorted(held, key=release_order)[: max(0, len(held) - asked)]))
    # A raise, or a pool kept from one, keeps its reason.
    if reason is Reason.UNCHANGED and (lift or desired < supply.desired):
        reason = Reason.RELEASE

    # The rebalance, whatever the size: it changes none. Its own count: an instance that waits,
    # whether or not another has room.
    queuing_before = dict(before.queuing_since)
    queuing_since = tuple(
        (name, queuing_before.get(name, now)) for name in sorted({h.instance for h in serving if h.waiting})
    )
    # The instances below half of the target, without the division.
    below_half = {heartbeat.instance for heartbeat in serving if 2 * heartbeat.running < target}
    # One instance has waited 60 seconds while another, not itself, runs below half of the target.
    lopsided = any(
        _lasted(since, REBALANCE_WAITING_SECONDS, now) and below_half - {name} for name, since in queuing_since
    )
    # A rule that gave the run its reason keeps it.
    rebalance_number, last_rebalance_at = before.rebalance_number, before.last_rebalance_at
    if lopsided and _older_than(last_rebalance_at, pool.config.rebalance_interval_seconds, now):
        rebalance_number, last_rebalance_at = rebalance_number + 1, now
        if reason is Reason.UNCHANGED:
            reason = Reason.REBALANCE
    if suspended and reason is Reason.UNCHANGED:
        reason = Reason.LAUNCH_SUSPENDED

    return PoolDecision(
        pool=pool.config.name,
        desired=desired,
        lift_protection=lift,
        suspend_launches=suspended,
        # The attributes of load, of idleness and of the gateway stay at their empty values.
        memory=Memory(
            launch_suspended=suspended,
            suspended_at=suspended_at,
            abandons_counted_from=abandons_counted_from,
            queuing_since=queuing_since,
            rebalance_number=rebalance_number,
            last_rebalance_at=last_rebalance_at,
        ),
        reason=reason,
        rebalance_number=rebalance_number,
        requested=asked,
    )


def decide(inputs: Inputs) -> Decision:
    """The decision for every pool of the inputs, in their order."""
    return Decision(
        pools=tuple(_decide_pool(pool, inputs.now, inputs.session_max_hours) for pool in inputs.pools)
    )
