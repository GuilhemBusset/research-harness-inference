"""Spike S7's job (`spikes/s7_drain.py`): each step writes its lines, on stubbed clients and a clock
the test controls. The pool-state table and the workspace are stubs that answer as a scripted
scaler and the instances' collectors would; the gateway is a stub transport. No AWS, no network.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import tomllib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import botocore.exceptions
import httpx
import pytest

import common
import s7_drain as s7
from rhi import config
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_common.records import ScalerNote
from rhi_gateway.errors import WireFormat, render_stream_event

REPO = Path(__file__).resolve().parents[3]
MOCK = next(
    pool
    for pool in config.parse(tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8")), "test").pools
    if pool.name == "mock"
)
T0 = 1_800_000_000.0
UTC_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
# What an AWS error's text might hold: it must reach no line.
AWS_TEXT = "an AWS message naming arn:aws:dynamodb:us-east-1:123456789012:table/pool-state"


class FakeClock:
    """A clock the test moves: `sleep` advances it at once."""

    def __init__(self, now: float = T0) -> None:
        self.at = now

    def now(self) -> float:
        return self.at

    async def sleep(self, seconds: float) -> None:
        self.at += seconds
        await asyncio.sleep(0)


def instance_id() -> str:
    """An instance ID of the shape AWS gives, made at run time."""
    return "i-" + secrets.token_hex(9)[:17]


@dataclass
class Scaler:
    """A scaler that runs every minute: its note asks for `asked(run)`, and its gauges, stamped a
    moment after the note's `written_at`, read `in_service(run)` and `warm(run)`. Each instance's
    collector writes `up` every 15 seconds from its first write to its last. Nothing is visible
    before the clock reaches it."""

    clock: FakeClock
    asked: Callable[[float], int]
    in_service: Callable[[float], int]
    warm: Callable[[float], int] = lambda run: 0
    instances: dict[str, tuple[float, float]] = field(default_factory=dict)
    failing: bool = False

    def runs(self, until: float) -> list[float]:
        return [T0 - 1200 + 60 * n for n in range(int((until - T0 + 1200) // 60) + 1)]

    # The pool-state table.
    def get_item(self, **arguments: Any) -> dict[str, Any]:
        if self.failing:
            error = {"Error": {"Code": "ThrottlingException", "Message": AWS_TEXT}}
            raise botocore.exceptions.ClientError(error, "GetItem")
        run = self.runs(self.clock.now())[-1]
        note = ScalerNote(pool="mock", capacity_asked=self.asked(run), no_capacity_at=None, written_at=int(run))
        return {"Item": note.to_item()}

    # The workspace.
    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        steps, now = [], self.clock.now()
        moment = start
        while moment <= end:
            steps.append(min(moment, now))
            moment += step
        if query.startswith("max by (instance)"):
            series = []
            for instance, (first, last) in self.instances.items():
                samples = []
                for at in steps:
                    written = [first + 15 * n for n in range(int((min(last, at) - first) // 15) + 1)]
                    if min(last, at) >= first and at - written[-1] <= 300:
                        samples.append((at, written[-1]))
                series.append(common.Series({"instance": instance}, tuple(samples)))
            return series
        gauge = self.in_service if s7.INSTANCES_IN_SERVICE in query else self.warm
        samples = []
        for at in steps:
            stamped = [run + 0.2 for run in self.runs(at) if run + 0.2 <= at]
            if stamped:
                value = stamped[-1] if query.startswith("timestamp(") else gauge(stamped[-1] - 0.2)
                samples.append((at, float(value)))
        return [common.Series({"pool": "mock"}, tuple(samples))]


def sse(name: str, data: dict[str, Any] | None = None) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data or {'type': name})}\n\n".encode()


TURN = [
    sse("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 50}}}),
    sse("content_block_start"),
    sse("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}),
    sse("content_block_stop"),
    sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 2}}),
    sse("message_stop"),
]
PINGS = [sse("ping"), sse("ping")]
WARMING = [render_stream_event(Cause.WARMING, WireFormat.MESSAGES)]


@dataclass(frozen=True)
class Reply:
    """What the gateway answers one request of the canary student with: a status, the events, and
    whether the connection is cut after them."""

    events: list[bytes] = field(default_factory=lambda: list(TURN))
    status: int = 200
    cut: bool = False


class Events(httpx.AsyncByteStream):
    def __init__(self, reply: Reply) -> None:
        self.reply = reply

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.reply.events:
            yield event
        if self.reply.cut:
            raise httpx.RemoteProtocolError("the stub cut the connection")


@dataclass
class Gateway:
    """The gateway: the canary student's requests get the replies given, in order, then a whole
    turn at once; so do the load's. It reads a body whole before it answers, as the gateway does,
    and keeps the student's bodies and when they arrived whole."""

    clock: FakeClock
    student: str
    replies: list[Reply] = field(default_factory=list)
    received: list[tuple[float, dict[str, Any]]] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        reply = Reply()
        if request.headers["authorization"] == f"Bearer {self.student}":
            self.received.append((self.clock.now(), body))
            reply = self.replies.pop(0) if self.replies else Reply()
        else:
            # The load sends one stream after another: it lets the job run between two.
            await asyncio.sleep(0.01)
        return httpx.Response(reply.status, headers={"content-type": "text/event-stream"}, stream=Events(reply))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def a_run(scaler: Scaler, replies: list[Reply] | None = None, *, wakes: int = 1) -> tuple[s7.Run, Gateway, list[str]]:
    keys = [generate_key() for _ in range(7)]
    gateway = Gateway(scaler.clock, keys[0], replies or [])
    out: list[str] = []
    pool = s7.Pool("mock", scaler, scaler)
    run = s7.Run(
        pool,
        "http://gateway.example.com",
        keys,
        MOCK,
        wakes,
        60.0,
        s7.Recorder(scaler.clock, out.append),
        scaler.clock,
        gateway.transport(),
        inline,
    )
    return run, gateway, out


