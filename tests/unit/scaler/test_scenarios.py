"""The scaler's scenarios of section 5.1, played over time against a simulated pool (`sim.py`)."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import Session
from rhi_scaler.model import Reason

# The simulator sits beside this file; the tests tree has no packages, so it is loaded by path.
_spec = importlib.util.spec_from_file_location("scaler_sim", Path(__file__).with_name("sim.py"))
assert _spec is not None and _spec.loader is not None
sim = importlib.util.module_from_spec(_spec)
sys.modules["scaler_sim"] = sim
_spec.loader.exec_module(sim)

T0 = 1_800_000_000  # a whole minute
MINUTE = 60
HOUR = 3600
POOL = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "prices": {"g6e.xlarge": 1.86},
            "max_instances": 4,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 4,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]


def session(instances: int, *, starts_at: int = T0, ends_at: int = T0 + 4 * HOUR) -> Session:
    """The pool's session record, written an hour before the scenario begins."""
    return Session(POOL.name, instances, starts_at, ends_at, set_by="instructor", set_at=T0 - HOUR)


def test_a_session_that_starts() -> None:
    starts_at, ends_at = T0 + 10 * MINUTE, T0 + 2 * HOUR
    pool = sim.Sim(POOL, start=T0, session=session(3, starts_at=starts_at, ends_at=ends_at))
    # Booked and not yet active: run after run, nothing is asked for.
    pool.advance(10 * MINUTE - 1)
    assert len(pool.runs) == 9 and pool.launches == []
    assert {(run.decision.desired, run.decision.reason) for run in pool.runs} == {(0, Reason.UNCHANGED)}

    # The run at its start asks for all three at once.
    pool.advance(1)
    first = pool.runs[-1]
    assert first.now == starts_at
    assert (first.decision.desired, first.decision.reason, first.decision.requested) == (3, Reason.PROVISION, 3)
    assert [launch.now for launch in pool.launches] == [starts_at] * 3

    # No request ever comes, and the pool holds its three instances to the last second.
    pool.advance(ends_at - 1 - pool.now)
    assert len(pool.in_service()) == 3 and len(pool.launches) == 3 and pool.released == []
    held = [run.decision for run in pool.runs if run.now > starts_at]
    assert {(run.desired, run.lift_protection, run.reason) for run in held} == {(3, (), Reason.UNCHANGED)}
    # At its end the pool goes to zero.
    pool.advance(1 + MINUTE)
    assert (pool.desired, pool.instances) == (0, {})


def test_a_session_raised_while_instances_wait_for_capacity() -> None:
    # A session of two, and AWS has room for one instance.
    pool = sim.Sim(POOL, start=T0, session=session(2), capacity=1)
    pool.advance(10 * MINUTE)
    assert (pool.desired, len(pool.in_service()), pool.starting()) == (2, 1, [])

    # Raised to four while one is still waited for: all four are asked for in that run.
    before = len(pool.runs)
    raised = pool.write_session(replace(pool.session, instances=4))
    assert (raised.desired, raised.reason, raised.requested) == (4, Reason.PROVISION, 4)
    # Two hours without capacity: the pool goes on asking, and nothing is abandoned or suspended.
    pool.advance(2 * HOUR)
    assert len(pool.launches) == len(pool.in_service()) == 1
    assert {run.decision.desired for run in pool.runs[before:]} == {4}
    assert [outcome for _, outcome in pool.history] == ["in-service"]
    assert not any(run.decision.suspend_launches for run in pool.runs)

    # The capacity comes: the three others are launched, with no run of the scaler in between.
    runs = len(pool.runs)
    pool.capacity = None
    pool.advance(1)
    assert len(pool.launches) == 4 and len(pool.runs) == runs
    pool.advance(pool.start_seconds)
    assert len(pool.in_service()) == 4


