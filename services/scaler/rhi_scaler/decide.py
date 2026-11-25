"""The scaler's decision: the size every pool should have (system design, section 5.1).

`decide` is a function of its inputs alone. It calls nothing and reads no clock: the time is
`Inputs.now`. Every rule is therefore tested without AWS, and a run that acts is judged on what
that run read, which is the re-check "at the moment of acting".

The rules here take a pool from zero, grow it, even its load out, and take it back, and a pool's
session is a floor under them:

| Transition | Rule |
| --- | --- |
| session | The pool's session asks for more than the other rules give, and the launches are not suspended: the session's number, the reason `provision`. While it asks for anything, a rule that would take the pool below its number does not apply |
| 0 to 1 | A held request for a pool in mode `enabled` at zero asks for one instance, however many are held |
| 1 to n | The instances in service full for 60 seconds, in a pool nobody stopped: one more instance, up to `max_instances`, and none while one asked for is still starting |
| rebalance | An instance in service that has reported waiting requests for 60 seconds, while another instance in service runs fewer requests than half of `target_concurrency`: the pool's rebalance number goes up by one, at most once every `rebalance_interval_seconds`. The size is what the other rules make it |
| n to n - 1 | Two instances or more in service and none starting or leaving, the requests in flight at most (n - 1) times half of `target_concurrency` for 10 minutes, 15 minutes after the last scale-out, in a pool nobody stopped: one instance fewer, one that reports nothing running and nothing waiting, and none when no instance does |
| to 0 | No request received and none in flight or held for `idle_minutes`, in any mode: zero, naming every instance |
| stop | Mode `disabled`: never woken; each instance that reports nothing running and nothing waiting is released at once, and one that reports work is kept, whatever the gateways report |
| gateway silent | No fresh gateway heartbeat for 10 minutes, in any mode: zero, naming every instance, even one that reports work |
| launch loop | Three launches abandoned in a row: suspend the group's launches and set the mark; nothing wakes or grows the pool until the admin clears it |

Demand is what the fresh gateway heartbeats report. An explicit wake reaches them as a held request
and as a request received, so it has no rule of its own. A pool in mode `scheduled-only` is not
woken by held requests, and is otherwise a pool nobody asked to stop.

The instances in service are full in two cases. The requests in flight, divided by the instances
in service, are above `target_concurrency`. Or an instance reports waiting requests while no
instance of the pool has room: fewer requests running than `target_concurrency` and none waiting,
in a fresh heartbeat. An instance that waits while another has room is not a reason to grow (the
rebalance transition moves students to the emptier one), and the time it spends so does not count.
The 60 seconds are counted in the memory, from one run to the next.

A pool's session is a floor (section 5.6). What it asks for at the time of the run is `requested`,
by `rhi_common.sessions.requested` and nothing else: the session's number of instances, at most
`max_instances`, from its start to its end, and 0 otherwise. The other rules decide first, as for a
pool with no session. Then, while `requested` is above zero, the rule among them that lowers the
capacity (gateway silent, the idle rule, a stop, a scale-in) does not apply if it would set the
capacity below `requested`: the capacity stays what the group had, no protection is lifted, and the
memory is as if the rule had not fired. It does not apply in part either: a pool above its
session's number that the idle rule would take to zero stays where it is, since which instances
leave when a session asks for fewer is not decided here. A rule whose result is at or above
`requested` applies as it does without a session. Last, when `requested` is above the capacity
this leaves, the capacity is `requested` and the reason is `provision`, whatever reason another rule
gave: in any mode, since the design gives a session no mode, and all at once. Suspended launches are
the one thing that keeps a session from raising a pool; they do not let a rule take it below.
A pool with no session, or whose session has not started or has ended, is decided by the other
rules alone.

The rebalance rule sets no size: it stands apart from the others, which decide the capacity as
if it were not there. For each new number the gateway moves a share of the pool's
students to the emptiest instance (section 5.5), a few at a time: each move costs the instance that
receives it a prefill. The rule keeps its own count of how long each instance in service has
reported waiting requests, whether or not another has room: a pool that has just grown is lopsided
from the first run that sees its new instance in service, since the 60 seconds went by while it
started. The other instance has room for this rule when its fresh heartbeat reports fewer requests
running than half of `target_concurrency`, whatever it reports waiting. The number goes up only in
a run that sees the pool lopsided, and only when the pool's `rebalance_interval_seconds` have
passed since the run that last raised it, a time the memory keeps. A decision names one reason:
it is `rebalance` when no other rule gives the run one, and that rule's when one does (the pool
grows, shrinks, goes to zero, or is kept from growing), in which case the number goes up all the
same.

A decision that lowers the capacity names the instances whose scale-in protection to lift: the
ones released by a stop, the one a scale-in removes, and every instance of the pool when it goes
to zero.

A scale-in removes only an instance whose fresh heartbeat reports nothing running and nothing
waiting: the design does not lean on the load balancer's drain. An instance with no fresh heartbeat
is not known to be idle, so it is not removed. The last instance never leaves by this rule, only by
idleness. A scale-in starts the 10 minutes again, so the next one is judged on the smaller pool.

The two fail-safes come from section 5.1. Nobody can reach the fleet without the gateway, so a pool
that has capacity and no fresh gateway heartbeat for 10 minutes goes to zero, before any other rule:
an instance that reports work does not keep it. The time of the last heartbeat is kept in the
memory, so the 10 minutes span runs; a pool no run has seen a gateway report on is silent.

A launch that fails every time would loop: launch, wait, abandon, relaunch. When the input counts
three launches abandoned in a row, the decision suspends the group's launches and sets the mark
`launch_suspended` in the memory. While it stands, every run keeps the launches suspended, and no
rule raises the capacity: a held request does not wake the pool and a full pool does not grow. The
rules that lower it still apply. The admin clears the mark by setting it back to false. The run
that finds it so resumes the launches and keeps its own time as the start of the count; it ignores
the abandons it was given, which the group's history still holds, and decides as for any pool, so a
held request wakes it. Only three abandons counted from that time suspend it again.
"""

