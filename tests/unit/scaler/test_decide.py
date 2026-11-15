"""The scaler's decision (`rhi_scaler.decide`): wake, stop and scale to zero."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import rhi_scaler.decide
from rhi_common.pool_config import DEFAULT_IDLE_MINUTES, parse_pools
from rhi_common.records import FRESH_SECONDS, GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
MINUTE = 60

# A pool that leaves `idle_minutes` out, so it has the default: 20.
POOL = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "max_instances": 2,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]
AT_ZERO = Supply(desired=0)
TWO_IN_SERVICE = Supply(desired=2, in_service=("instance-a", "instance-b"), running=("instance-a", "instance-b"))


def gateway(
    task: str = "task-1",
    *,
    in_flight: int = 0,
    held: int = 0,
    last_request_at: int | None = None,
    written_at: int = NOW,
) -> GatewayHeartbeat:
    return GatewayHeartbeat(
        task=task,
        pool=POOL.name,
        in_flight=in_flight,
        held=held,
        last_request_at=last_request_at,
        written_at=written_at,
    )


def instance(name: str, *, running: int = 0, waiting: int = 0, written_at: int = NOW) -> InstanceHeartbeat:
    return InstanceHeartbeat(
        instance=name,
        pool=POOL.name,
        running=running,
        waiting=waiting,
        image_digest="sha256:" + "ab" * 32,
        model_revision="rev-1",
        stage="serving",
        written_at=written_at,
    )


def pool_inputs(
    supply: Supply,
    *gateways: GatewayHeartbeat,
    mode: Mode = Mode.ENABLED,
    instances: tuple[InstanceHeartbeat, ...] = (),
    memory: Memory = Memory(),  # noqa: B008 - a frozen value, never changed
) -> PoolInputs:
    return PoolInputs(
        config=POOL,
        mode=mode,
        supply=supply,
        gateway_heartbeats=gateways,
        instance_heartbeats=instances,
        memory=memory,
    )


def decide_one(pool: PoolInputs, now: int = NOW) -> PoolDecision:
    (decision,) = decide(Inputs(now=now, pools=(pool,))).pools
    assert decision.pool == POOL.name
    assert isinstance(decision.reason, Reason)
    assert decision.suspend_launches is False
    return decision


def test_twenty_held_requests_ask_for_exactly_one_instance() -> None:
    held = (
        gateway("task-1", held=12, last_request_at=NOW),
        gateway("task-2", held=8, last_request_at=NOW - 3),
    )

    woken = decide_one(pool_inputs(AT_ZERO, *held))

    assert (woken.desired, woken.lift_protection, woken.reason) == (1, (), Reason.WAKE)
    # Once the instance is asked for, the requests still held ask for no other.
    starting = Supply(desired=1, starting=("instance-a",), running=("instance-a",))
    waiting = decide_one(pool_inputs(starting, *held))
    assert (waiting.desired, waiting.lift_protection, waiting.reason) == (1, (), Reason.UNCHANGED)
    # A row that is no longer fresh is not demand.
    stale = gateway(held=20, last_request_at=NOW, written_at=NOW - FRESH_SECONDS - 1)
    assert decide_one(pool_inputs(AT_ZERO, stale)).desired == 0


def test_an_idle_pool_goes_to_zero_at_idle_minutes_naming_both_instances() -> None:
    assert POOL.idle_minutes == DEFAULT_IDLE_MINUTES == 20

    def at(minutes: int) -> PoolDecision:
        # The same request, judged by a run that many minutes after it was received.
        now = NOW + minutes * MINUTE
        return decide_one(pool_inputs(TWO_IN_SERVICE, gateway(last_request_at=NOW, written_at=now)), now)

    kept, released = at(19), at(20)

    assert (kept.desired, kept.lift_protection, kept.reason) == (2, (), Reason.UNCHANGED)
    assert (released.desired, released.reason) == (0, Reason.IDLE)
    assert released.lift_protection == ("instance-a", "instance-b")
    assert kept.memory.last_request_at == released.memory.last_request_at == NOW


def test_going_to_zero_names_every_instance_of_the_pool_in_any_state() -> None:
    supply = Supply(
        desired=1,
        in_service=("instance-a",),
        starting=("instance-b",),
        draining=("instance-c",),
        warm=("instance-d",),
        # One being prepared for the warm pool is in none of the four states.
        running=("instance-a", "instance-b", "instance-c", "instance-e"),
    )

    released = decide_one(pool_inputs(supply, gateway(last_request_at=NOW - 20 * MINUTE)))

    assert released.desired == 0
    assert released.lift_protection == ("instance-a", "instance-b", "instance-c", "instance-d", "instance-e")
    # A pool that is at zero already is left alone, whatever its warm pool holds.
    resting = decide_one(pool_inputs(Supply(desired=0, warm=("instance-d",))))
    assert (resting.desired, resting.lift_protection, resting.reason) == (0, (), Reason.UNCHANGED)


def test_a_request_in_flight_keeps_the_pool() -> None:
    long_ago = NOW - 25 * MINUTE

    busy = decide_one(pool_inputs(TWO_IN_SERVICE, gateway(in_flight=1, last_request_at=long_ago)))

    assert (busy.desired, busy.lift_protection, busy.reason) == (2, (), Reason.UNCHANGED)
    # One request on two instances is a low load too: its 10 minutes start.
    assert busy.memory == Memory(
        last_request_at=long_ago, last_in_flight_at=NOW, low_load_since=NOW, last_gateway_at=NOW
    )

    # The request has ended: the pool is idle from the last run that saw it, not from its arrival.
    def after(minutes: int) -> PoolDecision:
        now = NOW + minutes * MINUTE
        quiet = gateway(last_request_at=long_ago, written_at=now)
        return decide_one(pool_inputs(TWO_IN_SERVICE, quiet, memory=busy.memory), now)

    assert after(19).desired == 2
    assert (after(20).desired, after(20).lift_protection) == (0, ("instance-a", "instance-b"))


def test_a_replaced_gateway_task_is_not_idleness() -> None:
    one = Supply(desired=1, in_service=("instance-a",), running=("instance-a",))
    memory = Memory(last_request_at=NOW - 5 * MINUTE)
    # The rows of the tasks that replaced the old ones: no request in them yet.
    new_tasks = (gateway("task-3"), gateway("task-4"))

    kept = decide_one(pool_inputs(one, *new_tasks, memory=memory))

    assert (kept.desired, kept.lift_protection, kept.reason) == (1, (), Reason.UNCHANGED)
    assert kept.memory.last_request_at == NOW - 5 * MINUTE
    # Without the memory the same rows are an idle pool.
    assert decide_one(pool_inputs(one, *new_tasks)).desired == 0


def test_stopped_pools_are_not_woken() -> None:
    held = gateway(held=3, last_request_at=NOW)
    for mode in (Mode.DISABLED, Mode.BUDGET_LATCHED, Mode.SCHEDULED_ONLY):
        asleep = decide_one(pool_inputs(AT_ZERO, held, mode=mode))
        assert (asleep.desired, asleep.lift_protection, asleep.reason) == (0, (), Reason.NOT_WOKEN)

    # One busy and one idle instance, and a request received this second.
    busy, idle = instance("instance-a", running=1), instance("instance-b")
    demand = gateway(in_flight=1, last_request_at=NOW)
    for mode in (Mode.DISABLED, Mode.BUDGET_LATCHED):
        stopped = decide_one(pool_inputs(TWO_IN_SERVICE, demand, mode=mode, instances=(busy, idle)))
        assert (stopped.desired, stopped.lift_protection, stopped.reason) == (1, ("instance-b",), Reason.STOPPED)
        # Waiting requests, or a report that is no longer fresh, keep an instance.
        for unsure in (
            instance("instance-b", waiting=1),
            instance("instance-b", written_at=NOW - FRESH_SECONDS - 1),
        ):
            kept = decide_one(pool_inputs(TWO_IN_SERVICE, demand, mode=mode, instances=(busy, unsure)))
            assert (kept.desired, kept.lift_protection, kept.reason) == (2, (), Reason.UNCHANGED)
    # Nobody asked to stop a scheduled-only pool: only idleness lowers it.
    scheduled = pool_inputs(TWO_IN_SERVICE, demand, mode=Mode.SCHEDULED_ONLY, instances=(busy, idle))
    assert (decide_one(scheduled).desired, decide_one(scheduled).lift_protection) == (2, ())


def test_the_decision_is_a_function_of_its_inputs_alone() -> None:
    # The module imports the shapes it works on and nothing that could call out or tell the time.
    tree = ast.parse(Path(rhi_scaler.decide.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported == {"__future__", "rhi_common.records", "rhi_scaler.model"}

    # Every pool of the inputs gets its decision, in order, and the same inputs get the same one.
    inputs = Inputs(
        now=NOW,
        pools=(
            pool_inputs(AT_ZERO, gateway(held=1, last_request_at=NOW)),
            pool_inputs(TWO_IN_SERVICE, gateway(last_request_at=NOW - 20 * MINUTE)),
        ),
    )
    decision = decide(inputs)
    assert [(pool.desired, pool.reason) for pool in decision.pools] == [(1, Reason.WAKE), (0, Reason.IDLE)]
    assert decide(dataclasses.replace(inputs)) == decision
