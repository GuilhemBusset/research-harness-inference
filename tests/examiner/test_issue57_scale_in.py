"""Independent evidence for issue 57's scale-in decision and memory."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
CONFIG = parse_pools(
    {"probe": {"engine": "mock", "instance_types": ["g6e.xlarge"], "prices": {"g6e.xlarge": 1.86}, "max_instances": 4,
               "warm_instances": 1, "wake": "on-demand", "target_concurrency": 8,
               "context_length": 32768, "largest_output": 4096}}
)[0]


def evaluate(
    now: int,
    load: int,
    memory: Memory,
    reports: tuple[tuple[str, int, int, int], ...],
    count: int = 2,
) -> PoolDecision:
    """Run the public decision function with fresh demand and explicit instance reports."""
    names = tuple(f"node-{index}" for index in range(count))
    pool = PoolInputs(
        config=CONFIG,
        mode=Mode.ENABLED,
        supply=Supply(desired=count, in_service=names, running=names),
        gateway_heartbeats=(GatewayHeartbeat("gateway", "probe", load, 0, now, now),),
        instance_heartbeats=tuple(
            InstanceHeartbeat(name, "probe", running, waiting, "sha256:" + "ab" * 32, "rev", "serving", now - age)
            for name, running, waiting, age in reports
        ),
        memory=memory,
    )
    return decide(Inputs(now=now, pools=(pool,))).pools[0]


@pytest.mark.parametrize(("count", "load", "expected"), [(2, 4, 1), (2, 5, 2), (3, 8, 2), (3, 9, 3)])
def test_threshold_and_ten_minute_boundary(count: int, load: int, expected: int) -> None:
    reports = tuple((f"node-{index}", load if index == 0 else 0, 0, 0) for index in range(count))
    first = evaluate(NOW, load, Memory(), reports, count)
    before = evaluate(NOW + 599, load, first.memory, reports, count)
    assert before.desired == count
    assert before.lift_protection == ()
    after = evaluate(NOW + 600, load, before.memory, reports, count)
    assert after.desired == expected
    assert after.lift_protection == (("node-1",) if expected < count else ())


def test_low_load_interruption_restarts_memory() -> None:
    reports = (("node-0", 4, 0, 0), ("node-1", 0, 0, 0))
    first = evaluate(NOW, 4, Memory(), reports)
    assert first.memory.low_load_since == NOW
    high = evaluate(NOW + 300, 5, first.memory, reports)
    assert high.memory.low_load_since is None
    restart = evaluate(NOW + 600, 4, high.memory, reports)
    assert restart.desired == 2
    assert restart.memory.low_load_since == NOW + 600
    removed = evaluate(NOW + 1200, 4, restart.memory, reports)
    assert (removed.desired, removed.lift_protection) == (1, ("node-1",))


@pytest.mark.parametrize("load", [0, 4, 5, 17])
def test_running_or_waiting_or_unknown_instances_are_kept(load: int) -> None:
    memory = Memory(low_load_since=NOW - 600)
    for reports in (
        (("node-0", 1, 0, 0), ("node-1", 1, 0, 0)),
        (("node-0", 1, 0, 0), ("node-1", 0, 1, 0)),
        (("node-0", 1, 0, 0),),
        (("node-0", 1, 0, 0), ("node-1", 0, 0, 21)),
    ):
        result = evaluate(NOW, load, memory, reports)
        assert result.desired == 2
        assert result.lift_protection == ()


def test_scale_out_timestamp_survives_until_cooldown_expires() -> None:
    reports = (("node-0", 4, 0, 0), ("node-1", 0, 0, 0))
    grown = evaluate(NOW, 17, Memory(above_target_since=NOW - 60), reports)
    assert grown.reason == Reason.SCALE_OUT
    assert grown.memory.last_scale_out_at == NOW
    low = evaluate(NOW + 1, 4, grown.memory, reports)
    assert low.memory.low_load_since == NOW + 1
    before = evaluate(NOW + 899, 4, low.memory, reports)
    assert before.desired == 2
    assert before.memory.last_scale_out_at == NOW
    after = evaluate(NOW + 900, 4, before.memory, reports)
    assert (after.desired, after.lift_protection) == (1, ("node-1",))
    assert after.memory.last_scale_out_at == NOW


def test_single_instance_does_not_leave_by_scale_in() -> None:
    assert CONFIG.idle_minutes > 10
    first = evaluate(NOW, 0, Memory(), (("node-0", 0, 0, 0),), count=1)
    after = evaluate(NOW + 600, 0, replace(first.memory, last_request_at=NOW),
                     (("node-0", 0, 0, 0),), count=1)
    assert after.desired == 1
    assert after.lift_protection == ()
