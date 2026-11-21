"""The GPU release smoke test (tests/smoke/deployed/test_gpu_release.py) takes as served only a
release reported after the wake: a stopped instance's report from before it stopped, still
returned by an instant query, cannot pass it, and a wrong release reported after the wake fails it
even once its series has aged out. A fake workspace stands in for Prometheus, returning each
series' latest sample for 5 minutes after it was taken, and a fake clock for time: no account, no
network.
"""

from __future__ import annotations

import math
import runpy
import warnings
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from rhi_gateway.sse import SseEvent

DEPLOYED = Path(__file__).resolve().parents[1] / "smoke" / "deployed"
RELEASE = ("sha256:" + "a" * 64, "b" * 40)
OTHER = ("sha256:" + "c" * 64, "b" * 40)
# How long an instant query returns a series after its last sample, and between two scrapes.
STALE, SCRAPE = 300.0, 15.0


class Clock:
    """Time that passes only when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    monotonic = time


def run_check(
    monkeypatch: pytest.MonkeyPatch,
    served: tuple[str, str],
    *,
    woken_scrapes: Sequence[float] | None = None,
    partner: bool = False,
) -> None:
    """The smoke test against a coursework pool woken from cold, whose stopped instance reported
    the deployed release before it stopped and whose woken instance reports `served`: on every
    scrape from one after the wake, or at `woken_scrapes`, seconds from the wake, on one series.
    A `partner` instance reports the deployed release on every scrape from one after the wake."""
    with warnings.catch_warnings():  # the smoke tests' mark is registered by their own conftest
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
        smoke = runpy.run_path(str(DEPLOYED / "test_gpu_release.py"))
    check = smoke["test_the_woken_pool_completes_a_stream_and_serves_the_deployed_release"]
    workspace_type = runpy.run_path(str(DEPLOYED / "conftest.py"))["Workspace"]
    clock = Clock()
    for namespace in (check.__globals__, workspace_type.series.__globals__):
        monkeypatch.setitem(namespace, "time", clock)
    monkeypatch.setitem(
        workspace_type.series.__globals__, "SigV4Auth", lambda *_: SimpleNamespace(add_auth=lambda _: None)
    )
    woken_at: list[float] = []
    # The stopped instance's last sample, taken before the wake.
    stopped_at = clock.now - 2 * SCRAPE

    def answer(request: httpx.Request) -> httpx.Response:
        (query,) = parse_qs(request.content.decode())["query"]
        assert query == 'rhi_lifecycle_release_info{pool="coursework"}'
        # Each series' latest sample: the series is returned while it is at most STALE old.
        latest = {("stopped", RELEASE): stopped_at}
        if woken_at:
            woken = woken_at[0]
            every = woken + SCRAPE * math.floor((clock.now - woken) / SCRAPE)
            if woken_scrapes is None:
                latest[("woken", served)] = every if every >= woken + SCRAPE else -math.inf
            else:
                taken = [woken + offset for offset in woken_scrapes if woken + offset <= clock.now]
                latest[("woken", served)] = max(taken, default=-math.inf)
            if partner:
                latest[("partner", RELEASE)] = every if every >= woken + SCRAPE else -math.inf
        result = [
            {"instance": instance, "digest": digest}
            for (instance, digest), sampled in latest.items()
            if clock.now - sampled <= STALE
        ]
        series = [
            {
                "metric": {
                    "pool": "coursework",
                    "instance": one["instance"],
                    "image_digest": one["digest"][0],
                    "model_revision": one["digest"][1],
                },
                "value": [clock.now, "1"],
            }
            for one in result
        ]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": series}})

    class Stack:
        pool = "coursework"

        def stream(self, path: str, body: dict[str, Any]) -> Any:
            assert path == "/v1/messages" and body["model"] == self.pool and body["stream"]
            clock.sleep(60.0)  # the hold while the pool wakes
            woken_at.append(clock.now)
            return SimpleNamespace(status=200, events=(SseEvent(name="message_stop", data="{}", comment=False),))

    session = SimpleNamespace(get_credentials=lambda: None, region_name="us-east-1")
    http = httpx.Client(transport=httpx.MockTransport(answer))
    workspace = workspace_type(url="https://workspace.example.com", http=http, session=session)
    check(stack=Stack(), workspace=workspace, deployed_release=RELEASE)


def test_a_woken_instance_serving_another_release_fails_despite_a_stopped_instance_s_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AssertionError, match="another release"):
        run_check(monkeypatch, served=OTHER)


def test_a_woken_instance_serving_the_deployed_release_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    run_check(monkeypatch, served=RELEASE)


def test_a_wrong_release_reported_after_the_wake_fails_once_its_series_has_aged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance that served the stream reported another release before it stopped and twice
    after the wake, then stopped reporting, while a partner reports the deployed one."""
    with pytest.raises(AssertionError, match="another release"):
        run_check(monkeypatch, served=OTHER, woken_scrapes=(-10.0, 5.0, 20.0), partner=True)