from __future__ import annotations

from rhi_common.records import Mode, is_fresh
from rhi_common.sessions import requested
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

# The modes of a pool somebody stopped: the admin's `disabled`, and nothing else.
STOPPED_MODES = frozenset({Mode.DISABLED})
# How long the instances in service must have been full before the pool grows.
SCALE_OUT_SECONDS = 60
# How long an instance must have reported waiting requests before the pool is lopsided.
REBALANCE_WAITING_SECONDS = 60
# How long the load must have fit in one instance fewer before the pool shrinks.
LOW_LOAD_SECONDS = 10 * 60
# How long after the last scale-out the pool may shrink.
AFTER_SCALE_OUT_SECONDS = 15 * 60
# How long without a fresh gateway heartbeat before a pool goes to zero.
GATEWAY_SILENT_SECONDS = 10 * 60
# How many launches abandoned in a row suspend the group's launches.
ABANDONED_LAUNCHES_LIMIT = 3
# The reasons of the rules that lower a pool's capacity, which a session's number is a floor under.
LOWERING_REASONS = frozenset({Reason.GATEWAY_SILENT, Reason.IDLE, Reason.STOPPED, Reason.SCALE_IN})


def _every_instance(supply: Supply) -> tuple[str, ...]:
    """Every instance of the pool, in whatever state the supply lists it."""
    return tuple(sorted({*supply.in_service, *supply.starting, *supply.draining, *supply.warm, *supply.running}))


def _older_than(time: int | None, seconds: int, now: int) -> bool:
    """Whether a time is at least `seconds` before `now`. A time nobody knows is."""
    return time is None or now - time >= seconds


def _lasted(since: int | None, seconds: int, now: int) -> bool:
    """Whether what began at `since` has lasted `seconds`. What has not begun has not."""
    return since is not None and now - since >= seconds


