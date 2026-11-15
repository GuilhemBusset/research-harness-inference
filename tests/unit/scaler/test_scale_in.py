"""The scaler's decision (`rhi_scaler.decide`): one instance fewer, and only an idle one."""

from __future__ import annotations

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

T = 1_800_000_000
TEN_MINUTES, FIFTEEN_MINUTES = 600, 900
POOL = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "prices": {"g6e.xlarge": 1.86},
            "max_instances": 3,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]
TWO = Supply(desired=2, in_service=("instance-a", "instance-b"), running=("instance-a", "instance-b"))
# instance-a works; instance-b reports nothing running and nothing waiting.
ONE_IDLE = (("instance-a", 4, 0), ("instance-b", 0, 0))


def run(
    now: int,
    supply: Supply,
    in_flight: int,
    reports: tuple[tuple[str, int, int], ...],
    memory: Memory = Memory(),  # noqa: B008 - a frozen value, never changed
    mode: Mode = Mode.ENABLED,
) -> PoolDecision:
    """One run at `now`: one gateway task with `in_flight` requests and the instances' reports."""
    pool = PoolInputs(
        config=POOL,
        mode=mode,
        supply=supply,
        gateway_heartbeats=(GatewayHeartbeat("task-0", POOL.name, in_flight, 0, now, now),),
        instance_heartbeats=tuple(
            InstanceHeartbeat(name, POOL.name, running, waiting, "sha256:" + "ab" * 32, "rev-1", "serving", now)
            for name, running, waiting in reports
        ),
        memory=memory,
    )
    (decision,) = decide(Inputs(now=now, pools=(pool,))).pools
    return decision


def for_ten_minutes(
    supply: Supply,
    in_flight: int,
    reports: tuple[tuple[str, int, int], ...],
    memory: Memory = Memory(),  # noqa: B008 - a frozen value, never changed
) -> PoolDecision:
    """A run at T, then one ten minutes later with its memory: the load has lasted ten minutes."""
    first = run(T, supply, in_flight, reports, memory)
    assert first.desired == supply.desired
    return run(T + TEN_MINUTES, supply, in_flight, reports, first.memory)


def test_the_idle_instance_leaves_after_ten_minutes_of_low_load() -> None:
    assert POOL.target_concurrency == 8
    first = run(T, TWO, 4, ONE_IDLE)
    assert (first.desired, first.lift_protection, first.memory.low_load_since) == (2, (), T)
    assert run(T + TEN_MINUTES - 1, TWO, 4, ONE_IDLE, first.memory).desired == 2

    removed = run(T + TEN_MINUTES, TWO, 4, ONE_IDLE, first.memory)
    assert (removed.desired, removed.lift_protection, removed.reason) == (1, ("instance-b",), Reason.SCALE_IN)
    # The next scale-in counts its 10 minutes again.
    assert removed.memory.low_load_since is None


def test_no_instance_leaves_above_half_the_target_or_soon_after_a_scale_out() -> None:
    above = for_ten_minutes(TWO, 5, ONE_IDLE)
    assert (above.desired, above.lift_protection, above.memory.low_load_since) == (2, (), None)

    # The scale-out itself is kept in the memory.
    grown = run(T - 60, TWO, 17, ONE_IDLE, Memory(above_target_since=T - 120))
    assert (grown.reason, grown.memory.last_scale_out_at) == (Reason.SCALE_OUT, T - 60)
    soon = for_ten_minutes(TWO, 4, ONE_IDLE, grown.memory)
    assert (soon.desired, soon.lift_protection, soon.memory.last_scale_out_at) == (2, (), T - 60)
    # Fifteen minutes after it, the same load removes the idle instance.
    later = run(T - 60 + FIFTEEN_MINUTES, TWO, 4, ONE_IDLE, soon.memory)
    assert (later.desired, later.lift_protection) == (1, ("instance-b",))


def test_the_last_instance_is_kept() -> None:
    assert POOL.idle_minutes > 10
    one = Supply(desired=1, in_service=("instance-a",), running=("instance-a",))
    recent = Memory(last_request_at=T - 60, last_in_flight_at=T - 60)
    kept = for_ten_minutes(one, 0, (("instance-a", 0, 0),), recent)
    assert (kept.desired, kept.lift_protection, kept.reason) == (1, (), Reason.UNCHANGED)


@pytest.mark.parametrize("in_flight", [0, 1, 2, 4])
def test_a_busy_instance_is_never_released(in_flight: int) -> None:
    busy = (("instance-a", 1, 0), ("instance-b", 1, 0))
    decision = for_ten_minutes(TWO, in_flight, busy)
    assert decision.memory.low_load_since == T
    assert (decision.desired, decision.lift_protection) == (2, ())
    # One that only has waiting requests, or reports nothing at all, is not known to be idle either.
    for reports in ((("instance-a", 1, 0), ("instance-b", 0, 1)), (("instance-a", 1, 0),)):
        assert for_ten_minutes(TWO, in_flight, reports).desired == 2
