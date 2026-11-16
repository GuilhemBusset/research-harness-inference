"""The scaler's scenarios of section 5.1, played over time against a simulated pool (`sim.py`)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from rhi_common.pool_config import parse_pools
from rhi_scaler.model import Reason

# The simulator sits beside this file; the tests tree has no packages, so it is loaded by path.
_spec = importlib.util.spec_from_file_location("scaler_sim", Path(__file__).with_name("sim.py"))
assert _spec is not None and _spec.loader is not None
sim = importlib.util.module_from_spec(_spec)
sys.modules["scaler_sim"] = sim
_spec.loader.exec_module(sim)

T0 = 1_800_000_000  # a whole minute
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


def test_two_first_requests_at_once() -> None:
    pool = sim.Sim(POOL, start=T0 + 30, gateways=("task-1", "task-2"))
    first, second = pool.request("task-1", 60), pool.request("task-2", 60)
    # Each gateway task held its request and called the scaler, in the same second.
    assert [run.caller for run in pool.runs] == ["task-1", "task-2"]
    pool.advance(10 * MINUTE)

    assert len(pool.launches) == 1
    assert max(run.decision.desired for run in pool.runs) == 1
    assert (first.outcome, second.outcome) == ("served", "served")
    assert first.instance == second.instance == pool.launches[0].instance


def test_a_request_during_scale_in() -> None:
    pool = sim.Sim(POOL, start=T0, in_service=2, warm=0)
    long = pool.request("task-1", 60 * MINUTE)
    busy, idle = long.instance, ({"instance-1", "instance-2"} - {long.instance}).pop()
    while not pool.released:
        assert pool.now < T0 + 20 * MINUTE, "no scale-in"
        pool.advance(1)
    assert pool.runs[-1].decision.reason is Reason.SCALE_IN
    assert pool.released == [(pool.now, idle)]
    assert pool.instances[idle].state == "draining"

    # A request arrives while the released instance drains.
    late = pool.request("task-1", 2 * MINUTE)
    assert late.instance == busy
    pool.advance(5 * MINUTE)
    assert late.outcome == "served"
    assert pool.in_service() == [busy]
    assert idle in pool.warm()
    assert [name for _, name in pool.released] == [idle]


def test_a_wake_after_a_long_idle() -> None:
    pool = sim.Sim(POOL, start=T0, in_service=1, warm=1)
    pool.request("task-1", MINUTE)
    pool.advance(30 * MINUTE)
    assert pool.desired == 0
    assert any(run.decision.reason is Reason.IDLE for run in pool.runs)
    pool.advance(6 * 60 * MINUTE)
    assert pool.in_service() == pool.starting() == []

    launched, ran = len(pool.launches), len(pool.runs)
    request = pool.request("task-1", MINUTE)
    pool.advance(15 * MINUTE)
    woken = pool.runs[ran:]
    assert woken[0].decision.reason is Reason.WAKE
    # Many runs while it started (the wake repeats, the schedule), one instance, from the warm pool.
    assert len([run for run in woken if run.now <= pool.launches[launched].now + pool.warm_start_seconds]) > 3
    assert len(pool.launches) - launched == 1
    assert pool.launches[launched].from_warm
    assert max(run.decision.desired for run in woken) == 1
    assert request.outcome == "served"


def test_a_launch_that_never_becomes_healthy() -> None:
    pool = sim.Sim(POOL, start=T0, healthy=False)
    # A student retries every 5 minutes for two hours; every request is held, then fails.
    for _ in range(24):
        pool.request("task-1", MINUTE)
        pool.advance(5 * MINUTE)

    abandons = [at for at, outcome in pool.history if outcome == "abandoned"]
    suspending = [run for run in pool.runs if run.decision.suspend_launches]
    assert suspending, "launches never suspended"
    suspended_at = suspending[0].now
    # Suspended by the first run after the third abandoned launch, and not before it.
    assert abandons[2] <= suspended_at < abandons[2] + MINUTE
    assert len([at for at in abandons if at <= suspended_at]) == 3
    assert suspending[0].decision.memory.launch_suspended
    # Nothing is launched once suspended, and the suspension stands to the end.
    assert [launch for launch in pool.launches if launch.now > suspended_at] == []
    assert all(run.decision.suspend_launches for run in pool.runs if run.now >= suspended_at)
    assert pool.launches_suspended and pool.memory.launch_suspended
    assert pool.in_service() == pool.starting() == []
    assert "served" not in {request.outcome for request in pool.requests}


def test_a_gateway_that_stops_reporting() -> None:
    pool = sim.Sim(POOL, start=T0, in_service=1, warm=0)
    request = pool.request("task-1", 60 * MINUTE)
    pool.advance(2 * MINUTE)
    pool.stop_gateway("task-1")
    last = pool.gateways["task-1"].row.written_at

    pool.advance(last + 10 * MINUTE - 1 - pool.now)
    assert pool.desired == 1
    assert not any(run.decision.reason is Reason.GATEWAY_SILENT for run in pool.runs)

    pool.advance(MINUTE + 1)
    silent = [run for run in pool.runs if run.decision.reason is Reason.GATEWAY_SILENT]
    assert silent and silent[0].now == last + 10 * MINUTE
    # Zero, even though the instance still reports the request it runs.
    assert pool.instance_rows[request.instance].running == 1
    assert pool.desired == 0
