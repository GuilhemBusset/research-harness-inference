"""Independent behavioural checks for issue 56's scale-out rules."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Supply

START = 3_000_000
CONFIG = parse_pools(
    {
        "examiner": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "prices": {"g6e.xlarge": 1.86},
            "max_instances": 3,
            "warm_instances": 0,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 4096,
            "largest_output": 512,
        }
    }
)[0]


def snapshot(
    now: int,
    supply: Supply,
    counts: tuple[int, ...],
    reports: tuple[tuple[str, int, int], ...] = (),
) -> PoolInputs:
    return PoolInputs(
        config=CONFIG,
        mode=Mode.ENABLED,
        supply=supply,
        gateway_heartbeats=tuple(
            GatewayHeartbeat(f"task-{index}", CONFIG.name, count, 0, now, now)
            for index, count in enumerate(counts)
        ),
        instance_heartbeats=tuple(
            InstanceHeartbeat(name, CONFIG.name, running, waiting, "sha256:" + "ef" * 32, "rev", "serving", now)
            for name, running, waiting in reports
        ),
    )


def evaluate(now: int, pool: PoolInputs, memory: Memory | None = None) -> PoolDecision:
    if memory is not None:
        pool = replace(pool, memory=memory)
    return decide(Inputs(now, (pool,))).pools[0]


@pytest.mark.parametrize(("count", "elapsed", "expected"), [(17, 59, 2), (17, 60, 3), (16, 600, 2)])
def test_average_boundary_and_input_memory(count: int, elapsed: int, expected: int) -> None:
    supply = Supply(2, in_service=("alpha", "beta"))
    first = evaluate(START, snapshot(START, supply, (8, count - 8)))
    later = snapshot(START + elapsed, supply, (8, count - 8))
    assert evaluate(START + elapsed, later, first.memory).desired == expected
    assert evaluate(START + elapsed, later).desired == 2
    assert evaluate(START + elapsed, later, first.memory) == evaluate(START + elapsed, later, first.memory)
    assert later.memory == Memory()


def test_only_fresh_demand_counts_and_zero_has_no_average() -> None:
    supply = Supply(2, in_service=("alpha", "beta"))
    memory: Memory | None = None
    for elapsed in (0, 60, 600):
        now = START + elapsed
        pool = snapshot(now, supply, (8, 8))
        stale = GatewayHeartbeat("gone", CONFIG.name, 100, 0, now - 21, now - 21)
        pool = replace(pool, gateway_heartbeats=(*pool.gateway_heartbeats, stale))
        result = evaluate(now, pool, memory)
        assert result.desired == 2
        memory = result.memory
    zero = Supply(0)
    first = evaluate(START, snapshot(START, zero, (100,)))
    assert evaluate(START + 60, snapshot(START + 60, zero, (100,)), first.memory).desired == 0


def test_average_timer_restarts_after_load_drops() -> None:
    supply = Supply(2, in_service=("alpha", "beta"))
    memory = evaluate(START, snapshot(START, supply, (17,))).memory
    memory = evaluate(START + 30, snapshot(START + 30, supply, (16,)), memory).memory
    memory = evaluate(START + 60, snapshot(START + 60, supply, (17,)), memory).memory
    assert evaluate(START + 119, snapshot(START + 119, supply, (17,)), memory).desired == 2
    assert evaluate(START + 120, snapshot(START + 120, supply, (17,)), memory).desired == 3


@pytest.mark.parametrize(
    ("names", "other", "expected"),
    [
        (("alpha",), (), 2),
        (("alpha", "beta"), (("beta", 7, 0),), 2),
        (("alpha", "beta"), (("beta", 8, 0),), 3),
        (("alpha", "beta"), (("beta", 1, 1),), 3),
    ],
)
def test_waiting_and_room(
    names: tuple[str, ...], other: tuple[tuple[str, int, int], ...], expected: int
) -> None:
    supply = Supply(len(names), in_service=names)
    reports = (("alpha", 6, 2), *other)
    memory = evaluate(START, snapshot(START, supply, (8,), reports)).memory
    assert evaluate(START + 59, snapshot(START + 59, supply, (8,), reports), memory).desired == len(names)
    assert evaluate(START + 60, snapshot(START + 60, supply, (8,), reports), memory).desired == expected


def test_room_and_stale_reports_break_waiting_duration() -> None:
    supply = Supply(2, in_service=("alpha", "beta"))
    busy = (("alpha", 7, 1), ("beta", 8, 0))
    room = (("alpha", 7, 1), ("beta", 7, 0))
    memory = evaluate(START, snapshot(START, supply, (8,), busy)).memory
    reset = evaluate(START + 30, snapshot(START + 30, supply, (8,), room), memory)
    assert reset.memory.waiting_since == ()
    resumed = evaluate(START + 60, snapshot(START + 60, supply, (8,), busy), reset.memory)
    assert resumed.desired == 2
    assert evaluate(START + 119, snapshot(START + 119, supply, (8,), busy), resumed.memory).desired == 2
    assert evaluate(START + 120, snapshot(START + 120, supply, (8,), busy), resumed.memory).desired == 3
    stale = snapshot(START, supply, (8,), busy).instance_heartbeats
    later = replace(snapshot(START + 60, supply, (8,)), instance_heartbeats=stale)
    stale_result = evaluate(START + 60, later, memory)
    assert stale_result.desired == 2
    assert stale_result.memory.waiting_since == ()


def test_one_addition_pending_supply_and_cap() -> None:
    supply = Supply(1, in_service=("alpha",))
    reports = (("alpha", 8, 1),)
    memory = evaluate(START, snapshot(START, supply, (9,), reports)).memory
    grown = evaluate(START + 60, snapshot(START + 60, supply, (9,), reports), memory)
    assert grown.desired == 2
    for pending in (Supply(2, in_service=("alpha",)), Supply(2, in_service=("alpha",), starting=("beta",))):
        assert evaluate(START + 600, snapshot(START + 600, pending, (9,), reports), grown.memory).desired == 2
    capped = Supply(3, in_service=("alpha", "beta", "gamma"))
    memory = evaluate(START, snapshot(START, capped, (25,), reports)).memory
    assert evaluate(START + 60, snapshot(START + 60, capped, (25,), reports), memory).desired == 3