def run_against_samples(
    monkeypatch: pytest.MonkeyPatch, samples: Sequence[tuple[str, tuple[str, str], float, float]]
) -> None:
    """The smoke test against a workspace that, like Prometheus, evaluates a query at its `time`:
    it returns each series whose latest sample at or before that time, among those that have
    arrived, is at most STALE old. Each sample is (instance, release, taken, arrived), in seconds
    from the request, which the pool completes after a hold of 60 seconds."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
        smoke = runpy.run_path(str(DEPLOYED / "test_gpu_release.py"))
    check = smoke["test_the_woken_pool_completes_a_stream_and_serves_the_deployed_release"]
    workspace_type = runpy.run_path(str(DEPLOYED / "conftest.py"))["Workspace"]
    clock = Clock()
    sent = clock.now
    for namespace in (check.__globals__, workspace_type.series.__globals__):
        monkeypatch.setitem(namespace, "time", clock)
    monkeypatch.setitem(
        workspace_type.series.__globals__, "SigV4Auth", lambda *_: SimpleNamespace(add_auth=lambda _: None)
    )

    def answer(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        evaluated = float(form["time"][0]) if "time" in form else clock.now
        latest: dict[tuple[str, tuple[str, str]], float] = {}
        for instance, served, taken, arrived in samples:
            if sent + taken <= evaluated and sent + arrived <= clock.now:
                latest[(instance, served)] = max(latest.get((instance, served), -math.inf), sent + taken)
        result = [
            {
                "metric": {"instance": instance, "image_digest": served[0], "model_revision": served[1]},
                "value": [evaluated, "1"],
            }
            for (instance, served), taken in latest.items()
            if evaluated - taken <= STALE
        ]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})

    class Stack:
        pool = "coursework"

        def stream(self, path: str, body: dict[str, Any]) -> Any:
            clock.sleep(60.0)
            return SimpleNamespace(status=200, events=(SseEvent(name="message_stop", data="{}", comment=False),))

    session = SimpleNamespace(get_credentials=lambda: None, region_name="us-east-1")
    http = httpx.Client(transport=httpx.MockTransport(answer))
    check(
        stack=Stack(),
        workspace=workspace_type(url="https://workspace.example.com", http=http, session=session),
        deployed_release=RELEASE,
    )


def every_scrape(
    instance: str, served: tuple[str, str], *, lag: float = 0.0
) -> list[tuple[str, tuple[str, str], float, float]]:
    """`instance` reporting `served` on every scrape from the stream's end, each arriving `lag` later."""
    return [(instance, served, taken, taken + lag) for taken in range(75, 1500, 15)]


def test_a_wrong_release_reported_just_after_the_request_fails_however_soon_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A series visible before the request reports another release 1 second after it, and never again."""
    samples = [("server", OTHER, -5.0, -5.0), ("server", OTHER, 1.0, 1.0), *every_scrape("partner", RELEASE)]
    with pytest.raises(AssertionError, match="another release"):
        run_against_samples(monkeypatch, samples)


def test_a_report_taken_before_the_request_does_not_count_even_when_it_arrives_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced instance's report from before the request reaches the workspace after the wake."""
    samples = [("replaced", OTHER, -1.0, 200.0), *every_scrape("woken", RELEASE, lag=20.0)]
    run_against_samples(monkeypatch, samples)