async def inline(action: Callable[[], Any]) -> Any:
    """Reads the stubs on the event loop: the clock moves only when the job waits on it."""
    return action()


def parsed(out: list[str], *hidden: str) -> list[dict[str, Any]]:
    """Every line as one JSON object with a UTC time and a kind, none holding any of `hidden`."""
    lines = []
    for line in out:
        record = json.loads(line)
        assert isinstance(record, dict)
        assert UTC_TIME.fullmatch(record["at"])
        assert isinstance(record["kind"], str)
        for text in hidden:
            assert text not in line
        lines.append(record)
    return lines


def of(lines: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [line for line in lines if line["kind"] == kind]


def test_the_scale_in_step_records_both_paths_and_one_stream_in_flight_as_the_instance_leaves() -> None:
    clock = FakeClock()
    first, second = T0 + 120, T0 + 900  # The scaler asks for one, then for none.
    leaving, staying = instance_id(), instance_id()
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else 1 if run < second else 0,
        # A run reads the pool as it was before it acted.
        in_service=lambda run: 2 if run <= first else 1 if run <= second else 0,
        # The first to leave goes back to the warm pool; the second is terminated.
        warm=lambda run: 0 if run <= first else 1,
        instances={leaving: (T0 - 600, first + 30), staying: (T0 - 600, second + 30)},
    )
    run, gateway, out = a_run(scaler)
    asyncio.run(run.cycle())

    lines = parsed(out, leaving, staying)
    assert [line["writing"] for line in of(lines, "instances")] == [["instance-1", "instance-2"]]
    asked, out_of_service, gone = of(lines, "scale_in_asked"), of(lines, "out_of_service"), of(lines, "gone")
    assert [(line["decision"], line["capacity_asked"]) for line in asked] == [(1, 1), (2, 0)]
    assert [(line["decision"], line["in_service"]) for line in out_of_service] == [(1, 1), (2, 0)]
    assert [line["decision"] for line in gone] == [1, 2]
    names = [line["instance"] for line in gone]
    assert sorted(names) == ["instance-1", "instance-2"]
    departures = of(lines, "scale_in")
    assert [(line["path"], line["instance"]) for line in departures] == [("warm_pool", names[0]), ("terminated", names[1])]
    for line in departures:
        assert line["asked_at"] <= line["asked_read_at"] <= line["out_of_service_read_at"] <= line["gone_read_at"]

    # One stream, held open at the gateway across the whole scale-in, from before the scaler asked
    # for fewer until the job read the departing instance gone, then sent whole and answered.
    (stream,) = of(lines, "stream")
    assert (stream["stream"], stream["decision"], stream["ended"], stream["events"]) == ("scale_in", 1, "closing_event", 6)
    assert stream["sent_at"] < asked[0]["written_at"] < out_of_service[0]["read_at"]
    assert out_of_service[0]["read_at"] <= gone[0]["read_at"] <= stream["body_sent_at"] < asked[1]["written_at"]
    assert stream["body_sent_at"] <= stream["first_event_at"] <= stream["last_event_at"]
    ((received, body),) = gateway.received
    assert received == datetime.fromisoformat(stream["body_sent_at"]).timestamp()
    assert body["model"] == "mock"


