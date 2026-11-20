"""Spike S7's verdict (`spikes/s7_drain.py`): how long an idle instance took to leave on each path,
whether the held streams completed and whether a wake was lost, from recorded numbers. No number
here is a measurement; the instance names are made up.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import common
import s7_drain as s7
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_common.records import ScalerNote
from rhi_gateway.errors import WireFormat, render_stream_event
from rhi_mock_engine.app import Settings, create_app
from rhi_reporter.measure import Verdict, measurement_event

Number = s7.Number

RECORDED: dict[str, int | float] = {
    Number.LEAVE_SECONDS_TERMINATED: 90,
    Number.LEAVE_SECONDS_WARM_POOL: 90,
    Number.STREAM_COMPLETED_SCALE_IN: 1,
    Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT: 1,
    Number.WAKES_SENT: 3,
    Number.WAKES_LOST: 0,
}


def test_an_idle_instance_gone_in_90_seconds_with_every_stream_complete_and_no_wake_lost_passes() -> None:
    assert s7.SPIKE.measurement == "S7"
    assert set(RECORDED) == set(Number)
    assert s7.SPIKE.verdict(RECORDED) is Verdict.PASS
    # The numbers are the declared names, in the shape the reporter takes.
    names = [name.value for name in Number]
    measurement_event("S7", "s7-drain", Verdict.PASS, RECORDED, names=names, commit="a" * 40, bus="bus")


@pytest.mark.parametrize("path", [Number.LEAVE_SECONDS_TERMINATED, Number.LEAVE_SECONDS_WARM_POOL])
def test_an_idle_instance_gone_in_180_seconds_fails(path: Number) -> None:
    assert s7.SPIKE.verdict({**RECORDED, path: 180}) is Verdict.FAIL


def test_one_lost_wake_fails() -> None:
    assert s7.SPIKE.verdict({**RECORDED, Number.WAKES_LOST: 1}) is Verdict.FAIL


@pytest.mark.parametrize("stream", [Number.STREAM_COMPLETED_SCALE_IN, Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT])
def test_a_held_stream_that_did_not_complete_fails(stream: Number) -> None:
    assert s7.SPIKE.verdict({**RECORDED, stream: 0}) is Verdict.FAIL


def test_an_instance_has_left_once_its_collector_has_been_silent() -> None:
    asked_at = 1000.0
    series = [
        common.Series({"instance": "instance-a"}, ((1000.0, 990.0), (1015.0, 1005.0), (1030.0, 1005.0))),
        common.Series({"instance": "instance-b"}, ((1000.0, 990.0), (1015.0, 1080.0))),
    ]
    written = s7.last_writes(series)
    assert written == {"instance-a": 1005.0, "instance-b": 1080.0}
    # instance-b still writes; instance-a has been silent long enough to have stopped.
    assert s7.stopped(written, {"instance-a", "instance-b"}, now=1100.0) == {"instance-a": 1005.0}
    assert s7.leave_seconds(asked_at, 1005.0) == 1005.0 + s7.SCRAPE_SECONDS - asked_at


def test_the_path_follows_the_warm_count() -> None:
    assert s7.path_of(0, 0, newcomer=False) is s7.Path.TERMINATED
    assert s7.path_of(0, 1, newcomer=False) is s7.Path.WARM_POOL
    # A fresh instance launched into the warm pool may stand for a terminated one: not told.
    assert s7.path_of(0, 1, newcomer=True) is None
    assert s7.warm_around([(990.0, 0.0), (1005.0, 0.0), (1020.0, 1.0)], 1000.0) == (0, 1)


def test_a_run_without_both_paths_is_not_measured() -> None:
    streams = {Number.STREAM_COMPLETED_SCALE_IN: True, Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT: True}
    terminated = s7.Departure(s7.Path.TERMINATED, 60.0)
    with pytest.raises(ValueError, match="warm_pool"):
        s7.numbers_of([terminated, s7.Departure(None, 30.0)], streams, 3, 0)
    numbers = s7.numbers_of([terminated, s7.Departure(s7.Path.WARM_POOL, 40.0), terminated], streams, 3, 0)
    assert numbers[Number.LEAVE_SECONDS_WARM_POOL] == 40.0
    assert s7.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_slow_stream_is_read_over_the_seconds_given_and_completes() -> None:
    key = generate_key()

    async def no_delay(seconds: float) -> None:
        pass

    paused: list[float] = []

    async def pause(seconds: float) -> None:
        paused.append(seconds)

    transport = httpx.ASGITransport(app=create_app(Settings(secret=key), sleep=no_delay))
    request = s7._request("mock")
    assert asyncio.run(s7.slow_stream("http://engine", key, request, 60.0, transport=transport, sleep=pause))
    assert sum(paused) == pytest.approx(60.0)
    # A student the engine refuses has no stream to complete.
    refused = httpx.ASGITransport(app=create_app(Settings(secret=key), sleep=no_delay))
    assert not asyncio.run(s7.slow_stream("http://engine", generate_key(), request, 60.0, transport=refused))


class _Departing:
    """A pool whose one instance, writing when the scaler asked for fewer, wrote last at `last`,
    and whose warm count the scaler read as `warm`."""

    name = "mock"

    def __init__(self, asked_at: int, last: float, warm: list[tuple[float, float]]) -> None:
        self.asked_at, self.last, self.warm = asked_at, last, warm

    def note(self) -> ScalerNote:
        return ScalerNote(pool="mock", capacity_asked=0, no_capacity_at=None, written_at=self.asked_at)

    def writes(self, start: float, end: float) -> dict[str, float]:
        return {"instance-a": float(self.asked_at) if end == self.asked_at else self.last}

    def warm_counts(self, start: float, end: float) -> list[tuple[float, float]]:
        return self.warm


@pytest.mark.parametrize(
    ("last", "warm", "path"),
    [
        (1005.0, [(990.0, 0.0), (1080.0, 0.0)], s7.Path.TERMINATED),
        (1005.0, [(990.0, 0.0), (1080.0, 1.0)], s7.Path.WARM_POOL),
        # No reading of the warm count across the departure: its path is not told.
        (1005.0, [], None),
        (1005.0, [(990.0, 0.0), (1010.0, 0.0)], None),
        # Still writing when the job stops waiting: no stop was seen.
        (2000.0, [(990.0, 0.0), (1980.0, 1.0)], None),
    ],
)
def test_a_departure_has_a_path_only_when_its_stop_and_the_warm_count_were_seen(
    monkeypatch: pytest.MonkeyPatch, last: float, warm: list[tuple[float, float]], path: s7.Path | None
) -> None:
    now = 1000 + (s7.LEAVE_WAIT_SECONDS + 1 if last > 1500 else 110)
    monkeypatch.setattr(s7.time, "time", lambda: now)
    pool = _Departing(1000, last, warm)
    run = s7.Run(pool, "http://gateway", [generate_key()], SimpleNamespace(), 1, 60)  # ty: ignore[invalid-argument-type] - a double
    departure = asyncio.run(run.departure(1, 1))
    assert departure.path is path
    if path is not None:
        assert departure.seconds == s7.leave_seconds(1000, last)


def test_a_held_stream_is_answered_only_once_the_event_it_spans_is_over() -> None:
    key, happened = generate_key(), []
    engine = create_app(Settings(secret=key))

    async def recorded(scope: Any, receive: Any, send: Any) -> None:
        async def sent(message: Any) -> None:
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                happened.append("answered")

        await engine(scope, receive, sent)

    async def over() -> bool:
        if len(happened) < 3:
            happened.append("held")
            return False
        happened.append("over")
        return True

    async def no_wait(seconds: float) -> None:
        pass

    transport = httpx.ASGITransport(app=recorded)
    request = s7._request("mock")
    held = s7.slow_stream("http://engine", key, request, 600.0, until=over, transport=transport, sleep=no_wait)
    assert asyncio.run(held)
    assert happened == ["held", "held", "held", "over", "answered"]


def _warming_stream(request: httpx.Request) -> httpx.Response:
    event = render_stream_event(Cause.WARMING, WireFormat.MESSAGES)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=event)


def _cut(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("cut", request=request)


def test_a_wake_is_lost_unless_its_inference_completes() -> None:
    key = generate_key()
    engine = httpx.ASGITransport(app=create_app(Settings(secret=key)))
    assert asyncio.run(s7.wake("http://gateway", key, "mock", 5, transport=engine))
    for respond in (_warming_stream, _cut, lambda request: httpx.Response(503)):
        lost = s7.wake("http://gateway", key, "mock", 5, transport=httpx.MockTransport(respond))
        assert not asyncio.run(lost)


def test_each_wake_goes_when_no_instance_serves_and_a_lost_one_does_not_end_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pool as the scaler reads it, (asked, in service), one reading per look.
    readings = iter([(1, 1), (0, 1), (0, 0), (1, 1), (0, 0), (1, 1), (0, 0)])
    now = next(readings)
    pool = SimpleNamespace(name="mock", asked=lambda: now[0], gauge=lambda name: now[1])
    answers = iter([True, False, True, True, True])  # the second request is a lost wake
    sent: list[tuple[int, int]] = []

    async def wait(condition: Any, *arguments: Any) -> None:
        nonlocal now
        while not condition():
            now = next(readings)

    async def wake(*arguments: Any, **options: Any) -> bool:
        sent.append(now)
        return next(answers)

    monkeypatch.setattr(s7, "_wait", wait)
    monkeypatch.setattr(s7, "wake", wake)
    settings = SimpleNamespace(hold_seconds=30, idle_minutes=1)
    run = s7.Run(pool, "http://gateway", [generate_key()], settings, 3, 60)  # ty: ignore[invalid-argument-type] - a double
    assert asyncio.run(run.send_wakes()) == (3, 1)
    # A request to bring the pool back, the first wake, lost, at zero in service, another request
    # to bring the pool back, then the two other wakes, each at zero in service.
    assert sent == [(1, 1), (0, 0), (0, 0), (0, 0), (0, 0)]


def test_a_gateway_deployment_counts_only_when_seen_under_the_stream() -> None:
    tasks = {"task-a": (900.0, 1500.0), "task-b": (1100.0, 1500.0)}
    assert s7.rollout_at({"task-a"}, tasks) == 1100.0
    assert s7.rollout_at({"task-a", "task-b"}, tasks) is None
    assert s7.across_rollout(completed=True, spanned=True, rolled_out_at=1100.0)
    assert not s7.across_rollout(completed=False, spanned=False, rolled_out_at=1100.0)
    for completed, spanned in ((True, False), (False, False)):
        with pytest.raises(ValueError, match="no gateway deployment"):
            s7.across_rollout(completed=completed, spanned=spanned, rolled_out_at=None)
    # A stream the mock answers while the same task writes throughout is no survived deployment.
    key = generate_key()
    pool = SimpleNamespace(name="mock", gateway_tasks=lambda start, end: {"task-a": (start, end)})
    engine = httpx.ASGITransport(app=create_app(Settings(secret=key)))
    run = s7.Run(pool, "http://gateway", [key], SimpleNamespace(), 1, 0.01, transport=engine)  # ty: ignore[invalid-argument-type] - a double
    with pytest.raises(RuntimeError, match="no gateway deployment"):
        asyncio.run(run.across_deployment())


def test_a_task_that_appears_while_the_old_ones_keep_writing_is_no_rollout() -> None:
    began, ended, now = 1000.0, 1500.0, 1700.0
    transient = {"task-a": (900.0, 1690.0), "task-b": (1100.0, 1200.0)}
    assert s7.rollout_under({"task-a"}, transient, began, ended, now) is None
    replaced = {"task-a": (900.0, 1200.0), "task-b": (1100.0, 1690.0)}
    assert s7.rollout_under({"task-a"}, replaced, began, ended, now) == 1200.0
    # The new task had stopped before the old one did: nothing took its place.
    gone_first = {"task-a": (900.0, 1300.0), "task-b": (1100.0, 1200.0)}
    assert s7.rollout_under({"task-a"}, gone_first, began, ended, now) is None


def test_the_warm_baseline_is_the_reading_the_scaler_decided_on() -> None:
    # The scaler writes the readings it decided on just after its note's whole second.
    already_warm = [(940.0, 0.0), (1000.4, 1.0), (1060.0, 1.0)]
    assert s7.warm_around(already_warm, 1000.0) == (1, 1)
    assert s7.path_of(*s7.warm_around(already_warm, 1000.0) or (0, 0), newcomer=False) is s7.Path.TERMINATED
    went_warm = [(940.0, 0.0), (1000.4, 0.0), (1060.2, 1.0)]
    assert s7.warm_around(went_warm, 1000.0) == (0, 1)


class _Streaming(httpx.AsyncBaseTransport):
    """An ASGI transport that hands the client each part of an answer as the app sends it, as a
    connection does: httpx's own hands over the whole answer once the app has returned. `read` is
    set once the client has a part of the answer."""

    def __init__(self, app: Any) -> None:
        self.app, self.read = app, asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = b"".join([part async for part in request.stream])  # ty: ignore[not-iterable] - an async stream
        parts: asyncio.Queue[bytes | None] = asyncio.Queue()
        started: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": request.method,
            "headers": [(name.lower(), value) for name, value in request.headers.raw], "scheme": "http",
            "path": request.url.path, "raw_path": request.url.raw_path, "query_string": request.url.query,
            "server": ("gateway", 80), "client": ("127.0.0.1", 1), "root_path": "",
        }  # fmt: skip
        received = False

        async def receive() -> dict[str, Any]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()  # the client stays until the answer ends
            return {"type": "http.disconnect"}

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start":
                started.set_result(message)
            elif message["type"] == "http.response.body":
                parts.put_nowait(message.get("body", b""))
                if not message.get("more_body", False):
                    parts.put_nowait(None)

        app = asyncio.ensure_future(self.app(scope, receive, send))
        start = await started
        transport = self

        class Parts(httpx.AsyncByteStream):
            async def __aiter__(self) -> Any:
                while (part := await parts.get()) is not None:
                    if part:
                        transport.read.set()
                        yield part

            async def aclose(self) -> None:
                app.cancel()

        return httpx.Response(start["status"], headers=start.get("headers", []), stream=Parts())


def test_the_stream_across_a_scale_in_is_answered_on_both_sides_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    key, happened = generate_key(), []
    engine = create_app(Settings(secret=key))

    async def scenario() -> bool | None:
        answering, departed = asyncio.Event(), asyncio.Event()
        transport = _Streaming(None)

        async def recorded(scope: Any, receive: Any, send: Any) -> None:
            async def sent(message: Any) -> None:
                if message["type"] == "http.response.start":
                    happened.append("answering")
                    answering.set()
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    # The closing event goes only once the instance has left.
                    await asyncio.wait_for(departed.wait(), 5)
                    await send(message)
                    happened.append("answered")
                    return
                await send(message)

            await engine(scope, receive, sent)

        async def nothing() -> None:
            pass

        async def departure(asked: int, wait: float, after: Any = None) -> s7.Departure:
            if after is not None:
                await after
                return s7.Departure(s7.Path.WARM_POOL, 60.0, "instance-b")
            try:
                # The client has the first part of the answer: its inference has begun.
                await asyncio.wait_for(transport.read.wait(), 1)
            except TimeoutError:
                pass
            happened.append("scale-in")
            departed.set()
            return s7.Departure(s7.Path.TERMINATED, 60.0, "instance-a")

        transport.app = recorded
        pool: Any = SimpleNamespace(name="mock")
        settings: Any = SimpleNamespace(idle_minutes=0)
        run = s7.Run(pool, "http://gateway", [key], settings, 1, 60, transport=transport)
        monkeypatch.setattr(run, "two_instances", nothing)
        monkeypatch.setattr(run, "departure", departure)
        return await run.cycle(scale_in_stream=True)

    assert asyncio.run(scenario()) is True
    # A request was being answered when the instance left, and its answer ended after.
    assert happened[:3] == ["answering", "scale-in", "answered"]


def test_a_scale_in_seen_over_between_two_streams_is_not_a_stream_across_it(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_key()
    engine = create_app(Settings(secret=key))

    async def scenario() -> tuple[bool | None, int]:
        answered, departed = asyncio.Event(), asyncio.Event()
        streams = 0

        async def gateway(scope: Any, receive: Any, send: Any) -> None:
            nonlocal streams
            streams += 1
            if answered.is_set():
                # The next stream is held at the gateway until the instance has left.
                await departed.wait()

            async def sent(message: Any) -> None:
                await send(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    answered.set()

            await engine(scope, receive, sent)

        async def nothing() -> None:
            pass

        async def departure(asked: int, wait: float, after: Any = None) -> s7.Departure:
            if after is not None:
                await after
                return s7.Departure(s7.Path.WARM_POOL, 60.0, "instance-b")
            await asyncio.wait_for(answered.wait(), 1)
            departed.set()
            return s7.Departure(s7.Path.TERMINATED, 60.0, "instance-a")

        pool: Any = SimpleNamespace(name="mock")
        settings: Any = SimpleNamespace(idle_minutes=0)
        run = s7.Run(pool, "http://gateway", [key], settings, 1, 60, transport=_Streaming(gateway))
        monkeypatch.setattr(run, "two_instances", nothing)
        monkeypatch.setattr(run, "departure", departure)
        return await run.cycle(scale_in_stream=True), streams

    # The first stream ended before the scale-in, the second began after: neither spanned it.
    assert asyncio.run(scenario()) == (None, 2)


class _TwoInstances:
    """A pool where instance-a, writing when the scaler asked for fewer at 1000, last wrote at
    1005, and instance-b writes from `b_from` on, and whose warm count the scaler read as `warm`."""

    name = "mock"

    def __init__(self, b_from: float, warm: list[tuple[float, float]]) -> None:
        self.b_from, self.warm = b_from, warm

    def note(self) -> ScalerNote:
        return ScalerNote(pool="mock", capacity_asked=1, no_capacity_at=None, written_at=1000)

    def writes(self, start: float, end: float) -> dict[str, float]:
        found = {"instance-a": min(end, 1005.0)}
        if end >= self.b_from:
            found["instance-b"] = min(end, 1115.0)
        return found

    def warm_counts(self, start: float, end: float) -> list[tuple[float, float]]:
        return self.warm


@pytest.mark.parametrize(
    ("b_from", "warm", "path"),
    [
        # The decision run's own reading is the baseline.
        (500.0, [(940.0, 1.0), (1000.3, 1.0), (1060.0, 1.0)], s7.Path.TERMINATED),
        (500.0, [(940.0, 1.0), (1000.3, 1.0), (1060.0, 2.0)], s7.Path.WARM_POOL),
        # Its push was lost: one warm instance read before may have left and been replaced.
        (500.0, [(940.0, 1.0), (1060.0, 1.0)], None),
        # An empty warm pool read before has none to lose.
        (500.0, [(940.0, 0.0), (1060.0, 1.0)], s7.Path.WARM_POOL),
        # Unless an instance new since that reading may have filled it.
        (980.0, [(940.0, 0.0), (1060.0, 1.0)], None),
    ],
)
def test_the_path_is_told_only_from_the_warm_count_the_decision_was_made_on(
    monkeypatch: pytest.MonkeyPatch, b_from: float, warm: list[tuple[float, float]], path: s7.Path | None
) -> None:
    monkeypatch.setattr(s7.time, "time", lambda: 1120.0)
    run = s7.Run(_TwoInstances(b_from, warm), "http://gateway", [generate_key()], SimpleNamespace(), 1, 60)  # ty: ignore[invalid-argument-type] - a double
    departure = asyncio.run(run.departure(2, 1))
    assert (departure.instance, departure.path) == ("instance-a", path)


@pytest.mark.parametrize(
    ("old_last", "new_first", "query_seconds", "survived"),
    [
        # The old task was replaced while the job read the tasks, before the stream opened.
        (1005.0, 1010.0, 25.0, None),
        # It was replaced while the stream was held.
        (1200.0, 1100.0, 0.0, True),
    ],
)
def test_a_gateway_deployment_counts_only_when_it_replaced_a_task_after_the_stream_opened(
    monkeypatch: pytest.MonkeyPatch, old_last: float, new_first: float, query_seconds: float, survived: bool | None
) -> None:
    key = generate_key()
    engine = create_app(Settings(secret=key))
    clock = [1000.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])

    def tasks(start: float, end: float) -> dict[str, tuple[float, float]]:
        if end == 1000.0:
            clock[0] += query_seconds
        # task-c, the old task the stream is on, runs until the stream has ended.
        found = {"task-a": (900.0, min(end, old_last)), "task-c": (900.0, end)}
        if end >= new_first:
            found["task-b"] = (new_first, end)
        return found

    async def gateway(scope: Any, receive: Any, send: Any) -> None:
        async def held() -> Any:
            message = await receive()
            if not message.get("more_body", False):
                clock[0] = 1400.0  # the stream is let go, long after
            return message

        await engine(scope, held, send)

    pool = SimpleNamespace(name="mock", gateway_tasks=tasks)
    run = s7.Run(pool, "http://gateway", [key], SimpleNamespace(), 1, 0.01, transport=httpx.ASGITransport(app=gateway))  # ty: ignore[invalid-argument-type] - a double
    if survived is None:
        with pytest.raises(RuntimeError, match="no gateway deployment"):
            asyncio.run(run.across_deployment())
    else:
        assert asyncio.run(run.across_deployment()) is survived


def test_only_the_streams_in_flight_while_the_instance_left_are_across_the_scale_in() -> None:
    gone = s7.Departure(s7.Path.TERMINATED, 20.0, "instance-a", asked_at=1000.0, stopped_at=1020.0)

    def stream(began: float, ended: float, completed: bool = True) -> s7.Streamed:
        return s7.Streamed(began, ended, s7.Answer(completed, over_as_begun=False, over_as_ended=True))

    # Begun after the stop, while the job waited for the collector to stay silent: not across it.
    assert s7.across_scale_in([stream(990.0, 999.0), stream(1115.0, 1116.0)], gone) is None
    assert s7.across_scale_in([stream(999.0, 1001.0), stream(1115.0, 1116.0)], gone) is True
    assert s7.across_scale_in([stream(1010.0, 1011.0, completed=False)], gone) is False
    # No stop seen: nothing to be across.
    assert s7.across_scale_in([stream(999.0, 1001.0)], s7.Departure(None, 900.0, asked_at=1000.0)) is None


def test_a_stream_begun_after_the_measured_stop_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s7.time, "time", lambda: 1115.0)
    key = generate_key()
    engine = create_app(Settings(secret=key))
    pool = _TwoInstances(500.0, [(1000.3, 0.0), (1060.0, 0.0)])  # instance-a stopped by 1020

    async def scenario() -> bool | None:
        seen_over = asyncio.Event()

        async def held(scope: Any, receive: Any, send: Any) -> None:
            async def sent(message: Any) -> None:
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    await seen_over.wait()  # in flight, at 1115, when the stop is seen over
                await send(message)

            await engine(scope, receive, sent)

        run = s7.Run(pool, "http://gateway", [key], SimpleNamespace(), 1, 60, transport=_Streaming(held))  # ty: ignore[invalid-argument-type] - a double
        first = asyncio.create_task(run.departure(2, 1))
        first.add_done_callback(lambda _: seen_over.set())
        return await run.relay(first.done)

    assert asyncio.run(scenario()) is None


def test_a_deployment_stream_opened_while_a_replacement_was_already_serving_is_not_measured() -> None:
    on_new = {"task-a": (900.0, 1120.0), "task-b": (1010.0, 1400.0)}
    assert s7.rollout_under({"task-a"}, on_new, 1020.0, 1300.0, 1400.0) is None
    after_open = {"task-a": (900.0, 1120.0), "task-b": (1030.0, 1400.0)}
    assert s7.rollout_under({"task-a"}, after_open, 1020.0, 1300.0, 1400.0) == 1120.0


def _note(asked: int, at: int) -> ScalerNote:
    return ScalerNote(pool="mock", capacity_asked=asked, no_capacity_at=None, written_at=at)


def test_the_warm_count_of_a_departure_ends_before_the_next_decision() -> None:
    assert s7.warm_bound(1, [_note(1, 1060), _note(1, 1120)], 1000.0) == math.inf
    assert s7.warm_bound(1, [_note(1, 1060), _note(0, 1120), _note(0, 1180)], 1000.0) == 1060.0 + s7.NOTE_SECOND


def test_a_warm_return_after_the_next_decision_is_not_the_earlier_departure_s(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1391.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])

    class Pool(_TwoInstances):
        def note(self) -> ScalerNote:
            # The next decision, at 1300, came while instance-a, asked away at 1000, still drained.
            return _note(1, 1000) if clock[0] < 1300 else _note(0, 1300)

        def writes(self, start: float, end: float) -> dict[str, float]:
            return {"instance-a": min(end, 1300.0), "instance-b": min(end, 1360.0)}

    async def scenario() -> s7.Departure:
        clock[0] = 1000.0
        pool: Any = Pool(500.0, [(1000.4, 0.0), (1300.4, 0.0), (1360.4, 1.0)])
        settings: Any = SimpleNamespace()
        run = s7.Run(pool, "http://gateway", [generate_key()], settings, 1, 60)

        async def wait(condition: Any, seconds: float, what: str, *arguments: Any) -> None:
            if what == "an instance to stop":
                clock[0] = 1391.0
            assert condition()

        monkeypatch.setattr(s7, "_wait", wait)
        return await run.departure(2, 1)

    departure = asyncio.run(scenario())
    assert (departure.instance, departure.path) == ("instance-a", None)


class _Workspace:
    """A workspace whose samples of one gauge are `samples`, which become `later` once read."""

    def __init__(self, samples: list[tuple[float, float]], later: list[tuple[float, float]] | None = None) -> None:
        self.samples, self.later = samples, later

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        found, at = [], start
        while at <= end:
            seen = [(stamp, value) for stamp, value in self.samples if stamp <= at]
            if seen:
                found.append((at, seen[-1][0] if query.startswith("timestamp(") else seen[-1][1]))
            at += step
        if self.later is not None:
            self.samples, self.later = self.later, None
        return [common.Series({"pool": "mock"}, tuple(found))]


def test_a_reading_is_its_own_value_at_its_own_time() -> None:
    # A newer reading becomes visible between the queries: its time is not paired with the old value.
    workspace = _Workspace([(1000.4, 0.0)], later=[(1000.4, 0.0), (1119.5, 1.0)])
    pool = s7.Pool("mock", workspace, None)  # ty: ignore[invalid-argument-type] - a double
    assert pool.warm_counts(880.0, 1120.0) == [(1000.4, 0.0)]
    assert pool.warm_counts(880.0, 1120.0) == [(1000.4, 0.0), (1119.5, 1.0)]


def test_a_gauge_older_than_the_scaler_s_note_is_not_its_last_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1001.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])
    note = [_note(0, 1000)]
    table = SimpleNamespace(get_item=lambda **arguments: {"Item": note[0].to_item()})
    # Zero in service, read at 940, before a wake: the note asking for none is newer.
    pool = s7.Pool("mock", _Workspace([(940.3, 0.0)]), table)  # ty: ignore[invalid-argument-type] - a double
    assert pool.gauge(s7.INSTANCES_IN_SERVICE) is None
    clock[0], note[0] = 1065.0, _note(0, 1060)
    pool = s7.Pool("mock", _Workspace([(940.3, 0.0), (1000.3, 1.0), (1060.3, 0.0)]), table)  # ty: ignore[invalid-argument-type] - a double
    assert pool.gauge(s7.INSTANCES_IN_SERVICE) == 0


def test_completed_requests_between_the_ask_and_the_stop_do_not_span_the_scale_in() -> None:
    gone = s7.Departure(s7.Path.TERMINATED, 60.0, "node-a", asked_at=1000.0, stopped_at=1060.0)

    def stream(began: float, ended: float) -> s7.Streamed:
        return s7.Streamed(began, ended, s7.Answer(completed=True, begun_at=began))

    assert s7.across_scale_in([stream(1010.0, 1010.4), stream(1020.0, 1020.4)], gone) is None
    assert s7.across_scale_in([stream(1010.0, 1010.4), stream(1059.8, 1060.3)], gone) is True


def test_a_completed_stream_was_on_a_replaced_task_only_if_an_old_one_ran_until_it_ended() -> None:
    before, stop, ended = {"task-a", "task-c"}, 1200.0, 1400.0
    # task-a stopped; task-c, which the stream kept running, wrote within a scrape of its end.
    assert s7.held_under(before, {"task-a": (900.0, 1200.0), "task-c": (900.0, 1395.0)}, stop, ended, 1400.0)
    # Every old task had stopped well before: the stream was on a replacement.
    on_new = {"task-a": (900.0, 1200.0), "task-c": (900.0, 1210.0), "task-b": (1030.0, 1400.0)}
    assert s7.held_under(before, on_new, stop, ended, 1400.0) is False
    # task-c last showed 1350: it may still write, or stop.
    assert s7.held_under(before, {"task-a": (900.0, 1200.0), "task-c": (900.0, 1350.0)}, stop, ended, 1400.0) is None


def test_a_stream_on_a_replacement_already_serving_does_not_prove_survival(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])
    key = generate_key()
    engine = create_app(Settings(secret=key))

    def tasks(start: float, end: float) -> dict[str, tuple[float, float]]:
        if end == 1000.0:
            clock[0] = 1020.0
        found = {"old-task": (900.0, min(end, 1200.0))}
        if end >= 1030.0:
            found["new-task"] = (1030.0, end)  # its collector first wrote after it took the stream
        return found

    async def gateway(scope: Any, receive: Any, send: Any) -> None:
        async def read() -> Any:
            message = await receive()
            if not message.get("more_body", False):
                clock[0] = 1400.0
            return message

        await engine(scope, read, send)

    pool = SimpleNamespace(name="mock", gateway_tasks=tasks)
    run = s7.Run(pool, "http://gateway", [key], SimpleNamespace(), 1, 0.01, transport=httpx.ASGITransport(app=gateway))  # ty: ignore[invalid-argument-type] - a double
    with pytest.raises(RuntimeError, match="no gateway deployment"):
        asyncio.run(run.across_deployment())


@pytest.mark.parametrize("completes", [True, False], ids=["answers", "disconnects"])
def test_a_stream_a_late_written_replacement_may_hold_is_unmeasured_while_an_old_task_runs_on(
    monkeypatch: pytest.MonkeyPatch, completes: bool
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])
    key = generate_key()
    engine = create_app(Settings(secret=key))

    def tasks(start: float, end: float) -> dict[str, tuple[float, float]]:
        if end == 1000.0:
            clock[0] = 1020.0  # the stream opens at 1020
        # old-a is replaced; old-b runs on, the rollout waiting for a second replacement.
        found = {"old-a": (900.0, min(end, 1200.0)), "old-b": (900.0, end)}
        if end >= 1030.0:
            found["new-a"] = (1030.0, end)  # serving from 1015, its collector first wrote at 1030
        return found

    async def gateway(scope: Any, receive: Any, send: Any) -> None:
        async def read() -> Any:
            message = await receive()
            if not message.get("more_body", False):
                clock[0] = 1400.0
                if not completes:
                    raise httpx.ReadError("cut")
            return message

        await engine(scope, read, send)

    pool = SimpleNamespace(name="mock", gateway_tasks=tasks)
    run = s7.Run(pool, "http://gateway", [key], SimpleNamespace(), 1, 0.01, transport=httpx.ASGITransport(app=gateway))  # ty: ignore[invalid-argument-type] - a double
    with pytest.raises(RuntimeError, match="no gateway deployment"):
        asyncio.run(run.across_deployment())


@pytest.mark.parametrize(("began", "ended"), [(999.9, 1000.2), (1019.8, 1020.3)], ids=["before-ask", "after-stop"])
def test_a_stream_that_only_crosses_a_bound_of_a_scale_in_event_is_unmeasured(
    monkeypatch: pytest.MonkeyPatch, began: float, ended: float
) -> None:
    # The run began at 1000 and asked for fewer by 1120, when the job read its note; idle-node last
    # wrote at 1005, so it stopped between 1005 and 1020.
    monkeypatch.setattr(s7.time, "time", lambda: 1120.0)

    class Pool:
        name = "mock"

        def note(self) -> ScalerNote:
            return _note(1, 1000)

        def writes(self, start: float, end: float) -> dict[str, float]:
            return {"idle-node": min(end, 1005.0), "serving-node": end}

        def warm_counts(self, start: float, end: float) -> list[tuple[float, float]]:
            return [(1000.3, 0.0), (1060.3, 0.0)]

    run = s7.Run(Pool(), "http://gateway", [generate_key()], SimpleNamespace(), 1, 60)  # ty: ignore[invalid-argument-type] - a double
    departure = asyncio.run(run.departure(2, 1))
    assert departure.events() == [(1000.0, 1120.0), (1005.0, 1020.0)]
    caught = s7.Streamed(began, ended, s7.Answer(completed=True, begun_at=began))
    assert s7.across_scale_in([caught], departure) is None
    # A stream in flight over the whole of the stop's bounds spanned it.
    spanning = s7.Streamed(1004.0, 1021.0, s7.Answer(completed=True, begun_at=1004.0))
    assert s7.across_scale_in([caught, spanning], departure) is True
