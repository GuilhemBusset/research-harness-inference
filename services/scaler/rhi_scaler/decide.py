"""The scaler's decision: the size every pool should have (system design, section 5.1).

`decide` is a function of its inputs alone. It calls nothing and reads no clock: the time is
`Inputs.now`. Every rule is therefore tested without AWS, and a run that acts is judged on what
that run read, which is the re-check "at the moment of acting".

The rules here take a pool from zero and back:

| Transition | Rule |
| --- | --- |
| 0 to 1 | A held request for a pool in mode `enabled` at zero asks for one instance, however many are held |
| to 0 | No request received and none in flight or held for `idle_minutes`, in any mode: zero, naming every instance |
| stop | Mode `disabled` or `budget-latched`: never woken; each instance that reports nothing running and nothing waiting is released at once |

Demand is what the fresh gateway heartbeats report. An explicit wake reaches them as a held request
and as a request received, so it has no rule of its own. A pool in mode `scheduled-only` is not
woken by held requests, and is otherwise a pool nobody asked to stop.

A decision that lowers the capacity names the instances whose scale-in protection to lift: the
ones released by a stop, and every instance of the pool when it goes to zero.
"""

from __future__ import annotations

from rhi_common.records import Mode, is_fresh
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

# The modes of a pool somebody stopped: the admin, or the budget.
STOPPED_MODES = frozenset({Mode.DISABLED, Mode.BUDGET_LATCHED})


def _every_instance(supply: Supply) -> tuple[str, ...]:
    """Every instance of the pool, in whatever state the supply lists it."""
    return tuple(sorted({*supply.in_service, *supply.starting, *supply.draining, *supply.warm, *supply.running}))


def _older_than(time: int | None, seconds: int, now: int) -> bool:
    """Whether a time is at least `seconds` before `now`. A time nobody knows is."""
    return time is None or now - time >= seconds


def _decide_pool(pool: PoolInputs, now: int) -> PoolDecision:
    gateways = [heartbeat for heartbeat in pool.gateway_heartbeats if is_fresh(heartbeat, now)]
    in_flight = sum(heartbeat.in_flight for heartbeat in gateways)
    held = sum(heartbeat.held for heartbeat in gateways)
    # The last request received: the latest among the fresh heartbeats and the memory, which keeps it.
    received = [pool.memory.last_request_at, *(heartbeat.last_request_at for heartbeat in gateways)]
    memory = Memory(
        last_request_at=max((time for time in received if time is not None), default=None),
        last_in_flight_at=now if in_flight or held else pool.memory.last_in_flight_at,
    )

    supply = pool.supply
    # The instances the desired capacity counts.
    counted = {*supply.in_service, *supply.starting}
    idle_seconds = pool.config.idle_minutes * 60
    desired = supply.desired
    lift: tuple[str, ...] = ()
    reason = Reason.UNCHANGED

    if (
        (desired > 0 or counted)
        and not in_flight
        and not held
        and _older_than(memory.last_request_at, idle_seconds, now)
        and _older_than(memory.last_in_flight_at, idle_seconds, now)
    ):
        desired, lift, reason = 0, _every_instance(supply), Reason.IDLE
    elif pool.mode in STOPPED_MODES:
        released = sorted(
            {
                heartbeat.instance
                for heartbeat in pool.instance_heartbeats
                if heartbeat.instance in counted
                and is_fresh(heartbeat, now)
                and heartbeat.running == 0
                and heartbeat.waiting == 0
            }
        )
        if released:
            desired, lift, reason = max(0, desired - len(released)), tuple(released), Reason.STOPPED
        elif held and desired == 0:
            reason = Reason.NOT_WOKEN
    elif held and desired == 0:
        if pool.mode is Mode.ENABLED:
            desired, reason = 1, Reason.WAKE
        else:
            reason = Reason.NOT_WOKEN

    return PoolDecision(
        pool=pool.config.name,
        desired=desired,
        lift_protection=lift,
        # The launch-loop rule sets it; no rule here does.
        suspend_launches=False,
        memory=memory,
        reason=reason,
    )


def decide(inputs: Inputs) -> Decision:
    """The decision for every pool of the inputs, in their order."""
    return Decision(pools=tuple(_decide_pool(pool, inputs.now) for pool in inputs.pools))