def test_two_instances_leaving_on_one_idle_note_are_each_recorded() -> None:
    clock = FakeClock()
    ask = T0 + 120  # From two to none at once, the collectors stopping five minutes apart.
    one, two = instance_id(), instance_id()
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < ask else 0,
        in_service=lambda run: 2 if run <= ask else int(run <= ask + 300),
        warm=lambda run: 0 if run <= ask else 1 if run <= ask + 300 else 2,
        instances={one: (T0 - 600, ask + 30), two: (T0 - 600, ask + 330)},
    )
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())

    lines = parsed(out, one, two)
    assert [line["fewer"] for line in of(lines, "scale_in_asked")] == [2]
    assert [line["in_service"] for line in of(lines, "out_of_service")] == [1, 0]
    gone = of(lines, "gone")
    assert [line["decision"] for line in gone] == [1, 1]
    assert sorted(line["instance"] for line in gone) == ["instance-1", "instance-2"]
    # Both stopped into the warm pool: the count rose by two.
    told = of(lines, "scale_in")
    assert [line["path"] for line in told] == ["warm_pool", "warm_pool"]
    # With no instance left to serve it, the stream across the scale-in waited for the paths.
    (stream,) = of(lines, "stream")
    assert stream["body_sent_at"] >= max(line["at"] for line in told)


@pytest.mark.parametrize("gap", ["collectors", "in_service", "warm"])
def test_a_gap_in_the_workspace_loses_no_observation_it_can_still_read(gap: str) -> None:
    clock = FakeClock()
    first, second = T0 + 120, T0 + 900
    leaving, staying = instance_id(), instance_id()
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else 1 if run < second else 0,
        in_service=lambda run: 2 if run <= first else 1 if run <= second else 0,
        warm=lambda run: 0 if run <= first else 1,
        instances={leaving: (T0 - 600, first + 30), staying: (T0 - 600, second + 30)},
    )
    read = scaler.read_series
    # The workspace answers nothing for a while: the collectors as the scaler first asks for fewer,
    # the in-service gauge after the last scale-in, or the warm gauge until the end.
    missing = {
        "collectors": ("max by (instance)", first, first + 30),
        "in_service": (s7.INSTANCES_IN_SERVICE, second + 1, second + 170),
        "warm": (s7.WARM_INSTANCES, T0, second + 420),
    }[gap]

    def gapped(query: str, **window: float) -> list[common.Series]:
        if missing[0] in query and missing[1] <= clock.now() < missing[2]:
            return []
        return read(query, **window)

    scaler.read_series = gapped  # ty: ignore[invalid-assignment] - a stub's method replaced by the test
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())

    lines = parsed(out, leaving, staying)
    assert [line["last_write_at"] for line in of(lines, "gone")] == [s7.utc(first + 30), s7.utc(second + 30)]
    assert [line["in_service"] for line in of(lines, "out_of_service")] == [1, 0]
    assert [line["path"] for line in of(lines, "scale_in")] == ["warm_pool", "terminated"]


