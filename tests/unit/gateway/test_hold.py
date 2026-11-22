"""The hold, on a clock the test controls (`rhi_gateway.hold`; system design, sections 5.1 and 5.3).

`run` is the clock: it steps a hold once a second from its start and records what the machine said
to do, with the second it said it at. Nothing sleeps and nothing reads the time.
"""

from __future__ import annotations

import json

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import Mode, ScalerNote
from rhi_gateway.errors import WireFormat
from rhi_gateway.hold import Action, End, Forward, Hold, KeepAlive, Wake
from rhi_gateway.sse import SseReader

# When the request arrives, on the clock of the scaler's note. Any time would do.
START = 1_700_000_000
FORMATS = (WireFormat.MESSAGES, WireFormat.RESPONSES, WireFormat.CHAT_COMPLETIONS)


def pool(**fields: int) -> Pool:
    """A pool whose hold fields are left out, so that they take the defaults of section 17."""
    entry = {
        "engine": "mock",
        "instance_types": ["t4g.small"],
        "prices": {"t4g.small": 0.0168},
        "max_instances": 1,
        "warm_instances": 0,
        "wake": "on-demand",
        "target_concurrency": 64,
        "context_length": 8192,
        "largest_output": 1024,
    }
    (parsed,) = parse_pools({"mock": entry | fields})
    return parsed


def note(capacity_asked: int = 0, no_capacity_at: int | None = None) -> ScalerNote:
    return ScalerNote(pool="mock", capacity_asked=capacity_asked, no_capacity_at=no_capacity_at, written_at=START)


def hold(*, streams: bool = True, wire_format: WireFormat = WireFormat.MESSAGES) -> Hold:
    return Hold(pool(), wire_format, streams=streams, started_at=START)


def run(
    machine: Hold,
    *,
    until: int,
    mode: Mode = Mode.ENABLED,
    scaler_note: ScalerNote | None = None,
    ready_at: int | None = None,
    first_event_at: int | None = None,
) -> list[tuple[int, Action]]:
    """Step the hold at every second up to `until`: each action, with the seconds since the start."""
    record: list[tuple[int, Action]] = []
    for second in range(until + 1):
        if second == first_event_at:
            machine.first_event()
        ready = ready_at is not None and second >= ready_at
        actions = machine.step(START + second, mode=mode, note=scaler_note, ready=ready)
        record += [(second, action) for action in actions]
    return record


def times(record: list[tuple[int, Action]], kind: type) -> list[int]:
    return [second for second, action in record if isinstance(action, kind)]


def ends(record: list[tuple[int, Action]]) -> list[tuple[int, str]]:
    return [(second, action.cause.value) for second, action in record if isinstance(action, End)]


def test_a_held_messages_stream_gets_a_ping_every_10_seconds_and_ends_at_420() -> None:
    record = run(hold(), until=600)

    assert times(record, KeepAlive) == list(range(10, 420, 10))
    for _, action in record:
        if isinstance(action, KeepAlive):
            (event,) = SseReader().feed(action.data)
            assert (event.name, event.comment, json.loads(event.data)) == ("ping", False, {"type": "ping"})
    assert ends(record) == [(420, "warming")]
    assert times(record, Forward) == []
    assert record[-1][0] == 420, "nothing follows the end"


@pytest.mark.parametrize("wire_format", FORMATS)
def test_a_held_non_streaming_call_ends_silently_at_240(wire_format: WireFormat) -> None:
    record = run(hold(streams=False, wire_format=wire_format), until=600)

    assert times(record, KeepAlive) == []
    assert ends(record) == [(240, "warming")]
    assert record[-1][0] == 240


@pytest.mark.parametrize("streams", [True, False])
def test_a_pool_ready_at_200_seconds_has_its_request_forwarded_then(streams: bool) -> None:
    record = run(hold(streams=streams), until=600, ready_at=200, first_event_at=201)

    assert times(record, Forward) == [200]
    assert ends(record) == []
    assert max(times(record, Wake)) == 195, "no wake once the pool is ready"


def test_a_ready_pool_is_not_held() -> None:
    assert run(hold(), until=5, ready_at=0, first_event_at=1) == [(0, Forward())]


def test_keep_alives_continue_until_the_first_event() -> None:
    record = run(hold(), until=600, ready_at=200, first_event_at=260)

    assert times(record, Forward) == [200]
    # The cadence is the same before and after the request is forwarded, and stops at the first event.
    assert times(record, KeepAlive) == list(range(10, 260, 10))
    assert [second for second in times(record, KeepAlive) if second > 200] == [210, 220, 230, 240, 250]
    assert ends(record) == [], "no deadline at 420"
    assert record[-1][0] == 250


