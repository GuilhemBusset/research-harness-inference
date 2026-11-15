"""The scaler's decision: the size every pool should have (system design, section 5.1).

`decide` is a function of its inputs alone. It calls nothing and reads no clock: the time is
`Inputs.now`. Every rule is therefore tested without AWS, and a run that acts is judged on what
that run read, which is the re-check "at the moment of acting".

The rules here take a pool from zero, grow it, and take it back:

| Transition | Rule |
| --- | --- |
| 0 to 1 | A held request for a pool in mode `enabled` at zero asks for one instance, however many are held |
| 1 to n | The instances in service full for 60 seconds, in a pool nobody stopped: one more instance, up to `max_instances`, and none while one asked for is still starting |
| n to n - 1 | Two instances or more in service and none starting or leaving, the requests in flight at most (n - 1) times half of `target_concurrency` for 10 minutes, 15 minutes after the last scale-out, in a pool nobody stopped: one instance fewer, one that reports nothing running and nothing waiting, and none when no instance does |
| to 0 | No request received and none in flight or held for `idle_minutes`, in any mode: zero, naming every instance |
| stop | Mode `disabled` or `budget-latched`: never woken; each instance that reports nothing running and nothing waiting is released at once, and one that reports work is kept, whatever the gateways report |

Demand is what the fresh gateway heartbeats report. An explicit wake reaches them as a held request
and as a request received, so it has no rule of its own. A pool in mode `scheduled-only` is not
woken by held requests, and is otherwise a pool nobody asked to stop.

The instances in service are full in two cases. The requests in flight, divided by the instances
in service, are above `target_concurrency`. Or an instance reports waiting requests while no
instance of the pool has room: fewer requests running than `target_concurrency` and none waiting,
in a fresh heartbeat. An instance that waits while another has room is not a reason to grow (the
rebalance transition moves students to the emptier one), and the time it spends so does not count.
The 60 seconds are counted in the memory, from one run to the next.

A decision that lowers the capacity names the instances whose scale-in protection to lift: the
ones released by a stop, the one a scale-in removes, and every instance of the pool when it goes
to zero.

A scale-in removes only an instance whose fresh heartbeat reports nothing running and nothing
waiting: the design does not lean on the load balancer's drain. An instance with no fresh heartbeat
is not known to be idle, so it is not removed. The last instance never leaves by this rule, only by
idleness. A scale-in starts the 10 minutes again, so the next one is judged on the smaller pool.
"""

from __future__ import annotations

from rhi_common.records import Mode, is_fresh
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

# The modes of a pool somebody stopped: the admin, or the budget.
STOPPED_MODES = frozenset({Mode.DISABLED, Mode.BUDGET_LATCHED})
# How long the instances in service must have been full before the pool grows.
SCALE_OUT_SECONDS = 60
# How long the load must have fit in one instance fewer before the pool shrinks.
LOW_LOAD_SECONDS = 10 * 60
# How long after the last scale-out the pool may shrink.
AFTER_SCALE_OUT_SECONDS = 15 * 60


def _every_instance(supply: Supply) -> tuple[str, ...]:
    """Every instance of the pool, in whatever state the supply lists it."""
    return tuple(sorted({*supply.in_service, *supply.starting, *supply.draining, *supply.warm, *supply.running}))


def _older_than(time: int | None, seconds: int, now: int) -> bool:
    """Whether a time is at least `seconds` before `now`. A time nobody knows is."""
    return time is None or now - time >= seconds


def _lasted(since: int | None, seconds: int, now: int) -> bool:
    """Whether what began at `since` has lasted `seconds`. What has not begun has not."""
    return since is not None and now - since >= seconds


def _decide_pool(pool: PoolInputs, now: int) -> PoolDecision:
    gateways = [heartbeat for heartbeat in pool.gateway_heartbeats if is_fresh(heartbeat, now)]
    in_flight = sum(heartbeat.in_flight for heartbeat in gateways)
    held = sum(heartbeat.held for heartbeat in gateways)
    # The last request received: the latest among the fresh heartbeats and the memory, which keeps it.
    received = [pool.memory.last_request_at, *(heartbeat.last_request_at for heartbeat in gateways)]

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

    if pool.mode in STOPPED_MODES:
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
        if pool.mode is Mode.ENABLED:
            desired, reason = 1, Reason.WAKE
        else:
            reason = Reason.NOT_WOKEN
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

    return PoolDecision(
        pool=pool.config.name,
        desired=desired,
        lift_protection=lift,
        # The launch-loop rule sets it; no rule here does.
        suspend_launches=False,
        memory=Memory(
            last_request_at=memory.last_request_at,
            last_in_flight_at=memory.last_in_flight_at,
            above_target_since=memory.above_target_since,
            waiting_since=memory.waiting_since,
            last_scale_out_at=last_scale_out_at,
            low_load_since=low_load_since,
        ),
        reason=reason,
    )


def decide(inputs: Inputs) -> Decision:
    """The decision for every pool of the inputs, in their order."""
    return Decision(pools=tuple(_decide_pool(pool, inputs.now) for pool in inputs.pools))
