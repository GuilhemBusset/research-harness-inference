"""The smoke tests' streams (`Stack.stream`, tests/smoke/conftest.py) on a gateway whose tasks do
not read a pool at the same moment. One task reads the pool ready, which starts the booked run; the
other still holds an older view, and answers 503 from it with one of the three causes of a pool that
cannot serve. The stream test passes once that task has read the pool, a pool that stays so still
fails it, and no other answer is asked again.

The lagging task's answer is the gateway's own: the rule on its view (`PoolView.refusal`), rendered
as the gateway renders it. A transport stands in for the endpoint and a clock that only its `sleep`
moves for time: no stack, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from rhi_common.causes import Cause
from rhi_common.records import ScalerNote, Session
from rhi_gateway.errors import WireFormat, render_json
from rhi_gateway.readiness import PoolView

REPO = Path(__file__).resolve().parents[2]


def _smoke(name: str, file: str) -> Any:
    """A module of the smoke tests' folder, which is in no folder of the import path."""
    spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "smoke" / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while it is run.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _smoke("smoke_streams", "conftest.py")
with warnings.catch_warnings():
    # The marker `smoke_id` is registered by the smoke tests' own run, which this one is not.
    warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
    smoke_tests = _smoke("smoke_stream_tests", "test_smoke.py")

NOW = 1_800_000_000
SESSION_MAX_HOURS = 4
STREAM = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)

# The booking, and the scaler's note before and after the pool's instance is in service.
SESSION = Session("mock", 1, NOW - 120, NOW + 3600, "verify", NOW - 120)
STARTING = ScalerNote("mock", capacity_asked=1, no_capacity_at=None, written_at=NOW - 60, requested=1, starting=1)
IN_SERVICE = replace(STARTING, written_at=NOW, starting=0, in_service=1)
# What the task that reads the pool ready holds, and what one that is a read behind may hold.
READY = PoolView(ready=True, note=IN_SERVICE, observed_at=NOW, session=SESSION)
LAGGING = {
    # Neither the healthy target nor the newer note.
    Cause.WARMING: replace(READY, ready=False, note=STARTING),
    # The health read before the target was healthy, the note after the scaler counted it in service.
    Cause.CAPACITY_UNAVAILABLE: replace(READY, ready=False),
    # A pool whose target was healthy when it was booked: the session is not read yet.
    Cause.NO_SESSION: replace(READY, session=None),
}


class Endpoint:
    """A gateway of two tasks: a request made less than `lag` seconds after the first is answered by
    the task that is behind, from the view `behind` or with the cause given for it; any later one by
    the task that reads the pool ready."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, lag: float, behind: PoolView | Cause) -> None:
        self.lag, self.behind, self.now = lag, behind, float(NOW)
        self.asked_at: list[float] = []
        self.refused: list[tuple[int, str]] = []
        clock = SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep)
        monkeypatch.setattr(smoke, "time", clock)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def cause(self) -> Cause | None:
        """Why the task that answers now does not serve the request; `None` when it does."""
        behind = self.behind if self.now - self.asked_at[0] < self.lag else READY
        if isinstance(behind, Cause):
            return behind
        return behind.refusal(self.now, session_max_hours=SESSION_MAX_HOURS)

    def answer(self, request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        self.asked_at.append(self.now)
        cause = self.cause()
        if cause is None:
            return httpx.Response(200, content=STREAM, headers={"Content-Type": "text/event-stream"})
        refusal = render_json(cause, WireFormat.MESSAGES)
        self.refused.append((refusal.status, cause.value))
        return httpx.Response(refusal.status, content=refusal.body, headers=dict(refusal.headers))

    def run_the_stream_test(self) -> None:
        """The smoke tests' stream test on the Messages route, on this endpoint as a deployed stack
        whose pool the verification booked."""
        with httpx.Client(transport=httpx.MockTransport(self.answer)) as http:
            stack = smoke.Stack(
                endpoint="https://smoke.example.com", key="a-key", pool="mock", deployed=True, http=http
            )
            smoke_tests.test_a_stream_completes_with_its_usage(
                stack, "/v1/messages", smoke_tests.messages_body, smoke_tests.messages_usage
            )


@pytest.mark.parametrize("cause", list(LAGGING))
def test_the_stream_test_passes_once_a_task_that_is_a_read_behind_has_read_the_pool(
    monkeypatch: pytest.MonkeyPatch, cause: Cause
) -> None:
    # One read of the gateway's, 5 seconds, is the most two tasks are apart.
    endpoint = Endpoint(monkeypatch, lag=5.0, behind=LAGGING[cause])

    # The whole stream, its last event and its usage: the test asserts them on the answer it ends on.
    endpoint.run_the_stream_test()

    # The task that was behind did refuse, from its view, and the stream came after its next read.
    assert set(endpoint.refused) == {(503, cause.value)}
    assert endpoint.asked_at[-1] - endpoint.asked_at[0] >= 5.0


@pytest.mark.parametrize("cause", list(LAGGING))
def test_a_pool_that_still_cannot_serve_after_the_moment_two_tasks_may_differ_fails_the_stream_test(
    monkeypatch: pytest.MonkeyPatch, cause: Cause
) -> None:
    endpoint = Endpoint(monkeypatch, lag=3600.0, behind=LAGGING[cause])

    with pytest.raises(AssertionError):
        endpoint.run_the_stream_test()

    assert len(endpoint.asked_at) > 1
    assert endpoint.asked_at[-1] - endpoint.asked_at[0] <= smoke.SETTLE_SECONDS


@pytest.mark.parametrize("cause", [Cause.CONCURRENCY_LIMIT, Cause.ENGINE_ERROR, Cause.TIME_LIMIT, Cause.INVALID_KEY])
def test_an_answer_that_is_not_of_a_pool_that_cannot_serve_is_not_asked_again(
    monkeypatch: pytest.MonkeyPatch, cause: Cause
) -> None:
    endpoint = Endpoint(monkeypatch, lag=5.0, behind=cause)

    with pytest.raises(AssertionError):
        endpoint.run_the_stream_test()

    assert len(endpoint.asked_at) == 1
