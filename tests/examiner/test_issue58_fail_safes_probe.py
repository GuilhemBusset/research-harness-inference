"""Independent behavioural checks of the scaler fail-safes in issue 58."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Supply

START = 2_000_000
EMPTY_MEMORY = Memory()
CONFIG = parse_pools(
    {"probe": {"engine": "mock", "instance_types": ["g6e.xlarge"], "prices": {"g6e.xlarge": 1.86}, "max_instances": 4,
               "warm_instances": 1, "wake": "on-demand", "target_concurrency": 4,
               "context_length": 4096, "largest_output": 512}}
)[0]


def heartbeat(now: int, *, held: int = 0, load: int = 0) -> GatewayHeartbeat:
    return GatewayHeartbeat("gateway-a", CONFIG.name, load, held, now, now)


def evaluate(pool: PoolInputs, now: int) -> PoolDecision:
    return decide(Inputs(now=now, pools=(pool,))).pools[0]


def cold(now: int, memory: Memory = EMPTY_MEMORY, abandons: int = 0) -> PoolInputs:
    return PoolInputs(
        config=CONFIG, mode=Mode.ENABLED, supply=Supply(0), memory=memory,
        gateway_heartbeats=(heartbeat(now, held=7),), abandoned_launches=abandons,
    )


@pytest.mark.parametrize("mode", list(Mode))
def test_silence_boundary_overrides_busy_instances_and_names_all_states(mode: Mode) -> None:
    supply = Supply(2, in_service=("node-a",), starting=("node-b",), draining=("node-c",),
                    warm=("node-d",), running=("node-a", "node-e"))
    initial = PoolInputs(
        config=CONFIG, mode=mode, supply=supply,
        gateway_heartbeats=(replace(heartbeat(START, load=2), written_at=START - 10), heartbeat(START, load=2)),
    )
    seen = evaluate(initial, START)
    assert seen.memory.last_gateway_at == START
    for elapsed in (599, 600):
        now = START + elapsed
        busy = tuple(
            InstanceHeartbeat(name, CONFIG.name, 1, 0, "sha256:" + "cd" * 32, "revision", "serving", now)
            for name in ("node-a", "node-b")
        )
        # An expired row claiming held requests must neither count as demand nor refresh the clock.
        stale = heartbeat(now - 21, held=10)
        result = evaluate(replace(initial, gateway_heartbeats=(stale,), instance_heartbeats=busy,
                                  memory=seen.memory), now)
        assert result.memory.last_gateway_at == START
        assert result.desired == (2 if elapsed == 599 else 0)
        assert result.lift_protection == (() if elapsed == 599 else
                                          ("node-a", "node-b", "node-c", "node-d", "node-e"))


def test_new_gateway_heartbeat_restarts_silence_clock() -> None:
    pool = replace(cold(START), supply=Supply(1, in_service=("node-a",)))
    first = evaluate(pool, START)
    now = START + 599
    refreshed = evaluate(replace(pool, memory=first.memory, gateway_heartbeats=(heartbeat(now, load=1),)), now)
    assert refreshed.desired == 1
    assert refreshed.memory.last_gateway_at == now
    continued = evaluate(replace(pool, memory=refreshed.memory, gateway_heartbeats=()), START + 600)
    assert continued.desired == 1
    assert continued.memory.last_gateway_at == now


def test_third_abandon_blocks_wake_and_suspension_survives_count_changes() -> None:
    normal = evaluate(cold(START), START)
    assert evaluate(cold(START, abandons=2), START) == normal
    assert normal.desired == 1
    third = evaluate(cold(START, abandons=3), START)
    assert third.desired == 0
    assert third.suspend_launches and third.memory.launch_suspended
    memory = third.memory
    for elapsed, count in ((1, 0), (60, 2), (600, 3)):
        now = START + elapsed
        asleep = evaluate(cold(now, memory, count), now)
        assert asleep.desired == 0
        assert asleep.suspend_launches and asleep.memory.launch_suspended
        memory = asleep.memory
    now = START + 601
    full = replace(cold(now, memory), supply=Supply(1, in_service=("node-a",)),
                   gateway_heartbeats=(heartbeat(now, load=8),),
                   memory=replace(memory, above_target_since=now - 60))
    blocked = evaluate(full, now)
    assert blocked.desired == 1
    assert blocked.suspend_launches and blocked.memory.launch_suspended


def test_admin_clear_resumes_wake_and_counts_only_the_new_row_of_abandons() -> None:
    suspended = evaluate(cold(START, abandons=3), START)
    for now in (START + 60, START + 120):
        cleared = replace(suspended.memory, launch_suspended=False)
        resumed = evaluate(cold(now, cleared, abandons=3), now)
        assert resumed.desired == 1
        assert not resumed.suspend_launches and not resumed.memory.launch_suspended
        assert resumed.memory.abandons_counted_from == now
        # The count boundary is retained rather than reset on each normal invocation.
        two = evaluate(cold(now + 1, resumed.memory, abandons=2), now + 1)
        assert two.desired == 1
        assert not two.suspend_launches
        assert two.memory.abandons_counted_from == now
        suspended = evaluate(cold(now + 2, two.memory, abandons=3), now + 2)
        assert suspended.desired == 0
        assert suspended.suspend_launches and suspended.memory.launch_suspended
        assert suspended.memory.abandons_counted_from == now
