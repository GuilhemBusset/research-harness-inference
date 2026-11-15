"""Independent checks of the issue 59 simulated scaler scenarios."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from rhi_common.pool_config import parse_pools
from rhi_scaler.model import Reason


def load_file(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCALER_TESTS = Path(__file__).resolve().parents[1] / "unit" / "scaler"
SIM = load_file("issue59_probe_sim", SCALER_TESTS / "sim.py")
START = 1_800_000_000
POOL = parse_pools(
    {
        "probe": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "max_instances": 3,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 4,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]


def test_named_scenarios_execute() -> None:
    scenarios = load_file("issue59_probe_scenarios", SCALER_TESTS / "test_scenarios.py")
    for name in (
        "test_two_first_requests_at_once",
        "test_a_request_during_scale_in",
        "test_a_wake_after_a_long_idle",
        "test_a_launch_that_never_becomes_healthy",
        "test_a_gateway_that_stops_reporting",
    ):
        getattr(scenarios, name)()


def test_delayed_start_heartbeats_and_return_to_warm_pool() -> None:
    pool = SIM.Sim(POOL, start=START, warm_start_seconds=45)
    request = pool.request("task-1", 20)
    assert len(pool.starting()) == 1 and pool.in_service() == []
    pool.advance(44)
    assert request.instance is None
    pool.advance(1)
    assert request.instance in pool.in_service()
    pool.advance(5)
    assert pool.instance_rows[request.instance].running == 1
    assert pool.gateways["task-1"].row.in_flight == 1
    pool.advance(20)
    assert request.outcome == "served"
    assert pool.instance_rows[request.instance].running == 0
    pool.advance((POOL.idle_minutes + 2) * 60)
    assert pool.desired == 0
    assert pool.in_service() == pool.starting() == []
    assert request.instance in pool.warm()
    assert any(run.decision.reason is Reason.IDLE for run in pool.runs)


def test_two_gateways_share_one_start() -> None:
    pool = SIM.Sim(POOL, start=START + 17, gateways=("one", "two"), warm_start_seconds=80)
    requests = [pool.request(task, 30) for task in ("one", "two")]
    pool.advance(240)
    assert len(pool.launches) == 1
    assert all(run.decision.desired <= 1 for run in pool.runs)
    assert all(request.outcome == "served" for request in requests)
    assert len({request.instance for request in requests}) == 1


def test_scale_in_serves_arrival_on_retained_instance() -> None:
    pool = SIM.Sim(POOL, start=START, warm=0, in_service=2)
    busy = pool.request("task-1", 2400)
    pool.advance(11 * 60)
    assert len(pool.released) == 1
    released = pool.released[0][1]
    assert released != busy.instance
    arrival = pool.request("task-1", 90)
    pool.advance(120)
    assert arrival.outcome == "served"
    assert arrival.instance == busy.instance
    assert arrival.instance in pool.in_service()
    assert released in pool.warm()


def test_wake_after_hours_idle_starts_one_warm_instance() -> None:
    pool = SIM.Sim(POOL, start=START, in_service=1)
    pool.request("task-1", 30)
    pool.advance(4 * 60 * 60)
    assert pool.desired == 0 and pool.in_service() == []
    before = len(pool.launches)
    request = pool.request("task-1", 30)
    pool.advance(300)
    assert len(pool.launches) == before + 1
    assert pool.launches[-1].from_warm
    assert request.outcome == "served"


def test_launch_failure_stops_after_three_attempts() -> None:
    pool = SIM.Sim(POOL, start=START, healthy=False)
    for _ in range(24):
        pool.request("task-1", 30)
        pool.advance(300)
    assert pool.launches_suspended and pool.memory.launch_suspended
    assert len(pool.launches) == 3, "the failed-launch scenario must stop after three attempts"
    assert len([entry for entry in pool.history if entry[1] == "abandoned"]) == 3
    assert pool.starting() == pool.in_service() == []


def test_silent_gateway_reaches_zero_at_ten_minutes() -> None:
    pool = SIM.Sim(POOL, start=START, in_service=1, warm=0)
    request = pool.request("task-1", 2400)
    pool.advance(60)
    pool.stop_gateway("task-1")
    last = pool.gateways["task-1"].row.written_at
    pool.advance(599)
    assert pool.desired == 1
    pool.advance(1)
    assert pool.now == last + 600
    assert pool.desired == 0
    assert request.instance not in pool.in_service()
    assert any(run.decision.reason is Reason.GATEWAY_SILENT for run in pool.runs)