@pytest.mark.parametrize("takes_starting", [True, False])
def test_a_session_lowered_under_load(takes_starting: bool) -> None:
    # Three in service and a session of four: the fourth instance is starting.
    pool = sim.Sim(POOL, start=T0, in_service=3, session=session(4), takes_starting=takes_starting)
    pool.advance(MINUTE)
    assert pool.starting() == ["instance-4"]
    # Three requests on instance 1, one on instance 2, none on instance 3.
    busy = [pool.request(10 * MINUTE, "instance-1") for _ in range(3)]
    light = pool.request(4 * MINUTE, "instance-2")
    pool.advance(10)

    # Lowered to two: of the three in service, the one that reports nothing leaves.
    first = pool.write_session(replace(pool.session, instances=2))
    assert (first.desired, first.lift_protection, first.reason) == (2, ("instance-3",), Reason.RELEASE)
    pool.advance(MINUTE)
    # Lowered to one: of the two left, the one with the fewest requests.
    second = pool.write_session(replace(pool.session, instances=1))
    assert (second.desired, second.lift_protection, second.reason) == (1, ("instance-2",), Reason.RELEASE)

    pool.advance(15 * MINUTE)
    # The pool is at what the session asks for, and every request that was running was served:
    # the one on the released instance within its drain.
    assert (pool.desired, pool.in_service(), list(pool.instances)) == (1, ["instance-1"], ["instance-1"])
    assert {request.outcome for request in (*busy, light)} == {"served"}
    # No run named an instance that was still starting, whatever the group did with it.
    assert all(not set(run.decision.lift_protection) & set(run.supply.starting) for run in pool.runs)
    if takes_starting:
        # The group took the starting instance itself when the capacity was lowered.
        assert [name for _, name in pool.taken] == ["instance-4"]
        assert [name for _, name in pool.released] == ["instance-3", "instance-2"]
    else:
        # It came into service, and the next run released it: it was the idle one.
        assert pool.taken == []
        assert [name for _, name in pool.released] == ["instance-3", "instance-2", "instance-4"]


def test_a_session_that_ends_with_requests_running() -> None:
    ends_at = T0 + 10 * MINUTE
    pool = sim.Sim(POOL, start=T0, in_service=2, session=session(2, ends_at=ends_at))
    pool.advance(9 * MINUTE)
    # One request that ends two minutes after the session, and one that would run half an hour.
    short, long = pool.request(3 * MINUTE, "instance-1"), pool.request(30 * MINUTE, "instance-2")
    pool.advance(MINUTE)

    last = pool.runs[-1]
    assert last.now == ends_at
    assert (last.decision.requested, last.decision.desired, last.decision.reason) == (0, 0, Reason.RELEASE)
    assert last.decision.lift_protection == ("instance-1", "instance-2")
    # Both instances leave service at once, busy as they are, and drain.
    assert pool.in_service() == [] and [name for _, name in pool.released] == ["instance-1", "instance-2"]

    pool.advance(2 * MINUTE)
    assert short.outcome == "served" and list(pool.instances) == ["instance-2"]
    # The pool is empty within the drain, which cuts the request that outlasts it.
    pool.advance(ends_at + POOL.drain_seconds - pool.now)
    assert pool.instances == {} and long.outcome == "cut"
    pool.advance(HOUR)
    assert pool.launches == [] and pool.desired == 0


def test_a_launch_that_never_becomes_healthy() -> None:
    pool = sim.Sim(POOL, start=T0, session=session(1), healthy=False)
    pool.advance(HOUR)

    abandons = [at for at, outcome in pool.history if outcome == "abandoned"]
    suspending = [run for run in pool.runs if run.decision.suspend_launches]
    assert suspending, "launches never suspended"
    suspended_at = suspending[0].now
    # Suspended by the first run after the third abandoned launch, and not before it.
    assert abandons[2] <= suspended_at < abandons[2] + MINUTE
    assert len([at for at in abandons if at <= suspended_at]) == 3
    assert suspending[0].decision.memory.launch_suspended

    # The session is raised: a suspended pool is not.
    refused = pool.write_session(replace(pool.session, instances=3))
    assert (refused.requested, refused.desired, refused.reason) == (3, 1, Reason.LAUNCH_SUSPENDED)
    pool.advance(HOUR)
    # Nothing is launched once suspended, and the suspension stands to the end.
    assert [launch for launch in pool.launches if launch.now > suspended_at] == []
    assert all(run.decision.suspend_launches for run in pool.runs if run.now >= suspended_at)
    assert pool.launches_suspended and pool.memory.launch_suspended
    assert pool.desired == 1 and pool.instances == {}


def test_a_record_that_asks_for_more_than_the_pool_allows() -> None:
    # A record written by hand: nine instances, for a pool that allows four.
    pool = sim.Sim(POOL, start=T0, session=session(9))
    pool.advance(30 * MINUTE)

    assert POOL.max_instances == 4
    assert {(run.decision.requested, run.decision.desired) for run in pool.runs} == {(4, 4)}
    assert pool.runs[0].decision.reason is Reason.PROVISION
    assert len(pool.launches) == len(pool.in_service()) == 4
