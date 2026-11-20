"""Spike S7's verdict (`spikes/s7_drain.py`): how long an idle instance took to leave on each path,
whether the held streams completed and whether a wake was lost, from recorded numbers. No number
here is a measurement; the instance names are made up.
"""

from __future__ import annotations

import asyncio
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


def test_the_stream_across_a_scale_in_is_answered_on_both_sides_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    key, happened = generate_key(), []
    engine = create_app(Settings(secret=key))

    async def scenario() -> bool | None:
        answering, departed = asyncio.Event(), asyncio.Event()

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
                await asyncio.wait_for(answering.wait(), 1)
            except TimeoutError:
                pass
            happened.append("scale-in")
            departed.set()
            return s7.Departure(s7.Path.TERMINATED, 60.0, "instance-a")

        transport = httpx.ASGITransport(app=recorded)
        pool: Any = SimpleNamespace(name="mock")
        settings: Any = SimpleNamespace(idle_minutes=0)
        run = s7.Run(pool, "http://gateway", [key], settings, 1, 60, transport=transport)
        monkeypatch.setattr(run, "two_instances", nothing)
        monkeypatch.setattr(run, "departure", departure)
        return await run.cycle(scale_in_stream=True)

    assert asyncio.run(scenario()) is True
    # A request was being answered when the instance left, and its answer ended after.
    assert happened[:3] == ["answering", "scale-in", "answered"]