def test_a_return_to_the_warm_pool_counted_late_is_still_its_departures() -> None:
    clock = FakeClock()
    first, second = T0 + 120, T0 + 1200
    early, late = instance_id(), instance_id()
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        # The warm count rises six minutes after the last decision, more than five past the stop.
        warm=lambda run: int(run >= second + 360),
        instances={early: (T0 - 600, first + 30), late: (T0 - 600, second + 45)},
    )
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())
    assert [line["path"] for line in of(parsed(out, early, late), "scale_in")] == ["terminated", "warm_pool"]


def test_the_stream_across_a_scale_in_is_held_until_the_instance_is_gone_and_a_cut_is_recorded() -> None:
    clock = FakeClock()
    first, second = T0 + 120, T0 + 900
    leaves = first + 30
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        warm=lambda run: int(run > second),
        instances={instance_id(): (T0 - 600, leaves), instance_id(): (T0 - 600, second + 30)},
    )
    run, gateway, out = a_run(scaler, [Reply(TURN[:3], cut=True)])
    asyncio.run(run.cycle())

    lines = parsed(out)
    (stream,) = of(lines, "stream")
    (gone, _) = of(lines, "gone")
    # The gateway had nothing to forward until the instance was gone; then the stub cut the answer.
    ((received, _),) = gateway.received
    assert s7.utc(leaves) < gone["read_at"] <= s7.utc(received) == stream["body_sent_at"]
    assert (stream["ended"], stream["events"], stream["error"]) == ("connection_closed", 3, "protocol_error")


def test_a_warm_reading_the_workspace_shows_late_is_waited_for_past_the_settle_time() -> None:
    clock = FakeClock()
    first, second = T0 + 120, T0 + 900
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        # The last instance is counted warm by the run five minutes after the decision, which the
        # workspace shows only some seventy seconds later, past the settle time.
        warm=lambda run: int(run >= second + 300),
        instances={instance_id(): (T0 - 600, first + 30), instance_id(): (T0 - 600, second + 15)},
    )
    read = scaler.read_series

    def lagging(query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s7.WARM_INSTANCES in query and clock.now() < second + 370:
            end = min(end, second + 299)
        return read(query, start=start, end=end, step=step)

    scaler.read_series = lagging  # ty: ignore[invalid-assignment] - a stub's method replaced by the test
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())
    assert [line["path"] for line in of(parsed(out), "scale_in")] == ["terminated", "warm_pool"]


def test_the_held_stream_spans_a_scale_in_decision_however_late_the_scaler_decides() -> None:
    first, second = T0 + 900, T0 + 1500
    scaler = Scaler(
        FakeClock(),
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        warm=lambda run: int(run > second),
        instances={instance_id(): (T0 - 600, first + 30), instance_id(): (T0 - 600, second + 30)},
    )
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())
    lines = parsed(out)
    (decision, _) = of(lines, "scale_in_asked")
    (stream,) = of(lines, "stream")
    assert stream["sent_at"] < decision["written_at"] < stream["body_sent_at"] <= stream["last_event_at"]