def _decide_pool(pool: PoolInputs, now: int, session_max_hours: int) -> PoolDecision:
    gateways = [heartbeat for heartbeat in pool.gateway_heartbeats if is_fresh(heartbeat, now)]
    in_flight = sum(heartbeat.in_flight for heartbeat in gateways)
    held = sum(heartbeat.held for heartbeat in gateways)
    # The last request received: the latest among the fresh heartbeats and the memory, which keeps it.
    received = [pool.memory.last_request_at, *(heartbeat.last_request_at for heartbeat in gateways)]
    # The last gateway heartbeat seen, kept the same way.
    heard = [pool.memory.last_gateway_at, *(heartbeat.written_at for heartbeat in gateways)]
    last_gateway_at = max((time for time in heard if time is not None), default=None)

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
    # The average over the instances in service, without the division. Over none it is no measure.
    above_target = bool(supply.in_service) and in_flight > target * len(supply.in_service)
    # An instance that waits has no room itself, so "no other instance has room" is "none has".
    room = any(heartbeat.running < target and not heartbeat.waiting for heartbeat in serving)
    waiting = () if room else sorted({heartbeat.instance for heartbeat in serving if heartbeat.waiting})
    waiting_before = dict(pool.memory.waiting_since)
    # The rebalance rule's own count: an instance that waits, whether or not another has room.
    queuing_before = dict(pool.memory.queuing_since)
    queuing_since = tuple(
        (name, queuing_before.get(name, now)) for name in sorted({h.instance for h in serving if h.waiting})
    )
    # The instances below half of the target, without the division.
    below_half = {heartbeat.instance for heartbeat in serving if 2 * heartbeat.running < target}
    # One instance has waited 60 seconds while another, not itself, runs below half of the target.
    lopsided = any(
        _lasted(since, REBALANCE_WAITING_SECONDS, now) and below_half - {name} for name, since in queuing_since
    )
    # A run that sees it for the first time starts the count; the runs after it keep that time.
    above_since = now if pool.memory.above_target_since is None else pool.memory.above_target_since
    n = len(supply.in_service)
    # The load fits in n - 1 instances at half of the target, without the division.
    low_load = n >= 2 and 2 * in_flight <= (n - 1) * target
    low_since = now if pool.memory.low_load_since is None else pool.memory.low_load_since
    memory = Memory(
        last_request_at=max((time for time in received if time is not None), default=None),
        last_in_flight_at=now if in_flight or held else pool.memory.last_in_flight_at,
        above_target_since=above_since if above_target else None,
        waiting_since=tuple((name, waiting_before.get(name, now)) for name in waiting),
        last_scale_out_at=pool.memory.last_scale_out_at,
        low_load_since=low_since if low_load else None,
    )
    full = _lasted(memory.above_target_since, SCALE_OUT_SECONDS, now) or any(
        _lasted(since, SCALE_OUT_SECONDS, now) for _, since in memory.waiting_since
    )
    # An instance asked for and not yet in service: listed as starting, or not listed yet.
    launching = bool(supply.starting) or supply.desired > len(supply.in_service)

    # The instances the desired capacity counts.
    counted = {*supply.in_service, *supply.starting}
    idle_seconds = pool.config.idle_minutes * 60
    desired = supply.desired
    lift: tuple[str, ...] = ()
    reason = Reason.UNCHANGED
    last_scale_out_at, low_load_since = memory.last_scale_out_at, memory.low_load_since

    # No request received and none in flight or held for `idle_minutes`, on what the gateways report.
    idle = (
        (desired > 0 or bool(counted))
        and not in_flight
        and not held
        and _older_than(memory.last_request_at, idle_seconds, now)
        and _older_than(memory.last_in_flight_at, idle_seconds, now)
    )
    # What the counted instances report themselves, in a fresh heartbeat.
    reports = [
        heartbeat
        for heartbeat in pool.instance_heartbeats
        if heartbeat.instance in counted and is_fresh(heartbeat, now)
    ]
    working = {heartbeat.instance for heartbeat in reports if heartbeat.running or heartbeat.waiting}
    # The instances in service that report nothing running and nothing waiting: the only ones a
    # scale-in may remove.
    unused = sorted({heartbeat.instance for heartbeat in serving if not heartbeat.running and not heartbeat.waiting})

    if (desired > 0 or bool(counted)) and _older_than(last_gateway_at, GATEWAY_SILENT_SECONDS, now):
        # Nobody can reach the fleet without the gateway: zero, before every other rule.
        desired, lift, reason = 0, _every_instance(supply), Reason.GATEWAY_SILENT
    elif pool.mode in STOPPED_MODES:
        # A stop comes before idleness: an instance that still reports work is kept, even when the
        # gateways have seen no request for `idle_minutes` (a client that left, an engine that has
        # not finished).
        released = tuple(sorted({heartbeat.instance for heartbeat in reports} - working))
        if idle and not working:
            desired, lift, reason = 0, _every_instance(supply), Reason.IDLE
        elif released:
            desired, lift, reason = max(0, desired - len(released)), released, Reason.STOPPED
        elif held and desired == 0:
            reason = Reason.NOT_WOKEN
    elif idle:
        desired, lift, reason = 0, _every_instance(supply), Reason.IDLE
    elif held and desired == 0:
        if pool.mode is not Mode.ENABLED:
            reason = Reason.NOT_WOKEN
        elif suspended:
            reason = Reason.LAUNCH_SUSPENDED
        else:
            desired, reason = 1, Reason.WAKE
    elif full and not launching and desired < pool.config.max_instances and suspended:
        reason = Reason.LAUNCH_SUSPENDED
    elif full and not launching and desired < pool.config.max_instances:
        # One instance at a time: the next waits for this one to be in service.
        desired, reason = desired + 1, Reason.SCALE_OUT
        last_scale_out_at = now
    elif (
        low_load
        # Every instance asked for is in service: none starting, none already being removed.
        and desired == n
        and not supply.starting
        and _lasted(low_load_since, LOW_LOAD_SECONDS, now)
        and _older_than(last_scale_out_at, AFTER_SCALE_OUT_SECONDS, now)
        and unused
    ):
        desired, lift, reason = n - 1, (unused[0],), Reason.SCALE_IN
        # The next scale-in is judged on the smaller pool: its 10 minutes start again.
        low_load_since = None
    # The session, a floor under the rules above. One of them that would take the pool below what
    # the session asks for does not apply: the capacity stays, and nothing is released.
    asked = requested(pool.session, now, max_instances=pool.config.max_instances, session_max_hours=session_max_hours)
    if reason in LOWERING_REASONS and desired < asked:
        desired, lift, reason = supply.desired, (), Reason.UNCHANGED
        low_load_since = memory.low_load_since
    if asked > desired and not suspended:
        desired, reason = asked, Reason.PROVISION
    # The rebalance, whatever the rules above decided: it changes no size. A rule that gave the
    # run its reason keeps it.
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
        memory=Memory(
            last_request_at=memory.last_request_at,
            last_in_flight_at=memory.last_in_flight_at,
            above_target_since=memory.above_target_since,
            waiting_since=memory.waiting_since,
            last_scale_out_at=last_scale_out_at,
            low_load_since=low_load_since,
            last_gateway_at=last_gateway_at,
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
