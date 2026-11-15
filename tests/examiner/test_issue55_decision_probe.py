"""Independent checks of issue 55's scaler transitions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 2_000_000
POOL = parse_pools(
    {
        "probe": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "max_instances": 2,
            "warm_instances": 0,
            "wake": "on-demand",
            "target_concurrency": 4,
            "context_length": 4096,
            "largest_output": 512,
        }
    }
)[0]


def gateway(*, last: int | None = None, held: int = 0, flying: int = 0) -> GatewayHeartbeat:
    return GatewayHeartbeat("task-probe", POOL.name, flying, held, last, NOW)


def engine(name: str, running: int = 0) -> InstanceHeartbeat:
    return InstanceHeartbeat(name, POOL.name, running, 0, "sha256:" + "cd" * 32, "revision", "serving", NOW)


def base() -> PoolInputs:
    return PoolInputs(
        config=POOL,
        mode=Mode.ENABLED,
        supply=Supply(2, in_service=("worker-a", "worker-b"), running=("worker-a", "worker-b")),
        gateway_heartbeats=(gateway(last=NOW - 1200),),
    )


def run(pool: PoolInputs) -> PoolDecision:
    result = decide(Inputs(now=NOW, pools=(pool,)))
    assert len(result.pools) == 1
    decision = result.pools[0]
    assert decision.pool == POOL.name
    assert isinstance(decision.reason, Reason)
    assert isinstance(decision.suspend_launches, bool)
    return decision


def test_wake_is_one_and_pending_supply_is_preserved() -> None:
    demand = (gateway(last=NOW, held=7), replace(gateway(last=NOW, held=13), task="other-task"))
    pool = replace(base(), supply=Supply(0), gateway_heartbeats=demand)
    awake = run(pool)
    assert awake.desired == 1
    assert awake.lift_protection == ()
    assert run(replace(pool, supply=Supply(1, starting=("worker-a",)))).desired == 1


def test_idle_boundary_and_all_instance_states() -> None:
    pool = replace(
        base(),
        supply=Supply(
            2,
            in_service=("worker-a",),
            starting=("worker-b",),
            draining=("worker-c",),
            warm=("worker-d",),
            running=("worker-a", "worker-e"),
        ),
    )
    recent = run(replace(pool, gateway_heartbeats=(gateway(last=NOW - 1140),)))
    assert recent.desired == 2
    assert recent.lift_protection == ()
    idle = run(pool)
    assert idle.desired == 0
    assert set(idle.lift_protection) == {"worker-a", "worker-b", "worker-c", "worker-d", "worker-e"}
    assert idle.memory.last_request_at == NOW - 1200


def test_in_flight_request_prevents_zero() -> None:
    decision = run(replace(base(), gateway_heartbeats=(gateway(last=NOW - 1800, flying=1),)))
    assert decision.desired == 2
    assert decision.lift_protection == ()


def test_memory_survives_gateway_replacement_and_keeps_the_latest_time() -> None:
    pool = replace(base(), gateway_heartbeats=(gateway(),), memory=Memory(last_request_at=NOW - 300))
    decision = run(pool)
    assert decision.desired == 2
    assert decision.memory.last_request_at == NOW - 300
    older_gateway = replace(pool, gateway_heartbeats=(gateway(last=NOW - 600),))
    assert run(older_gateway).memory.last_request_at == NOW - 300
    newer_gateway = replace(pool, gateway_heartbeats=(gateway(last=NOW - 60),))
    assert run(newer_gateway).memory.last_request_at == NOW - 60
    stale_gateway = replace(gateway(last=NOW), written_at=NOW - 21)
    assert run(replace(pool, gateway_heartbeats=(stale_gateway,))).memory.last_request_at == NOW - 300


@pytest.mark.parametrize("mode", [Mode.DISABLED, Mode.BUDGET_LATCHED, Mode.SCHEDULED_ONLY])
def test_non_enabled_modes_cannot_wake(mode: Mode) -> None:
    decision = run(replace(base(), mode=mode, supply=Supply(0), gateway_heartbeats=(gateway(held=20),)))
    assert decision.desired == 0
    assert decision.lift_protection == ()


@pytest.mark.parametrize("mode", [Mode.DISABLED, Mode.BUDGET_LATCHED])
def test_stop_releases_idle_instances_immediately(mode: Mode) -> None:
    pool = replace(
        base(),
        mode=mode,
        gateway_heartbeats=(gateway(last=NOW, flying=1),),
        instance_heartbeats=(engine("worker-a", running=1), engine("worker-b")),
    )
    decision = run(pool)
    assert decision.desired == 1
    assert decision.lift_protection == ("worker-b",)


@pytest.mark.parametrize("mode", [Mode.DISABLED, Mode.BUDGET_LATCHED])
def test_stop_preserves_a_busy_instance_even_after_the_idle_deadline(mode: Mode) -> None:
    # Gateway demand may have ended while an engine is still working, for example after disconnect.
    pool = replace(
        base(),
        mode=mode,
        instance_heartbeats=(engine("worker-a", running=1), engine("worker-b")),
    )
    decision = run(pool)
    assert decision.desired == 1
    assert decision.lift_protection == ("worker-b",)


def test_decision_is_repeatable_and_does_not_mutate_inputs() -> None:
    pool = replace(base(), abandoned_launches=2, memory=Memory(last_request_at=NOW - 60))
    inputs = Inputs(now=NOW, pools=(pool,))
    copy = replace(inputs, pools=(replace(pool, memory=replace(pool.memory)),))
    first = decide(inputs)
    assert inputs == copy
    assert decide(copy) == first
    assert first.pools[0].desired == 2
    assert decide(replace(inputs, now=NOW + 1200)).pools[0].desired == 0
