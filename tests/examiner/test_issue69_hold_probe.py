"""Independent clock-driven checks of issue 69's hold contract."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import Mode, ScalerNote
from rhi_gateway.errors import WireFormat
from rhi_gateway.hold import Action, End, Forward, Hold, KeepAlive, Wake

START = 2_000_000


def configured_pool() -> Pool:
    return parse_pools({"probe": {
        "engine": "mock", "instance_types": ["t4g.small"], "max_instances": 1,
        "prices": {"t4g.small": 0.0168},
        "warm_instances": 0, "wake": "on-demand", "target_concurrency": 1,
        "context_length": 1024, "largest_output": 128,
    }})[0]


def trace(
    machine: Hold,
    *,
    ready_at: int = 1000,
    first_at: int = 1000,
    mode: Mode = Mode.ENABLED,
    note: ScalerNote | None = None,
) -> list[tuple[int, Action]]:
    result: list[tuple[int, Action]] = []
    for elapsed in range(651):
        if elapsed == first_at:
            machine.first_event()
        actions = machine.step(START + elapsed, mode=mode, note=note, ready=elapsed >= ready_at)
        result.extend((elapsed, action) for action in actions)
    return result


def at(events: list[tuple[int, Action]], kind: type) -> list[int]:
    return [elapsed for elapsed, action in events if isinstance(action, kind)]


@pytest.mark.parametrize("streams", [True, False])
def test_defaults_hold_silently_or_ping_and_repeat_wakes(streams: bool) -> None:
    machine = Hold(configured_pool(), WireFormat.MESSAGES, streams=streams, started_at=START)
    events = trace(machine)
    deadline = 420 if streams else 240
    assert at(events, End) == [deadline]
    assert events[-1] == (deadline, End(Cause.WARMING))
    assert at(events, Forward) == []
    assert at(events, Wake) == list(range(0, deadline, 15))
    assert at(events, KeepAlive) == (list(range(10, deadline, 10)) if streams else [])
    assert machine.next_at is None


@pytest.mark.parametrize("wire", list(WireFormat))
def test_wire_bytes_and_keepalives_across_forwarding(wire: WireFormat) -> None:
    machine = Hold(configured_pool(), wire, streams=True, started_at=START)
    events = trace(machine, ready_at=200, first_at=260)
    assert at(events, Forward) == [200]
    assert at(events, End) == []
    assert at(events, Wake) == list(range(0, 200, 15))
    assert at(events, KeepAlive) == list(range(10, 260, 10))
    assert [t for t in at(events, KeepAlive) if t > 200] == [210, 220, 230, 240, 250]
    for _, action in events:
        if isinstance(action, KeepAlive):
            assert action.data.endswith(b"\n\n")
            lines = action.data.splitlines()
            if wire is WireFormat.MESSAGES:
                assert b"event: ping" in lines
                payloads = [json.loads(line[5:]) for line in lines if line.startswith(b"data:")]
                assert payloads == [{"type": "ping"}]
            else:
                assert all(line.startswith(b":") for line in lines if line)
    assert machine.next_at is None


def test_forwarded_stream_has_no_hold_deadline_while_engine_is_silent() -> None:
    machine = Hold(configured_pool(), WireFormat.MESSAGES, streams=True, started_at=START)
    events = trace(machine, ready_at=200, first_at=600)
    assert at(events, End) == []
    assert at(events, Forward) == [200]
    assert at(events, KeepAlive) == list(range(10, 600, 10))


@pytest.mark.parametrize("streams", [True, False])
def test_pool_deadlines_and_keepalive_parameter(streams: bool) -> None:
    pool = replace(configured_pool(), hold_seconds=53, hold_seconds_non_streaming=23)
    machine = Hold(pool, WireFormat.RESPONSES, streams=streams, started_at=START, keepalive_seconds=7)
    events = trace(machine)
    deadline = 53 if streams else 23
    assert events[-1] == (deadline, End(Cause.WARMING))
    assert at(events, KeepAlive) == (list(range(7, deadline, 7)) if streams else [])
    assert at(events, Wake) == list(range(0, deadline, 15))


@pytest.mark.parametrize("streams", [True, False])
@pytest.mark.parametrize(("mode", "cause"), [
    (Mode.DISABLED, Cause.POOL_DISABLED),
    (Mode.BUDGET_LATCHED, Cause.POOL_CEILING_REACHED),
    (Mode.SCHEDULED_ONLY, Cause.POOL_SCHEDULED_ONLY),
])
def test_stopped_requests_return_only_the_immediate_cause(mode: Mode, cause: Cause, streams: bool) -> None:
    for note in (None, ScalerNote(pool="probe", capacity_asked=0, no_capacity_at=None, written_at=START)):
        machine = Hold(configured_pool(), WireFormat.MESSAGES, streams=streams, started_at=START)
        assert trace(machine, mode=mode, note=note) == [(0, End(cause))]


def test_scheduled_start_holds_then_forwards_without_any_wake() -> None:
    note = ScalerNote(pool="probe", capacity_asked=1, no_capacity_at=None, written_at=START)
    machine = Hold(configured_pool(), WireFormat.MESSAGES, streams=True, started_at=START)
    events = trace(machine, mode=Mode.SCHEDULED_ONLY, note=note, ready_at=200, first_at=260)
    assert at(events, Wake) == []
    assert at(events, Forward) == [200]
    assert at(events, End) == []
    assert at(events, KeepAlive) == list(range(10, 260, 10))


@pytest.mark.parametrize(("age", "cause"), [(600, Cause.CAPACITY_UNAVAILABLE), (601, Cause.WARMING)])
def test_scaler_capacity_note_controls_timeout_cause(age: int, cause: Cause) -> None:
    machine = Hold(configured_pool(), WireFormat.MESSAGES, streams=True, started_at=START)
    note = ScalerNote(pool="probe", capacity_asked=1, no_capacity_at=START + 420 - age, written_at=START)
    events = trace(machine, note=note)
    assert events[-1] == (420, End(cause))
    assert at(events, Wake) == list(range(0, 420, 15))