def test_warm_readings_the_workspace_shows_late_are_waited_for_before_a_path_is_told() -> None:
    clock = FakeClock()
    first, second = T0 + 120, T0 + 900
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        # The last instance to leave is counted warm three minutes after the decision...
        warm=lambda run: int(run >= second + 180),
        instances={instance_id(): (T0 - 600, first + 30), instance_id(): (T0 - 600, second + 30)},
    )
    read = scaler.read_series

    def lagging(query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        # ... and, for a while, the workspace shows the warm count only up to a minute after it.
        if s7.WARM_INSTANCES in query and clock.now() < second + 420:
            end = min(end, second + 100)
        return read(query, start=start, end=end, step=step)

    scaler.read_series = lagging  # ty: ignore[invalid-assignment] - a stub's method replaced by the test
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())
    assert [line["path"] for line in of(parsed(out), "scale_in")] == ["terminated", "warm_pool"]


def test_the_next_decisions_reading_made_before_it_acted_counts_for_the_departure_before() -> None:
    first, second = T0 + 120, T0 + 540
    scaler = Scaler(
        FakeClock(),
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        # The run that asks for none first counts the first instance warm, before it acts.
        warm=lambda run: int(run >= second),
        instances={instance_id(): (T0 - 600, first + 30), instance_id(): (T0 - 600, second + 30)},
    )
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())
    assert [line["path"] for line in of(parsed(out), "scale_in")] == ["warm_pool", "terminated"]


def test_the_run_goes_on_to_the_wakes_once_both_paths_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    run, _, out = a_run(Scaler(FakeClock(), asked=lambda run: 0, in_service=lambda run: 0))
    cycles: list[int] = []

    async def cycle() -> None:
        cycles.append(1)
        for path in (s7.Path.WARM_POOL, s7.Path.TERMINATED):
            departure = s7.Departure(len(run.departures) + 1, instance_id(), T0, T0)
            run._scale_in_line(departure, s7.Told(path), None, (None, None))
        run.scale_in_streamed = True

    async def nothing() -> None:
        return None

    monkeypatch.setattr(run, "cycle", cycle)
    monkeypatch.setattr(run, "across_deployment", nothing)
    monkeypatch.setattr(run, "send_wakes", nothing)
    asyncio.run(run.run())
    assert cycles == [1]
    assert [line["kind"] for line in parsed(out)][-1] == "run_ended"


def test_a_collector_batch_shown_late_withdraws_the_stop_it_seemed_to_be() -> None:
    clock = FakeClock()
    first, second = T0 + 240, T0 + 1200
    terminates, returns = instance_id(), instance_id()
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        warm=lambda run: int(run > second),
        instances={terminates: (T0 - 600, first + 30), returns: (T0 - 600, second + 30)},
    )
    read = scaler.read_series

    def delayed(query: str, **window: float) -> list[common.Series]:
        # Until a minute after the first decision, the workspace shows one collector's writes only
        # up to T0 + 45: it seems to stop while it serves.
        if query.startswith("max by (instance)") and T0 + 45 < clock.now() < first + 60:
            kept = scaler.instances[returns]
            scaler.instances[returns] = (kept[0], T0 + 45)
            try:
                return read(query, **window)
            finally:
                scaler.instances[returns] = kept
        return read(query, **window)

    scaler.read_series = delayed  # ty: ignore[invalid-assignment] - a stub's method replaced by the test
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())

    lines = parsed(out, terminates, returns)
    (withdrawn,) = of(lines, "gone_withdrawn")
    assert withdrawn["last_write_at"] == s7.utc(T0 + 45)
    departures = [(line["decision"], line["last_write_at"], line["path"]) for line in of(lines, "scale_in")]
    assert departures == [(1, s7.utc(first + 30), "terminated"), (2, s7.utc(second + 30), "warm_pool")]


def test_an_instance_is_named_by_the_order_the_job_first_reads_it() -> None:
    clock = FakeClock()
    first, second = T0 + 240, T0 + 1200
    earlier, later = instance_id(), instance_id()
    # The instance that appears first stops last.
    scaler = Scaler(
        clock,
        asked=lambda run: 2 if run < first else int(run < second),
        in_service=lambda run: 2 if run <= first else int(run <= second),
        warm=lambda run: int(run > second),
        instances={earlier: (T0 + 37, second + 37), later: (T0 + 80, first + 20)},
    )
    run, _, out = a_run(scaler)
    asyncio.run(run.cycle())
    gone = of(parsed(out, earlier, later), "gone")
    assert [(line["last_write_at"], line["instance"]) for line in gone] == [
        (s7.utc(first + 20), "instance-2"),
        (s7.utc(second + 37), "instance-1"),
    ]


