"""The scaler's decision (`rhi_scaler.decide`): each pool at the size its session asks for."""

from __future__ import annotations

import ast
import dataclasses
import itertools
from pathlib import Path

import pytest

import rhi_scaler.decide
from rhi_common.pool_config import parse_pools
from rhi_common.records import FRESH_SECONDS, GatewayHeartbeat, InstanceHeartbeat, Mode, Session
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
            "max_instances": 5,
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


def session(instances: int = 2, *, starts_at: int = NOW - HOUR, ends_at: int = NOW + HOUR) -> Session:
    """A session of the pool, active at `NOW` unless its times say otherwise."""
    return Session(POOL.name, instances, starts_at, ends_at, set_by="instructor", set_at=starts_at - HOUR)


def gateway(
    task: str = "task-1", *, in_flight: int = 0, held: int = 0, last_request_at: int | None = None
) -> GatewayHeartbeat:
    return GatewayHeartbeat(
        task=task, pool=POOL.name, in_flight=in_flight, held=held, last_request_at=last_request_at, written_at=NOW
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


def decide_one(supply: Supply, *, session_max_hours: int = 12, **fields: object) -> PoolDecision:
    """The decision of one run at `NOW` for the pool with that supply, and the inputs `fields` give."""
    pool = dataclasses.replace(PoolInputs(config=POOL, mode=Mode.ENABLED, supply=supply), **fields)
    (decision,) = decide(Inputs(now=NOW, pools=(pool,), session_max_hours=session_max_hours)).pools
    assert decision.pool == POOL.name
    assert isinstance(decision.reason, Reason)
    assert decision.suspend_launches is False
    return decision


@pytest.mark.parametrize(
    ("record", "session_max_hours", "requested"),
    [
        (None, 12, 0),
        (session(2, starts_at=NOW + 1), 12, 0),
        (session(2, starts_at=NOW), 12, 2),
        (session(2), 12, 2),
        # More than the pool's `max_instances`: the pool's.
        (session(9), 12, 5),
        # The end is excluded.
        (session(2, ends_at=NOW), 12, 0),
        # An end later than the longest session: the session ended `session_max_hours` after its start.
        (session(2, starts_at=NOW - 2 * HOUR, ends_at=NOW + HOUR), 3, 2),
        (session(2, starts_at=NOW - 2 * HOUR, ends_at=NOW + HOUR), 2, 0),
    ],
)
def test_the_size_is_what_the_session_asks_for(record: Session | None, session_max_hours: int, requested: int) -> None:
    # A group asked for nothing, one that holds two, and one asked for four that has two.
    for supply in (AT_ZERO, TWO_IN_SERVICE, dataclasses.replace(TWO_IN_SERVICE, desired=4)):
        decision = decide_one(supply, session=record, session_max_hours=session_max_hours)
        assert (decision.requested, decision.desired) == (requested, requested)
        # All of it at once, whatever the group was asked for before.
        if requested > supply.desired:
            assert (decision.reason, decision.lift_protection) == (Reason.PROVISION, ())
        elif requested == supply.desired:
            assert (decision.reason, decision.lift_protection) == (Reason.UNCHANGED, ())
        else:
            assert decision.reason is Reason.RELEASE
            assert decision.lift_protection == supply.in_service[requested:]


def test_release_order() -> None:
    # Four in service, and one still starting whose agent already reports nothing running.
    names = ("instance-a", "instance-b", "instance-c", "instance-d")
    supply = Supply(desired=5, in_service=names, starting=("instance-e",), running=(*names, "instance-e"))
    reports = (
        # By the requests running alone, instance a would leave before instance c.
        instance("instance-a", running=1, waiting=3),
        instance("instance-b"),
        instance("instance-c", running=2),
        instance("instance-d"),
        instance("instance-e"),
    )

    def released(instances: int) -> tuple[str, ...]:
        decision = decide_one(supply, session=session(instances), instance_heartbeats=reports)
        assert (decision.desired, decision.reason) == (instances, Reason.RELEASE)
        return decision.lift_protection

    # Four asked for and four in service: the starting one is no longer asked for, and nobody is named.
    assert released(4) == ()
    # The two that report nothing running and nothing waiting leave before a busy one.
    assert released(3) == ("instance-b",)
    assert released(2) == ("instance-b", "instance-d")
    # Then the busy ones, the fewest requests running and waiting first.
    assert released(1) == ("instance-b", "instance-c", "instance-d")
    # No session: every instance in service, and never the one that is starting.
    ended = decide_one(supply, session=session(2, ends_at=NOW), instance_heartbeats=reports)
    assert (ended.desired, ended.lift_protection, ended.reason) == (0, names, Reason.RELEASE)


def test_an_instance_that_reports_nothing_fresh_is_released_last() -> None:
    stale = NOW - FRESH_SECONDS - 1
    reports = (instance("instance-a", written_at=stale), instance("instance-b", running=6, waiting=2))
    # Instance a reported nothing running, too long ago: what it runs now is not known.
    one = decide_one(TWO_IN_SERVICE, session=session(1), instance_heartbeats=reports)
    assert (one.desired, one.lift_protection) == (1, ("instance-b",))
    silent = decide_one(TWO_IN_SERVICE, session=session(1), instance_heartbeats=reports[1:])
    assert silent.lift_protection == ("instance-b",)


def test_an_instance_still_in_service_after_the_capacity_was_lowered_is_named() -> None:
    # The capacity is already two, and a third instance came into service all the same.
    names = ("instance-a", "instance-b", "instance-c")
    supply = Supply(desired=2, in_service=names, running=names)
    reports = (instance("instance-a", running=2), instance("instance-b", running=1), instance("instance-c"))
    decision = decide_one(supply, session=session(2), instance_heartbeats=reports)
    assert (decision.desired, decision.lift_protection, decision.reason) == (2, ("instance-c",), Reason.RELEASE)


def test_no_rule_reads_demand() -> None:
    long_ago = NOW - 6 * HOUR
    gateways: tuple[tuple[GatewayHeartbeat, ...], ...] = (
        (),
        (gateway(held=12, last_request_at=NOW), gateway("task-2", held=8, last_request_at=NOW)),
        (gateway(in_flight=40, last_request_at=NOW),),
        (gateway(last_request_at=long_ago),),
    )
    memories = (
        Memory(),
        # What the rules of load, of idleness and of the silent gateway kept.
        Memory(
            last_request_at=long_ago,
            last_in_flight_at=long_ago,
            above_target_since=NOW - 10 * MINUTE,
            waiting_since=(("instance-a", NOW - 10 * MINUTE),),
            last_scale_out_at=long_ago,
            low_load_since=long_ago,
            last_gateway_at=NOW - HOUR,
        ),
    )
    # Both instances run as many requests as the target, and more wait on each.
    full = (instance("instance-a", running=8, waiting=4), instance("instance-b", running=8, waiting=4))
    idle = (instance("instance-a"), instance("instance-b"))
    # Each pool: its supply, its session and what its instances report.
    pools: dict[str, tuple[Supply, Session | None, tuple[InstanceHeartbeat, ...]]] = {
        "at zero, no session": (AT_ZERO, None, ()),
        "full, at its session's number": (TWO_IN_SERVICE, session(2), full),
        "idle, at its session's number": (TWO_IN_SERVICE, session(2), idle),
        "above its session's number": (TWO_IN_SERVICE, session(1), idle),
    }

    decisions: dict[str, PoolDecision] = {}
    for name, (supply, record, reports) in pools.items():
        # The same decision, whatever the gateways' heartbeats, the mode and the old memory say.
        seen = {
            decide_one(
                supply,
                session=record,
                instance_heartbeats=reports,
                gateway_heartbeats=heartbeats,
                mode=mode,
                memory=memory,
            )
            for heartbeats, mode, memory in itertools.product(gateways, Mode, memories)
        }
        assert len(seen) == 1, name
        (decisions[name],) = seen
        # And the memory's attributes of load, of idleness and of the gateway are left empty: it
        # holds what the rebalance counts, and nothing else.
        assert dataclasses.replace(decisions[name].memory, queuing_since=()) == Memory(), name

    def outcome(name: str) -> tuple[int, tuple[str, ...], Reason]:
        return decisions[name].desired, decisions[name].lift_protection, decisions[name].reason

    # Held requests wake nothing.
    assert outcome("at zero, no session") == (0, (), Reason.UNCHANGED)
    # A full pool does not grow.
    assert outcome("full, at its session's number") == (2, (), Reason.UNCHANGED)
    # An idle pool stays at its session's number, in any mode, and a silent gateway releases nothing.
    assert outcome("idle, at its session's number") == (2, (), Reason.UNCHANGED)
    # What is released is what the session no longer asks for, and nothing else decides it.
    assert outcome("above its session's number") == (1, ("instance-a",), Reason.RELEASE)


def test_the_reasons() -> None:
    assert {reason.value for reason in Reason} == {
        "provision",
        "release",
        "rebalance",
        "launch-suspended",
        "unchanged",
    }


def test_the_decision_is_a_function_of_its_inputs_alone() -> None:
    # The module imports the shapes it works on and nothing that could call out or tell the time:
    # the shared sessions module, which says what a session asks for, calls nothing either.
    tree = ast.parse(Path(rhi_scaler.decide.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported == {"__future__", "rhi_common.records", "rhi_common.sessions", "rhi_scaler.model"}
    # It reads neither the gateways' heartbeats nor the pool's mode, which the inputs still carry.
    read = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not read & {"gateway_heartbeats", "mode"}

    # Every pool of the inputs gets its decision, in order, and the same inputs get the same one.
    inputs = Inputs(
        now=NOW,
        pools=(
            PoolInputs(config=POOL, mode=Mode.ENABLED, supply=AT_ZERO, session=session(3)),
            PoolInputs(config=POOL, mode=Mode.ENABLED, supply=TWO_IN_SERVICE),
        ),
    )
    decision = decide(inputs)
    assert [(pool.desired, pool.reason) for pool in decision.pools] == [(3, Reason.PROVISION), (0, Reason.RELEASE)]
    assert decide(dataclasses.replace(inputs)) == decision
