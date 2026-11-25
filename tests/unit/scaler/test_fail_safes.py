"""The scaler's launch loop (`rhi_scaler.decide`): a launch that fails every time."""

from __future__ import annotations

from dataclasses import replace

from rhi_common.pool_config import parse_pools
from rhi_common.records import Mode, Session
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
MINUTE = 60
HOUR = 3600

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
ONE = Supply(desired=1, in_service=("instance-a",), draining=("instance-w",), running=("instance-a", "instance-w"))


def session(instances: int = 1) -> Session:
    """A session of the pool, active through every run below."""
    return Session(POOL.name, instances, NOW - HOUR, NOW + 6 * HOUR, set_by="instructor", set_at=NOW - 2 * HOUR)


def run(supply: Supply, now: int = NOW, **fields: object) -> PoolDecision:
    """One run at `now` of a pool whose session asks for one instance, unless `fields` give another."""
    pool = replace(PoolInputs(config=POOL, mode=Mode.ENABLED, supply=supply, session=session()), **fields)
    return decide(Inputs(now=now, pools=(pool,))).pools[0]


def test_two_abandoned_launches_change_nothing_and_the_third_suspends() -> None:
    unchanged = run(ONE, NOW)
    assert run(ONE, NOW, abandoned_launches=2) == unchanged
    assert not unchanged.suspend_launches

    third = run(ONE, NOW, abandoned_launches=3)
    assert (third.desired, third.suspend_launches, third.reason) == (1, True, Reason.LAUNCH_SUSPENDED)
    assert (third.memory.launch_suspended, third.memory.suspended_at) == (True, NOW)
    # A suspended pool whose session asks for more is not raised, in that run or after it.
    more = run(ONE, NOW, session=session(3), abandoned_launches=3)
    assert (more.requested, more.desired, more.suspend_launches, more.reason) == (3, 1, True, Reason.LAUNCH_SUSPENDED)
    later = run(ONE, NOW + MINUTE, session=session(3), memory=third.memory)
    assert (later.desired, later.suspend_launches, later.reason) == (1, True, Reason.LAUNCH_SUSPENDED)
    # Nor is it when more instances are in service than the session asks for: the one beyond its
    # number is named (none reports its load, so the first by name), and the reason is still that
    # the pool is not raised.
    names = ("instance-a", "instance-b", "instance-c")
    above = run(Supply(desired=1, in_service=names, running=names), NOW + MINUTE, session=session(2), memory=third.memory)
    assert (above.desired, above.lift_protection, above.reason) == (1, ("instance-a",), Reason.LAUNCH_SUSPENDED)


def test_a_suspended_pool_stays_suspended() -> None:
    memory = run(AT_ZERO, NOW, abandoned_launches=3).memory
    assert memory.launch_suspended
    # The session asks for an instance, run after run, with the abandon count back to zero: the
    # pool is not raised.
    for minutes in (1, 5, 60):
        now = NOW + minutes * MINUTE
        asleep = run(AT_ZERO, now, memory=memory)
        assert (asleep.desired, asleep.suspend_launches, asleep.reason) == (0, True, Reason.LAUNCH_SUSPENDED)
        memory = asleep.memory
        assert memory.launch_suspended
    # A size that goes down still does: the session is over, and the instance in service leaves.
    over = run(ONE, NOW + 61 * MINUTE, session=None, memory=memory)
    assert (over.desired, over.lift_protection, over.reason) == (0, ("instance-a",), Reason.RELEASE)
    assert over.suspend_launches and over.memory.launch_suspended

    # The admin clears the mark. The edge still counts the three old abandons.
    now = NOW + 90 * MINUTE
    raised = run(AT_ZERO, now, abandoned_launches=3, memory=replace(memory, launch_suspended=False))
    assert (raised.desired, raised.suspend_launches, raised.reason) == (1, False, Reason.PROVISION)
    assert raised.memory.abandons_counted_from == now
    assert not raised.memory.launch_suspended


def test_only_three_new_abandons_suspend_again() -> None:
    cleared_at = NOW + 90 * MINUTE
    memory = Memory(abandons_counted_from=cleared_at)
    two = run(ONE, NOW + 100 * MINUTE, abandoned_launches=2, memory=memory)
    assert not two.suspend_launches
    assert two.memory.abandons_counted_from == cleared_at
    three = run(ONE, NOW + 110 * MINUTE, abandoned_launches=3, memory=two.memory)
    assert three.suspend_launches
    assert three.memory.launch_suspended
