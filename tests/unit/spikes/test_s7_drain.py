"""How spike S7 (`spikes/s7_drain.py`) reads a run: which instance left, which held stream spanned
a scale-in or a gateway deployment, and how many wakes it sent. Times are made up; no AWS, no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import s7_drain as s7
from rhi_common.keys import generate_key
from rhi_common.records import ScalerNote
from rhi_mock_engine.app import Settings, create_app


def _note(asked: int, at: int) -> ScalerNote:
    return ScalerNote(pool="mock", capacity_asked=asked, no_capacity_at=None, written_at=at)


def test_an_instance_stopped_before_the_scaler_asked_is_not_the_departure(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1005.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])

    async def wait(condition: Any, *arguments: Any, **options: Any) -> None:
        if not condition():
            clock[0] = 1170.0
        assert condition()

    monkeypatch.setattr(s7, "_wait", wait)

    class Pool:
        name = "mock"

        def note(self) -> ScalerNote:
            return _note(1, 1000)

        def writes(self, start: float, end: float) -> dict[str, float]:
            # previous-node stopped at 980, before the run that asked for fewer began at 1000.
            return {"previous-node": 980.0, "idle-node": min(end, 1030.0), "serving-node": end}

        def warm_counts(self, start: float, end: float) -> list[tuple[float, float]]:
            return [(1000.2, 0.0), (1060.2, 0.0)]

    run = s7.Run(Pool(), "http://gateway", [generate_key()], SimpleNamespace(), 1, 60)  # ty: ignore[invalid-argument-type] - a double
    departure = asyncio.run(run.departure(2, 1))
    assert (departure.instance, departure.path) == ("idle-node", s7.Path.TERMINATED)
    assert departure.seconds == s7.leave_seconds(1000, 1030.0)


class _Frames(httpx.AsyncBaseTransport):
    """Runs the mock to its end, its pacing moving the clock, then hands the client each part of
    its answer as the moment it was written."""

    def __init__(self, app: Any, clock: list[float]) -> None:
        self.app, self.clock = app, clock

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        frames: list[tuple[float, bytes]] = []
        start: list[Any] = []
        received = False

        async def receive() -> dict[str, Any]:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.append(message)
            elif message.get("body"):
                frames.append((self.clock[0], message["body"]))

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": request.method,
            "headers": [(name.lower(), value) for name, value in request.headers.raw], "scheme": "http",
            "path": request.url.path, "raw_path": request.url.raw_path, "query_string": request.url.query,
            "server": ("gateway", 80), "client": ("127.0.0.1", 1), "root_path": "",
        }  # fmt: skip
        await self.app(scope, receive, send)
        clock = self.clock

        class Parts(httpx.AsyncByteStream):
            async def __aiter__(self) -> Any:
                for moment, data in frames:
                    clock[0] = max(clock[0], moment)
                    yield data

        return httpx.Response(start[0]["status"], headers=start[0].get("headers", []), stream=Parts())


def test_the_scale_in_relay_holds_one_mock_response_open_across_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [999.75]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])
    key = generate_key()

    async def pace(seconds: float) -> None:
        clock[0] += seconds

    real_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        clock[0] += seconds
        await real_sleep(0)

    monkeypatch.setattr(s7.asyncio, "sleep", sleep)
    # The mock writes its whole turn in under half a second; the scaler's run began at 1000, and the
    # job read its note at 1005; the instance last wrote at 1030, so stopped by 1045.
    gone = s7.Departure(s7.Path.TERMINATED, 45.0, "idle-node", 1000.0, 1045.0, 1005.0, 1030.0)
    sent: list[float] = []

    def over() -> bool:
        if not run.leaving and clock[0] >= 1045.0 + s7.QUIET_SECONDS:
            run.leaving.append(gone)
        return bool(run.leaving)

    def counted(app: Any) -> Any:
        async def gateway(scope: Any, receive: Any, send: Any) -> None:
            sent.append(clock[0])
            await app(scope, receive, send)

        return gateway

    transport = _Frames(counted(create_app(Settings(secret=key), sleep=pace)), clock)
    run = s7.Run(SimpleNamespace(name="mock"), "http://gateway", [key], SimpleNamespace(), 1, 60, transport=transport)  # ty: ignore[invalid-argument-type] - a double
    assert asyncio.run(run.relay(over)) is True
    # One response, begun before the decision, was held open until the scale-in was over.
    assert sent == [999.75]


def test_a_connection_that_never_opened_is_not_a_cut_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1040.0]
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])

    async def no_wait(seconds: float) -> None:
        pass

    monkeypatch.setattr(s7.asyncio, "sleep", no_wait)
    attempted: list[bool] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempted.append(True)
        raise httpx.ConnectError("never opened", request=request)

    gone = s7.Departure(s7.Path.TERMINATED, 115.0, "idle-node", 1000.0, 1115.0, 1005.0, 1100.0)

    def over() -> bool:
        if attempted and not run.leaving:
            clock[0] = 1210.0
            run.leaving.append(gone)
        return bool(attempted)

    transport = httpx.MockTransport(refuse)
    run = s7.Run(SimpleNamespace(name="mock"), "http://gateway", [generate_key()], SimpleNamespace(), 1, 60, transport=transport)  # ty: ignore[invalid-argument-type] - a double
    assert asyncio.run(run.relay(over)) is None
    # A stream cut once it was in flight still fails.
    cut = s7.Streamed(1040.0, 1210.0, s7.Answer(completed=False))
    assert s7.across_scale_in([cut], gone) is False


def test_one_gateway_task_that_drains_a_completed_stream_is_a_survived_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    released: list[float] = []
    monkeypatch.setattr(s7.time, "time", lambda: clock[0])
    monkeypatch.setattr(s7.time, "monotonic", lambda: clock[0])

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(s7.asyncio, "sleep", sleep)

    def tasks(start: float, end: float) -> dict[str, tuple[float, float]]:
        # The one old task runs until the stream lets it go; its replacement first wrote at 1120.
        found = {"old-task": (900.0, min(end, released[0]) if released else end)}
        if end >= 1120.0:
            found["new-task"] = (1120.0, end)
        return found

    async def held(endpoint: str, key: str, request: Any, seconds: float, *, until: Any, opened: Any, **options: Any) -> bool:
        opened()
        for _ in range(int(seconds // s7.TRICKLE_SECONDS)):
            if await until():
                break
            await sleep(s7.TRICKLE_SECONDS)
        released.append(clock[0])
        return True

    monkeypatch.setattr(s7, "slow_stream", held)
    deployed = SimpleNamespace(gateway_tasks=1)
    pool = SimpleNamespace(name="mock", gateway_tasks=tasks)
    run = s7.Run(pool, "http://gateway", [generate_key()], SimpleNamespace(), 1, 300, deployed=deployed)  # ty: ignore[invalid-argument-type] - a double
    assert asyncio.run(run.across_deployment()) is True


def test_wakes_go_on_when_a_lost_one_still_woke_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    capacity, calls = [0], [0]
    pool = SimpleNamespace(name="mock", asked=lambda: capacity[0], gauge=lambda name: capacity[0])

    async def request(*arguments: Any, **options: Any) -> bool:
        capacity[0] = 1  # every request wakes the pool, answered or not
        calls[0] += 1
        return calls[0] == 1

    async def wait(condition: Any, *arguments: Any) -> None:
        if not condition():
            capacity[0] = 0  # the awake pool idles back to zero
        assert condition()

    monkeypatch.setattr(s7, "wake", request)
    monkeypatch.setattr(s7, "_wait", wait)
    settings = SimpleNamespace(hold_seconds=30, idle_minutes=20)
    run = s7.Run(pool, "http://gateway", [generate_key()], settings, 3, 60)  # ty: ignore[invalid-argument-type] - a double
    assert asyncio.run(run.send_wakes()) == (3, 3)
