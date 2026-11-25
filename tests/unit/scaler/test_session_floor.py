"""The scaler's decision under a session (`rhi_scaler.decide`): a pool is never below what its
session asks for, and the other rules decide the rest."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode, Session
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

# The simulator sits beside this file; the tests tree has no packages, so it is loaded by path.
_spec = importlib.util.spec_from_file_location("scaler_sim", Path(__file__).with_name("sim.py"))
assert _spec is not None and _spec.loader is not None
sim = importlib.util.module_from_spec(_spec)
sys.modules["scaler_sim"] = sim
_spec.loader.exec_module(sim)

NOW = 1_800_000_000  # a whole minute
MINUTE = 60
HOUR = 3600

# A pool that leaves `idle_minutes` out: 20.
ENTRY = {
    "engine": "mock",
    "instance_types": ["g6e.xlarge"],
    "prices": {"g6e.xlarge": 1.86},
    "max_instances": 3,
    "warm_instances": 1,
    "wake": "on-demand",
    "target_concurrency": 4,
    "context_length": 32_768,
    "largest_output": 4_096,
}
POOL = parse_pools({"coursework": ENTRY})[0]
NAMES = ("instance-a", "instance-b", "instance-c")
AT_ZERO = Supply(desired=0)
TWO = Supply(desired=2, in_service=NAMES[:2], running=NAMES[:2])
THREE = Supply(desired=3, in_service=NAMES, running=NAMES)
# A gateway was heard a moment ago, so no pool below is silent unless its scenario says so.
HEARD = Memory(last_gateway_at=NOW)


def session(instances: int = 2, *, starts_at: int = NOW - HOUR, ends_at: int = NOW + HOUR) -> Session:
    """A session of the pool, active at `NOW` unless its times say otherwise."""
    return Session(POOL.name, instances, starts_at, ends_at, set_by="instructor", set_at=starts_at - HOUR)


def gateway(*, held: int = 0, in_flight: int = 0, last_request_at: int | None = NOW - MINUTE) -> GatewayHeartbeat:
    return GatewayHeartbeat("task-1", POOL.name, in_flight, held, last_request_at, NOW)


def reports(*names: str, running: int = 0, waiting: int = 0) -> tuple[InstanceHeartbeat, ...]:
    return tuple(
        InstanceHeartbeat(name, POOL.name, running, waiting, "sha256:" + "ab" * 32, "rev-1", "serving", NOW)
        for name in names
    )


def run(supply: Supply, *gateways: GatewayHeartbeat, session_max_hours: int = 12, **fields: object) -> PoolDecision:
    pool = PoolInputs(config=POOL, mode=Mode.ENABLED, supply=supply, gateway_heartbeats=gateways, memory=HEARD)
    inputs = Inputs(now=NOW, pools=(replace(pool, **fields),), session_max_hours=session_max_hours)
    return decide(inputs).pools[0]


def test_a_session_raises_a_pool_at_zero_to_its_number() -> None:
    # No demand: without a session nothing asks for an instance.
    assert (run(AT_ZERO, gateway()).desired, run(AT_ZERO, gateway()).reason) == (0, Reason.UNCHANGED)
    raised = run(AT_ZERO, gateway(), session=session(2))
    assert (raised.desired, raised.reason, raised.requested) == (2, Reason.PROVISION, 2)
    assert (raised.lift_protection, raised.suspend_launches) == ((), False)
    # A held request would wake one instance: the session's number and its reason replace the wake.
    held = run(AT_ZERO, gateway(held=1), session=session(2))
    assert (held.desired, held.reason) == (2, Reason.PROVISION)
    # No gateway has ever reported, and nobody has to: a session asks by itself.
    assert run(AT_ZERO, session=session(2), memory=Memory()).desired == 2


@pytest.mark.parametrize(
    ("record", "session_max_hours", "requested"),
    [
        (None, 12, 0),
        (session(2, starts_at=NOW + 1), 12, 0),
        (session(2, starts_at=NOW), 12, 2),
        (session(2), 12, 2),
        # More than the pool's `max_instances`: the pool's.
        (session(5), 12, 3),
        # The end is excluded.
        (session(2, ends_at=NOW), 12, 0),
        # An end later than the longest session: the session ended `session_max_hours` after its start.
        (session(2, starts_at=NOW - 2 * HOUR, ends_at=NOW + HOUR), 3, 2),
        (session(2, starts_at=NOW - 2 * HOUR, ends_at=NOW + HOUR), 2, 0),
    ],
)
def test_the_decision_carries_what_the_session_asks_for_at_the_time_of_the_run(
    record: Session | None, session_max_hours: int, requested: int
) -> None:
    decision = run(AT_ZERO, gateway(), session=record, session_max_hours=session_max_hours)
    assert decision.requested == requested
    assert (decision.desired, decision.reason) == (
        (requested, Reason.PROVISION) if requested else (0, Reason.UNCHANGED)
    )


# Each rule that takes a pool of two to zero, as the inputs that make it fire.
LOWERING: dict[Reason, dict[str, Any]] = {
    Reason.IDLE: {"gateway_heartbeats": (gateway(last_request_at=NOW - 20 * MINUTE),)},
    Reason.STOPPED: {
        "mode": Mode.DISABLED,
        "gateway_heartbeats": (gateway(),),
        "instance_heartbeats": reports(*NAMES[:2]),
    },
    Reason.GATEWAY_SILENT: {"memory": Memory(last_gateway_at=NOW - 10 * MINUTE)},
}


@pytest.mark.parametrize("rule", LOWERING)
def test_a_rule_that_would_take_the_pool_below_its_session_does_not_apply(rule: Reason) -> None:
    without = run(TWO, **LOWERING[rule])
    assert (without.desired, without.reason, without.lift_protection) == (0, rule, NAMES[:2])
    kept = run(TWO, **LOWERING[rule], session=session(2))
    assert (kept.desired, kept.reason, kept.lift_protection) == (2, Reason.UNCHANGED, ())
    assert kept.requested == 2
    # A pool the session has not filled yet is raised in the same run, and nothing is released.
    short = run(replace(TWO, desired=1, in_service=NAMES[:1]), **LOWERING[rule], session=session(2))
    assert (short.desired, short.reason, short.lift_protection) == (2, Reason.PROVISION, ())


def test_above_the_session_s_number_the_other_rules_decide() -> None:
    # Three in service and little load for 10 minutes: one idle instance leaves, down to the
    # session's two.
    light: dict[str, Any] = {
        "gateway_heartbeats": (gateway(in_flight=1),),
        "instance_heartbeats": reports(*NAMES),
        "memory": replace(HEARD, low_load_since=NOW - 10 * MINUTE),
    }
    shrunk = run(THREE, **light, session=session(2))
    assert (shrunk.desired, shrunk.reason, shrunk.lift_protection) == (2, Reason.SCALE_IN, NAMES[:1])
    # Under a session of three the same scale-in does not apply, and its 10 minutes do not start again.
    kept = run(THREE, **light, session=session(3))
    assert (kept.desired, kept.reason, kept.lift_protection) == (3, Reason.UNCHANGED, ())
    assert kept.memory.low_load_since == NOW - 10 * MINUTE

    # The idle rule would take three to zero, below the session's two: the capacity stays at three.
    idle = run(THREE, gateway(last_request_at=NOW - 20 * MINUTE), session=session(2))
    assert (idle.desired, idle.lift_protection) == (3, ())

    # A pool at the session's two that has been full for a minute still grows, for its own reason.
    full: dict[str, Any] = {
        "gateway_heartbeats": (gateway(in_flight=10),),
        "memory": replace(HEARD, above_target_since=NOW - MINUTE),
    }
    grown = run(TWO, **full, session=session(2))
    assert (grown.desired, grown.reason, grown.requested) == (3, Reason.SCALE_OUT, 2)


def test_a_suspended_pool_is_not_raised() -> None:
    suspended = replace(HEARD, launch_suspended=True, suspended_at=NOW - HOUR)
    asleep = run(AT_ZERO, gateway(), session=session(2), memory=suspended)
    assert (asleep.desired, asleep.reason, asleep.suspend_launches) == (0, Reason.LAUNCH_SUSPENDED, True)
    assert asleep.requested == 2
    # The run that suspends the launches, on its third abandoned launch, does not raise the pool either.
    third = run(replace(TWO, desired=1, in_service=NAMES[:1]), gateway(), session=session(2), abandoned_launches=3)
    assert (third.desired, third.suspend_launches) == (1, True)
    # Suspended launches raise nothing; they do not let a rule take the pool below its session.
    idle = run(TWO, gateway(last_request_at=NOW - 20 * MINUTE), session=session(2), memory=suspended)
    assert (idle.desired, idle.lift_protection) == (2, ())


def test_after_the_end_the_other_rules_apply() -> None:
    # `idle_minutes` is 3, so the idle rule comes before a scale-in could: that one needs 10
    # minutes of low load.
    quick = parse_pools({"coursework": {**ENTRY, "idle_minutes": 3}})[0]
    end = NOW + 6 * MINUTE
    pool = sim.Sim(quick, start=NOW, in_service=2, warm=0, session=session(2, ends_at=end))
    pool.advance(4 * MINUTE)
    # No request yet: the idle rule would have taken the pool to zero, run after run.
    assert [(r.decision.desired, r.decision.requested) for r in pool.runs] == [(2, 2)] * 4
    # A request received at 4 minutes runs for 90 seconds: the run at 5 minutes sees it in flight.
    pool.request("task-1", 90)
    pool.advance(6 * MINUTE)

    by_minute = {(r.now - NOW) // MINUTE: r.decision for r in pool.runs}
    assert [by_minute[minute].requested for minute in (5, 6, 7, 8)] == [2, 0, 0, 0]
    # From the end on the session asks for nothing, and the pool stays while its last request is
    # younger than `idle_minutes`.
    assert [by_minute[minute].desired for minute in (5, 6, 7)] == [2, 2, 2]
    last = by_minute[8]
    assert (last.desired, last.reason) == (0, Reason.IDLE)
    assert last.lift_protection == ("instance-1", "instance-2")
    assert pool.in_service() == []