def test_an_instance_back_from_the_warm_pool_is_recorded_leaving_again() -> None:
    clock = FakeClock()
    returning = instance_id()
    scaler = Scaler(clock, asked=lambda run: 2, in_service=lambda run: 2)
    run, _, out = a_run(scaler)
    stops = []
    for cycle in range(2):
        start = clock.now()
        first = 60 * (start // 60) + 120
        second = first + 780
        scaler.asked = lambda run, first=first, second=second: 2 if run < first else int(run < second)
        scaler.in_service = lambda run, first=first, second=second: 2 if run <= first else int(run <= second)
        scaler.warm = lambda run, second=second: int(run > second)
        # The instance returning stops last in each cycle; another instance stops first.
        scaler.instances = {instance_id(): (start - 45, first + 30), returning: (start - 45, second + 30)}
        asyncio.run(run.cycle())
        stops.append(s7.utc(second + 30))
        clock.at += 600

    gone = of(parsed(out, returning), "gone")
    assert len(gone) == 4
    named = {line["last_write_at"]: line["instance"] for line in gone}
    # The same name each time: it left, returned and left again.
    assert named[stops[0]] == named[stops[1]]


def test_a_path_is_unknown_unless_the_readings_of_one_scale_in_agree() -> None:
    asked_at, stopped = 1000.0, 1030.0

    def told(warm: list[tuple[float, float]], **changes: Any) -> s7.Told:
        given: dict[str, Any] = {
            "stops": [stopped],
            "leaving": 1,
            "out_of_service": True,
            "end": math.inf,
            "settled": True,
            "newcomer": False,
        }
        return s7.path_of(asked_at=asked_at, warm=warm, **(given | changes))

    def runs(*values: float) -> list[tuple[float, float]]:
        """The scaler's readings, one a minute from the decision's own."""
        return [(asked_at + 0.2 + 60 * n, value) for n, value in enumerate(values)]

    assert told(runs(0, 1)).path is s7.Path.WARM_POOL
    assert told(runs(0, 0, 0, 0, 0, 0, 0)).path is s7.Path.TERMINATED
    # The warm count may rise some time after the stop: a late rise is a return to the warm pool,
    # and the same count over a window another decision cut short tells neither.
    assert told(runs(0, 0, 0, 1)).path is s7.Path.WARM_POOL
    assert told(runs(0, 0, 0, 0, 0, 0, 0), settled=False).unknown is s7.Unknown.NOT_SETTLED
    # Readings that stop short of the settle time, or that miss a run of the scaler within it, the
    # rest not shown yet, establish no termination.
    assert told(runs(0, 0, 0)).unknown is s7.Unknown.WARM_READINGS_END_EARLY
    assert told(runs(0, 0, 0, 0, 0, 0)).unknown is s7.Unknown.WARM_READINGS_END_EARLY
    one_run_missing = [reading for reading in runs(*[0] * 7) if reading[0] != asked_at + 0.2 + 240]
    assert told(one_run_missing).unknown is s7.Unknown.WARM_READINGS_END_EARLY
    # A reading not yet after the stop says nothing of the path, nor one from the next decision on.
    assert told([(1000.2, 0), (1040.2, 0)]).unknown is s7.Unknown.NO_READING_AFTER_STOP
    assert told([(1000.2, 0), (1120.2, 1)], end=1061.0).unknown is s7.Unknown.NO_READING_AFTER_STOP
    # The next decision's own reading, made before it acted, is this one's, unless a run the job did
    # not read came between them.
    cut = {"settled": False, "next_at": 1240.0}
    assert told(runs(0, 0, 0, 0, 1), end=1181.0, **cut).path is s7.Path.WARM_POOL
    assert told(runs(0, 0, 0, 0, 1), end=1121.0, **cut).unknown is s7.Unknown.NOT_SETTLED
    assert told(runs(0, 1, 0, 0, 0, 0, 0)).unknown is s7.Unknown.WARM_COUNT_DIFFERS
    # A warm slot that empties while the instance still writes, and fills again after it stopped,
    # may have been filled by it or by a new instance: neither path is told.
    for counts in [(1.0, 0, 0, 1, 1, 1, 1, 1, 1), (1.0, 0, 0, 2, 2, 2, 2, 2, 2)]:
        emptied = [(1000.2 + 60 * n, value) for n, value in enumerate(counts)]
        assert told(emptied, stops=[1125.0]).unknown is s7.Unknown.WARM_COUNT_DIFFERS
    assert told(runs(0, 1), newcomer=True).unknown is s7.Unknown.NEW_INSTANCE
    assert told(runs(0, 1), out_of_service=False).unknown is s7.Unknown.NOT_READ_OUT_OF_SERVICE
    # Two instances leaving on one decision: told only when the count moved for both or for neither.
    both = {"stops": [stopped, stopped + 300], "leaving": 2}
    assert told(runs(0, 1, 1, 1, 1, 1, 2), **both).path is s7.Path.WARM_POOL
    assert told(runs(*[0] * 12), **both).path is s7.Path.TERMINATED
    assert told(runs(*[0] * 11, 1), **both).unknown is s7.Unknown.ANOTHER_INSTANCE_STOPPED


def test_a_stream_the_gateway_cuts_is_recorded_as_ended_by_the_connection_closing_with_its_events_counted() -> None:
    clock = FakeClock()
    scaler = Scaler(clock, asked=lambda run: 1, in_service=lambda run: 1)
    run, gateway, out = a_run(scaler, [Reply(TURN[:3], cut=True)])
    asyncio.run(run.across_deployment())

    hold, stream = parsed(out)
    assert (hold["kind"], hold["stream"], hold["hold_seconds"]) == ("hold_started", "gateway_deployment", 60.0)
    assert (stream["kind"], stream["stream"]) == ("stream", "gateway_deployment")
    assert (stream["ended"], stream["events"], stream["error"]) == ("connection_closed", 3, "protocol_error")
    assert stream["last_event_at"] is not None
    # The body was held open for the hold, then sent whole: the gateway read it as JSON.
    assert stream["body_sent_at"] == hold["until"]
    assert gateway.received[0][1]["model"] == "mock"


@pytest.mark.parametrize(
    ("reply", "ended", "events"),
    [
        (Reply(), "closing_event", 6),
        (Reply(TURN[:2] + WARMING), "error_event", 3),
        (Reply(TURN[:2]), "connection_closed", 2),
        (Reply([], status=429), "http_status", 0),
    ],
)
def test_a_stream_records_how_it_ended(reply: Reply, ended: str, events: int) -> None:
    clock = FakeClock()
    gateway = Gateway(clock, "key", [reply])
    request = s7._request("mock")
    read = asyncio.run(s7.send("http://gateway.example.com", "key", request, clock=clock, transport=gateway.transport()))
    assert (read.ended, read.events, read.status) == (ended, events, reply.status)


def test_each_wake_goes_at_a_fresh_zero_and_its_first_engine_event_is_told_from_keep_alives() -> None:
    clock = FakeClock()
    idle = 600
    woke: list[float] = []

    def asked(run: float) -> int:
        return int(any(at < run <= at + idle for at in woke))

    scaler = Scaler(clock, asked=asked, in_service=lambda run: asked(run - 60))
    # The first request wakes the pool; the first wake, answered with keep-alives until the hold
    # ends, does not; the second request, sent to wake the pool again, does; the second wake does.
    replies = [Reply(), Reply(PINGS + WARMING), Reply(), Reply(PINGS + TURN)]
    run, gateway, out = a_run(scaler, replies, wakes=2)
    wakes_the_pool = [True, False, True, True]
    handle = gateway.handle

    async def waking(request: httpx.Request) -> httpx.Response:
        answer = await handle(request)
        if wakes_the_pool[len(gateway.received) - 1]:
            woke.append(clock.now())
        return answer

    run.transport = httpx.MockTransport(waking)
    asyncio.run(run.send_wakes())

    lines = parsed(out)
    rewakes, wakes = of(lines, "rewake"), of(lines, "wake")
    assert len(rewakes) == 2
    assert [line["wake"] for line in wakes] == [1, 2]
    lost, answered = wakes
    assert (lost["none_within_hold"], lost["ended"], lost["cause"]) == (True, "error_event", "warming")
    assert lost["first_engine_event_at"] is None and lost["pings"] == 2
    assert lost["capacity_asked_at"] is None
    assert (answered["none_within_hold"], answered["ended"], answered["pings"]) == (False, "closing_event", 2)
    assert answered["first_event_at"] <= answered["first_engine_event_at"]
    assert answered["zero_written_at"] <= answered["zero_read_at"] <= answered["sent_at"]
    assert answered["capacity_asked_at"] > answered["zero_written_at"]
    # The lost wake left the pool asleep: the second wake waited for the pool to wake and idle again.
    assert answered["zero_written_at"] > rewakes[1]["sent_at"] > lost["sent_at"]


def test_a_failing_read_of_the_pool_is_recorded_by_its_kind_without_its_text() -> None:
    clock = FakeClock()
    scaler = Scaler(clock, asked=lambda run: 2, in_service=lambda run: 2, failing=True)
    run, _, out = a_run(scaler)
    assert asyncio.run(run.note()) is None
    (line,) = parsed(out, AWS_TEXT, "ThrottlingException")
    assert (line["kind"], line["source"], line["error"]) == ("read_failed", "pool_state", "aws_error")


def test_the_job_ends_with_a_line_and_no_traceback_when_aws_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused() -> config.Config:
        raise botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied", "Message": AWS_TEXT}}, "GetParameters")

    monkeypatch.setattr(s7.config, "read_ssm", refused)
    assert s7.main({}, FakeClock()) == 1
    captured = capsys.readouterr()
    (line,) = parsed(captured.out.splitlines(), AWS_TEXT, "AccessDenied")
    assert (line["kind"], line["step"], line["error"]) == ("failed", "configuration", "aws_error")
    assert captured.err == ""


def test_the_gateway_stack_output_a_host_name_is_reached_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SimpleNamespace(get_credentials=lambda: None, resource=lambda _: SimpleNamespace(Table=lambda _: None))
    monkeypatch.setattr(s7.boto3, "Session", lambda: session)
    monkeypatch.setattr(s7.config, "read_ssm", lambda: SimpleNamespace(pools=[MOCK]))
    monkeypatch.setattr(s7.common, "canary_keys", lambda: SimpleNamespace(students=[generate_key()]))
    reached: list[httpx.URL] = []

    async def deployment(run: s7.Run) -> None:
        run.transport = httpx.MockTransport(lambda request: reached.append(request.url) or httpx.Response(200))
        await run.across_deployment()

    monkeypatch.setattr(s7.Run, "run", deployment)
    environ = {
        s7.ENDPOINT_VARIABLE: "gateway.example.com",
        s7.WORKSPACE_URL_VARIABLE: "https://workspace.example.com",
        s7.POOL_STATE_TABLE_VARIABLE: "pool-state",
        s7.DEPLOYMENT_HOLD_VARIABLE: "1",
        "AWS_REGION": "us-east-1",
    }
    assert s7.main(environ, FakeClock()) == 0
    assert [(url.scheme, url.host, url.path) for url in reached] == [("https", "gateway.example.com", "/v1/messages")]
