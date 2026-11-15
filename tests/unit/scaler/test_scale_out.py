"""The scaler's decision (`rhi_scaler.decide`): one more instance when those in service are full."""

from __future__ import annotations

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

T = 1_800_000_000
POOL = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "max_instances": 3,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]


def in_service(*names: str, desired: int | None = None, starting: tuple[str, ...] = ()) -> Supply:
    """A supply whose desired capacity, unless given, is what it lists in service."""
    listed = (*names, *starting)
    return Supply(
        desired=len(names) if desired is None else desired, in_service=names, starting=starting, running=listed
    )


def report(name: str, *, running: int, waiting: int = 0) -> tuple[str, int, int]:
    return name, running, waiting


def run(
    now: int,
    supply: Supply,
    *in_flight: int,
    reports: tuple[tuple[str, int, int], ...] = (),
    memory: Memory = Memory(),  # noqa: B008 - a frozen value, never changed
    mode: Mode = Mode.ENABLED,
) -> PoolDecision:
    """One run at `now`: a gateway task per in-flight count and the instances' reports, all written at `now`."""
    pool = PoolInputs(
        config=POOL,
        mode=mode,
        supply=supply,
        gateway_heartbeats=tuple(
            GatewayHeartbeat(f"task-{index}", POOL.name, count, 0, now, now) for index, count in enumerate(in_flight)
        ),
        instance_heartbeats=tuple(
            InstanceHeartbeat(name, POOL.name, running, waiting, "sha256:" + "ab" * 32, "rev-1", "serving", now)
            for name, running, waiting in reports
        ),
        memory=memory,
    )
    (decision,) = decide(Inputs(now=now, pools=(pool,))).pools
    return decision


def test_in_flight_above_target_for_sixty_seconds() -> None:
    two = in_service("instance-a", "instance-b")
    assert POOL.target_concurrency == 8

    # 17 in flight over two gateway tasks and two instances: above 8 each, from the run at T.
    first = run(T, two, 9, 8)
    assert (first.desired, first.reason) == (2, Reason.UNCHANGED)
    early = run(T + 59, two, 9, 8, memory=first.memory)
    assert (early.desired, early.reason) == (2, Reason.UNCHANGED)
    grown = run(T + 60, two, 9, 8, memory=early.memory)
    assert (grown.desired, grown.lift_protection, grown.reason) == (3, (), Reason.SCALE_OUT)
    # The 60 seconds are in the memory: the same run with none sees the load for the first time.
    assert run(T + 60, two, 9, 8).desired == 2
    # A run that sees the pool at its target starts the count again.
    eased = run(T + 30, two, 16, memory=first.memory)
    assert run(T + 60, two, 9, 8, memory=eased.memory).desired == 2

    # 16 in flight is the target and not above it, for as long as it lasts.
    memory = Memory()
    for seconds in range(0, 601, 30):
        level = run(T + seconds, two, 8, 8, memory=memory)
        assert (level.desired, level.memory.above_target_since) == (2, None)
        memory = level.memory

    # With no instance in service there is nothing to average over.
    none = in_service()
    nothing = run(T + 60, none, 17, memory=run(T, none, 17).memory)
    assert (nothing.desired, nothing.memory.above_target_since) == (0, None)
    # A pool somebody stopped does not grow; a scheduled-only pool in use does.
    for mode, desired in ((Mode.DISABLED, 2), (Mode.BUDGET_LATCHED, 2), (Mode.SCHEDULED_ONLY, 3)):
        assert run(T + 60, two, 17, memory=first.memory, mode=mode).desired == desired


def test_waiting_with_no_room_elsewhere() -> None:
    one, two = in_service("instance-a"), in_service("instance-a", "instance-b")
    # Six running and two waiting: the eight in flight are not above the target.
    waits = report("instance-a", running=6, waiting=2)

    def after_sixty_seconds(supply: Supply, *others: tuple[str, int, int]) -> PoolDecision:
        reports = (waits, *others)
        first = run(T, supply, 8, reports=reports)
        early = run(T + 59, supply, 8, reports=reports, memory=first.memory)
        assert (first.desired, early.desired) == (supply.desired, supply.desired)
        return run(T + 60, supply, 8, reports=reports, memory=early.memory)

    alone = after_sixty_seconds(one)
    assert (alone.desired, alone.reason) == (2, Reason.SCALE_OUT)

    # Beside an instance that has room it changes no size, and no time is counted.
    roomy = report("instance-b", running=7)
    beside = after_sixty_seconds(two, roomy)
    assert (beside.desired, beside.reason, beside.memory.waiting_since) == (2, Reason.UNCHANGED, ())
    # An instance at the target, one that waits itself and one that reports nothing have no room.
    for full in ((report("instance-b", running=8),), (report("instance-b", running=1, waiting=1),), ()):
        assert after_sixty_seconds(two, *full).desired == 3

    # The 60 seconds start when the room goes, not when the waiting did.
    no_room = (waits, report("instance-b", running=8))
    gone = run(T + 61, two, 8, reports=no_room, memory=beside.memory)
    assert (gone.desired, gone.memory.waiting_since) == (2, (("instance-a", T + 61),))
    assert run(T + 120, two, 8, reports=no_room, memory=gone.memory).desired == 2
    assert run(T + 121, two, 8, reports=no_room, memory=gone.memory).desired == 3


def test_starting_instances_count() -> None:
    assert POOL.max_instances == 3
    one = in_service("instance-a")
    # Nine in flight on one instance, one of them waiting: both conditions, and still one instance.
    reports = (report("instance-a", running=8, waiting=1),)
    first = run(T, one, 9, reports=reports)
    assert (first.memory.above_target_since, first.memory.waiting_since) == (T, (("instance-a", T),))
    grown = run(T + 60, one, 9, reports=reports, memory=first.memory)
    assert (grown.desired, grown.reason) == (2, Reason.SCALE_OUT)

    # The instance asked for is not in service yet: not listed, then starting. The load goes on.
    memory = grown.memory
    for seconds, supply in (
        (65, in_service("instance-a", desired=2)),
        (120, in_service("instance-a", desired=2, starting=("instance-b",))),
        (600, in_service("instance-a", desired=2, starting=("instance-b",))),
    ):
        pending = run(T + seconds, supply, 9, reports=reports, memory=memory)
        assert (pending.desired, pending.reason) == (2, Reason.UNCHANGED)
        memory = pending.memory

    # At `max_instances`, all in service and above the target for ten minutes: no fourth.
    three = in_service("instance-a", "instance-b", "instance-c")
    capped = run(T + 600, three, 25, memory=run(T, three, 25).memory)
    assert (capped.desired, capped.reason, capped.memory.above_target_since) == (3, Reason.UNCHANGED, T)
