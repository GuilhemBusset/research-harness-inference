"""The scaler's fail-safes (`rhi_scaler.decide`): a silent gateway, and a launch that fails every time."""

from __future__ import annotations

from dataclasses import replace

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
MINUTE = 60

POOL = parse_pools(
    {
        "coursework": {
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
    }
)[0]
AT_ZERO = Supply(desired=0)
ONE = Supply(desired=1, in_service=("instance-a",), warm=("instance-w",), running=("instance-a",))


def gateway(now: int, *, held: int = 0, in_flight: int = 0) -> GatewayHeartbeat:
    return GatewayHeartbeat("task-1", POOL.name, in_flight, held, now, now)


def busy(now: int) -> InstanceHeartbeat:
    return InstanceHeartbeat("instance-a", POOL.name, 3, 0, "sha256:" + "ab" * 32, "rev-1", "serving", now)


def run(supply: Supply, now: int = NOW, *gateways: GatewayHeartbeat, **fields: object) -> PoolDecision:
    pool = replace(PoolInputs(config=POOL, mode=Mode.ENABLED, supply=supply, gateway_heartbeats=gateways), **fields)
    return decide(Inputs(now=now, pools=(pool,))).pools[0]


def test_a_silent_gateway_takes_the_pool_to_zero() -> None:
    # The last gateway heartbeat was seen at NOW and kept in the memory; none is fresh after it.
    seen = run(ONE, NOW, gateway(NOW, in_flight=3))
    assert seen.memory.last_gateway_at == NOW
    for mode in (Mode.ENABLED, Mode.DISABLED):
        later = NOW + 10 * MINUTE
        silent = run(ONE, later, mode=mode, instance_heartbeats=(busy(later),), memory=seen.memory)
        # Zero even with an instance busy, every instance named.
        assert (silent.desired, silent.reason) == (0, Reason.GATEWAY_SILENT)
        assert silent.lift_protection == ("instance-a", "instance-w")
        assert silent.memory.last_gateway_at == NOW
        sooner = NOW + 10 * MINUTE - 1
        kept = run(ONE, sooner, mode=mode, instance_heartbeats=(busy(sooner),), memory=seen.memory)
        assert kept.desired == 1
    # A pool at zero already is left alone.
    assert run(AT_ZERO, NOW + 60 * MINUTE, memory=seen.memory).reason is Reason.UNCHANGED


def test_two_abandoned_launches_change_nothing_and_the_third_suspends() -> None:
    demand = gateway(NOW, in_flight=1)
    unchanged = run(ONE, NOW, demand)
    assert run(ONE, NOW, demand, abandoned_launches=2) == unchanged
    assert not unchanged.suspend_launches

    third = run(ONE, NOW, demand, abandoned_launches=3)
    assert (third.desired, third.suspend_launches, third.reason) == (1, True, Reason.LAUNCH_SUSPENDED)
    assert (third.memory.launch_suspended, third.memory.suspended_at) == (True, NOW)
    # A suspended pool that is full does not grow either.
    full = run(
        ONE, NOW + MINUTE, gateway(NOW + MINUTE, in_flight=8), memory=replace(third.memory, above_target_since=NOW)
    )
    assert (full.desired, full.suspend_launches, full.reason) == (1, True, Reason.LAUNCH_SUSPENDED)


def test_a_suspended_pool_stays_suspended() -> None:
    memory = run(AT_ZERO, NOW, abandoned_launches=3).memory
    assert memory.launch_suspended
    # Held requests, run after run, with the abandon count back to zero: nothing wakes the pool.
    for minutes in (1, 5, 60):
        now = NOW + minutes * MINUTE
        asleep = run(AT_ZERO, now, gateway(now, held=4), memory=memory)
        assert (asleep.desired, asleep.suspend_launches, asleep.reason) == (0, True, Reason.LAUNCH_SUSPENDED)
        memory = asleep.memory
        assert memory.launch_suspended

    # The admin clears the mark. The edge still counts the three old abandons.
    now = NOW + 90 * MINUTE
    woken = run(
        AT_ZERO, now, gateway(now, held=1), abandoned_launches=3, memory=replace(memory, launch_suspended=False)
    )
    assert (woken.desired, woken.suspend_launches, woken.reason) == (1, False, Reason.WAKE)
    assert woken.memory.abandons_counted_from == now
    assert not woken.memory.launch_suspended


def test_only_three_new_abandons_suspend_again() -> None:
    cleared_at = NOW + 90 * MINUTE
    memory = Memory(abandons_counted_from=cleared_at, last_gateway_at=NOW)
    two = run(ONE, NOW + 100 * MINUTE, gateway(NOW + 100 * MINUTE), abandoned_launches=2, memory=memory)
    assert not two.suspend_launches
    assert two.memory.abandons_counted_from == cleared_at
    three = run(ONE, NOW + 110 * MINUTE, gateway(NOW + 110 * MINUTE), abandoned_launches=3, memory=two.memory)
    assert three.suspend_launches
    assert three.memory.launch_suspended