@pytest.mark.parametrize("wire_format", [WireFormat.RESPONSES, WireFormat.CHAT_COMPLETIONS])
def test_the_openai_formats_are_kept_alive_with_sse_comments(wire_format: WireFormat) -> None:
    record = run(hold(wire_format=wire_format), until=420)

    assert times(record, KeepAlive) == list(range(10, 420, 10))
    for _, action in record:
        if isinstance(action, KeepAlive):
            events = SseReader().feed(action.data)
            assert [event.comment for event in events] == [True]
            assert action.data.endswith(b"\n\n")


def test_the_interval_and_the_deadlines_are_the_ones_given() -> None:
    custom = pool(hold_seconds=50, hold_seconds_non_streaming=20)
    stream = Hold(custom, WireFormat.MESSAGES, streams=True, started_at=START, keepalive_seconds=4)
    record = run(stream, until=100)
    assert times(record, KeepAlive) == list(range(4, 50, 4))
    assert ends(record) == [(50, "warming")]

    call = Hold(custom, WireFormat.MESSAGES, streams=False, started_at=START)
    assert ends(run(call, until=100)) == [(20, "warming")]


def test_wakes_repeat_every_15_seconds() -> None:
    stream = run(hold(), until=600)
    assert times(stream, Wake) == list(range(0, 420, 15))
    assert len(times(stream, Wake)) == 28

    call = run(hold(streams=False), until=600)
    assert times(call, Wake) == list(range(0, 240, 15))
    assert len(times(call, Wake)) == 16


def test_no_capacity_ends_the_hold_with_capacity_unavailable() -> None:
    # A launch found no capacity during the hold: the wakes go on, since the scaler tries again.
    record = run(hold(), until=600, scaler_note=note(capacity_asked=1, no_capacity_at=START + 30))
    assert ends(record) == [(420, "capacity_unavailable")]
    assert len(times(record, Wake)) == 28

    # One that found none more than 10 minutes before the deadline says nothing of this start.
    stale = note(capacity_asked=1, no_capacity_at=START + 420 - 601)
    assert ends(run(hold(), until=600, scaler_note=stale)) == [(420, "warming")]


@pytest.mark.parametrize(
    ("mode", "scaler_note", "cause"),
    [
        (Mode.DISABLED, note(capacity_asked=1), "pool_disabled"),
        (Mode.SCHEDULED_ONLY, note(capacity_asked=0), "pool_scheduled_only"),
        (Mode.SCHEDULED_ONLY, None, "pool_scheduled_only"),
    ],
)
@pytest.mark.parametrize("streams", [True, False])
def test_stopped_pools_are_not_held(mode: Mode, scaler_note: ScalerNote | None, cause: str, streams: bool) -> None:
    machine = hold(streams=streams)

    first = machine.step(START, mode=mode, note=scaler_note, ready=False)
    assert first == (End(Cause(cause)),), "the end and nothing else: no wake, no keep-alive"
    assert run(machine, until=600, mode=mode, scaler_note=scaler_note) == []
    assert machine.next_at is None


def test_only_a_disabled_pool_ends_whatever_is_in_service() -> None:
    # The first rule: a ready pool is forwarded in every mode but `disabled`.
    for mode in Mode:
        first = hold().step(START, mode=mode, note=note(capacity_asked=1), ready=True)
        assert (first == (End(Cause.POOL_DISABLED),)) == (mode is Mode.DISABLED), mode


def test_a_scheduled_pool_is_never_woken() -> None:
    asked_by_its_schedule = note(capacity_asked=1)
    record = run(
        hold(), until=600, mode=Mode.SCHEDULED_ONLY, scaler_note=asked_by_its_schedule, ready_at=130, first_event_at=131
    )

    assert times(record, Wake) == []
    assert times(record, KeepAlive) == list(range(10, 140, 10)), "it is held like any other"
    assert times(record, Forward) == [130]
    assert ends(record) == []


def test_next_at_names_every_time_something_is_due() -> None:
    """A caller that sleeps until `next_at` does what one that steps every second does."""
    machine = hold()
    record: list[tuple[int, Action]] = []
    while (due := machine.next_at) is not None:
        ready = due - START >= 203
        if due - START >= 260:
            machine.first_event()
        actions = machine.step(due, mode=Mode.ENABLED, note=None, ready=ready)
        record += [(int(due - START), action) for action in actions]

    # The pool is ready from 203 seconds. Only a step tells this caller, and its next one is at 210.
    every_second = run(hold(), until=600, ready_at=210, first_event_at=260)
    assert record == every_second
    assert times(record, Forward) == [210]
