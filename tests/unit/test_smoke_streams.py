"""The smoke tests' streams (`Stack.stream`, tests/smoke/conftest.py) on a gateway whose tasks do
not read a pool ready at the same moment: a stream answered 503 with `warming` is asked again and
completes once every task has read the pool, a pool that stays warming still fails its test, and no
other answer is asked again. A transport stands in for the endpoint and a clock that only its
`sleep` moves for time: no stack, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]


def _smoke_stack() -> Any:
    """The module of the smoke tests' stack, which is in no folder of the import path."""
    spec = importlib.util.spec_from_file_location("smoke_streams", REPO / "tests" / "smoke" / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while it is run.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _smoke_stack()

STREAM = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def refusal(cause: str) -> httpx.Response:
    """What the gateway answers for a pool that cannot serve, in the Messages format."""
    body = {"type": "error", "error": {"type": cause, "message": "The pool cannot serve this request now."}}
    return httpx.Response(503, json=body, headers={"Retry-After": "30"})


class Endpoint:
    """A gateway of two tasks: a request made less than `lag` seconds after the first is answered by
    the task that has not read the pool ready yet, with `cause`; any later one gets its stream."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, lag: float, cause: str = "warming") -> None:
        self.lag, self.cause, self.now = lag, cause, 1000.0
        self.asked_at: list[float] = []
        clock = SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep)
        monkeypatch.setattr(smoke, "time", clock)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def answer(self, request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        self.asked_at.append(self.now)
        if self.now - self.asked_at[0] < self.lag:
            return refusal(self.cause)
        return httpx.Response(200, content=STREAM, headers={"Content-Type": "text/event-stream"})

    def stream(self) -> Any:
        with httpx.Client(transport=httpx.MockTransport(self.answer)) as http:
            stack = smoke.Stack(
                endpoint="https://smoke.example.com", key="a-key", pool="mock", deployed=True, http=http
            )
            return stack.stream("/v1/messages", {"model": "mock", "stream": True})


def test_a_stream_a_lagging_task_answers_warming_completes_once_that_task_reads_the_pool_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One read of the gateway's, 5 seconds, is the most two tasks are apart.
    endpoint = Endpoint(monkeypatch, lag=5.0)

    stream = endpoint.stream()

    # The answer is the whole stream, its last event and its usage: what a stream test asserts.
    assert stream.status == 200
    assert [event.name for event in stream.events] == ["message_start", "message_delta", "message_stop"]
    assert len(endpoint.asked_at) > 1 and endpoint.asked_at[-1] - endpoint.asked_at[0] >= 5.0


def test_a_pool_that_stays_warming_fails_its_stream_after_the_moment_two_tasks_may_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = Endpoint(monkeypatch, lag=3600.0)

    stream = endpoint.stream()

    assert (stream.status, stream.events) == (503, ())
    assert endpoint.asked_at[-1] - endpoint.asked_at[0] <= smoke.SETTLE_SECONDS


@pytest.mark.parametrize("cause", ["capacity_unavailable", "no_session", "concurrency_limit"])
def test_an_answer_that_is_not_warming_is_not_asked_again(monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    endpoint = Endpoint(monkeypatch, lag=5.0, cause=cause)

    assert endpoint.stream().status == 503
    assert len(endpoint.asked_at) == 1
