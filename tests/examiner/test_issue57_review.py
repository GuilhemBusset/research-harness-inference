"""Independent review of the scale-in threshold, timing and release selection."""

from __future__ import annotations

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

START = 1_800_000_000
CONFIG = parse_pools(
    {"review": {"engine": "mock", "instance_types": ["g6e.xlarge"], "max_instances": 4,
                "warm_instances": 1, "wake": "on-demand", "target_concurrency": 8,
                "context_length": 32768, "largest_output": 4096}}
)[0]


def run(
    now: int,
    count: int,
    load: int,
    memory: Memory,
    reports: tuple[tuple[str, int, int, int], ...],
) -> PoolDecision:
    """Evaluate actual decision code; report ages make freshness explicit."""
    names = tuple(f"node-{index}" for index in range(count))
    pool = PoolInputs(
        config=CONFIG, mode=Mode.ENABLED, memory=memory,
        supply=Supply(desired=count, in_service=names),
        gateway_heartbeats=(
            GatewayHeartbeat("gateway-a", CONFIG.name, load // 2, 0, now, now),
            GatewayHeartbeat("gateway-b", CONFIG.name, load - load // 2, 0, now, now),
        ),
        instance_heartbeats=tuple(
            InstanceHeartbeat(name, CONFIG.name, running, waiting, "sha256:" + "ab" * 32,
                              "revision", "serving", now - age)
            for name, running, waiting, age in reports
        ),
    )
    return decide(Inputs(now=now, pools=(pool,))).pools[0]


@pytest.mark.parametrize(("count", "load", "removes"), [(2, 4, True), (2, 5, False), (3, 8, True), (3, 9, False)])
def test_load_and_duration(count: int, load: int, removes: bool) -> None:
    reports = (("node-0", load, 0, 0), ("node-1", 0, 0, 0))
    first = run(START, count, load, Memory(), reports)
    early = run(START + 599, count, load, first.memory, reports)
    assert (early.desired, early.lift_protection) == (count, ())
    final = run(START + 600, count, load, early.memory, reports)
    assert final.desired == count - int(removes)
    assert final.lift_protection == (("node-1",) if removes else ())


def test_low_load_must_be_continuous() -> None:
    reports = (("node-0", 4, 0, 0), ("node-1", 0, 0, 0))
    first = run(START, 2, 4, Memory(), reports)
    assert first.memory.low_load_since == START
    high = run(START + 300, 2, 5, first.memory, reports)
    assert high.memory.low_load_since is None
    restart = run(START + 600, 2, 4, high.memory, reports)
    assert (restart.desired, restart.memory.low_load_since) == (2, START + 600)
    assert run(START + 1200, 2, 4, restart.memory, reports).desired == 1


def test_scale_out_time_is_recorded_and_enforces_cooldown() -> None:
    reports = (("node-0", 8, 0, 0), ("node-1", 8, 0, 0))
    grown = run(START, 2, 17, Memory(above_target_since=START - 60), reports)
    assert (grown.desired, grown.reason, grown.memory.last_scale_out_at) == (3, Reason.SCALE_OUT, START)
    idle = (("node-0", 4, 0, 0), ("node-1", 0, 0, 0), ("node-2", 1, 0, 0))
    low = run(START + 1, 3, 5, grown.memory, idle)
    before = run(START + 899, 3, 5, low.memory, idle)
    assert (before.desired, before.lift_protection, before.memory.last_scale_out_at) == (3, (), START)
    after = run(START + 900, 3, 5, before.memory, idle)
    assert (after.desired, after.lift_protection, after.memory.last_scale_out_at) == (2, ("node-1",), START)


@pytest.mark.parametrize("load", [0, 1, 4, 5, 17])
@pytest.mark.parametrize("candidate", [None, (1, 0, 0), (0, 1, 0), (0, 0, 21)])
def test_no_release_without_known_idle_instance(load: int, candidate: tuple[int, int, int] | None) -> None:
    reports = (("node-0", 1, 0, 0),)
    if candidate is not None:
        reports += (("node-1", *candidate),)
    result = run(START, 2, load, Memory(low_load_since=START - 600), reports)
    assert (result.desired, result.lift_protection) == (2, ())


def test_single_instance_survives_ten_minutes() -> None:
    first = run(START, 1, 0, Memory(), (("node-0", 0, 0, 0),))
    after = run(START + 600, 1, 0, first.memory, (("node-0", 0, 0, 0),))
    assert (after.desired, after.lift_protection) == (1, ())
