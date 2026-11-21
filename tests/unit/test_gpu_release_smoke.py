"""The GPU release smoke test (tests/smoke/deployed/test_gpu_release.py) takes as served only what
the pool's lifecycle agents reported after the request: a report from before it, of a stopped or a
replaced instance, neither passes nor fails it, and a wrong release reported after it fails it,
however soon its series ages out or is marked stale. A fake workspace stands in for Prometheus,
answering `count_over_time` over the raw samples that have arrived, and a fake clock for time: no
account, no network.
"""

from __future__ import annotations

import re
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
# The hold while the pool wakes: the stream ends this long after the request.
HOLD = 60.0
QUERY = re.compile(r'count_over_time\(rhi_lifecycle_release_info\{pool="coursework"\}\[(\d+)ms\]\)')

# One report: (instance, release, taken, arrived), in seconds from the request.
Sample = tuple[str, tuple[str, str], float, float]


class Clock:
    """Time that passes only when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    monotonic = time


def run_against_samples(monkeypatch: pytest.MonkeyPatch, samples: Sequence[Sample]) -> None:
    """The smoke test against a coursework pool whose lifecycle agents made `samples`. Like
    Prometheus, the workspace answers `count_over_time` of the metric over a window ending at the
    query's `time` with the series that have a sample taken in that window, among the samples that
    have arrived; a stale marker is no sample."""
    with warnings.catch_warnings():  # the smoke tests' mark is registered by their own conftest
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
        match = QUERY.fullmatch(form["query"][0])
        assert match, f"the workspace cannot answer {form['query'][0]}"
        evaluated = float(form["time"][0]) if "time" in form else clock.now
        start = evaluated - int(match[1]) / 1000
        found = {
            (instance, served)
            for instance, served, taken, arrived in samples
            if start < sent + taken <= evaluated and sent + arrived <= clock.now
        }
        result = [
            {
                "metric": {
                    "pool": "coursework",
                    "instance": instance,
                    "image_digest": served[0],
                    "model_revision": served[1],
                },
                "value": [evaluated, "1"],
            }
            for instance, served in sorted(found)
        ]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})

    class Stack:
        pool = "coursework"

        def stream(self, path: str, body: dict[str, Any]) -> Any:
            assert path == "/v1/messages" and body["model"] == self.pool and body["stream"]
            clock.sleep(HOLD)
            return SimpleNamespace(status=200, events=(SseEvent(name="message_stop", data="{}", comment=False),))

    session = SimpleNamespace(get_credentials=lambda: None, region_name="us-east-1")
    http = httpx.Client(transport=httpx.MockTransport(answer))
    check(
        stack=Stack(),
        workspace=workspace_type(url="https://workspace.example.com", http=http, session=session),
        deployed_release=RELEASE,
    )


def every_scrape(instance: str, served: tuple[str, str], *, lag: float = 0.0, start: float = HOLD + 15) -> list[Sample]:
    """`instance` reporting `served` on every scrape from `start`, each arriving `lag` later."""
    return [(instance, served, float(taken), taken + lag) for taken in range(int(start), 1500, 15)]


# The stopped instance's last report of the deployed release, before the wake.
STOPPED = [("stopped", RELEASE, -30.0, -30.0)]


def test_a_woken_instance_serving_another_release_fails_despite_a_stopped_instance_s_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AssertionError, match="another release"):
        run_against_samples(monkeypatch, [*STOPPED, *every_scrape("woken", OTHER)])


def test_a_woken_instance_serving_the_deployed_release_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    run_against_samples(monkeypatch, [*STOPPED, *every_scrape("woken", RELEASE)])


def test_a_wrong_release_reported_after_the_wake_fails_once_its_series_has_aged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance that served the stream reported another release before the stream ended and
    twice after it, then stopped reporting, while a partner reports the deployed one."""
    woken = [("woken", OTHER, HOLD + offset, HOLD + offset) for offset in (-10.0, 5.0, 20.0)]
    with pytest.raises(AssertionError, match="another release"):
        run_against_samples(monkeypatch, [*STOPPED, *woken, *every_scrape("partner", RELEASE)])


def test_a_wrong_release_reported_just_after_the_request_fails_however_soon_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A series visible before the request reports another release 1 second after it, and never again."""
    samples = [("server", OTHER, -5.0, -5.0), ("server", OTHER, 1.0, 1.0), *every_scrape("partner", RELEASE)]
    with pytest.raises(AssertionError, match="another release"):
        run_against_samples(monkeypatch, samples)


def test_a_wrong_release_reported_after_the_request_fails_though_its_series_is_marked_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A series visible before the request reports another release 65 seconds after it, and is
    marked stale at 80 seconds: an instant query would no longer return it, the samples remain."""
    samples = [("server", OTHER, -5.0, -5.0), ("server", OTHER, 65.0, 65.0), *every_scrape("partner", RELEASE)]
    with pytest.raises(AssertionError, match="another release"):
        run_against_samples(monkeypatch, samples)


def test_a_report_taken_before_the_request_does_not_count_even_when_it_arrives_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced instance's report from before the request reaches the workspace after the wake."""
    samples = [("replaced", OTHER, -1.0, 200.0), *every_scrape("woken", RELEASE, lag=20.0)]
    run_against_samples(monkeypatch, samples)


def test_an_instance_stopped_well_before_the_request_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """An instance of another release reported last 220 seconds before the request, so that an
    instant query returns its series until 80 seconds after it, as with the stale series above."""
    stopped = [("stopped", OTHER, float(taken), float(taken)) for taken in range(-400, -219, 15)]
    run_against_samples(monkeypatch, [*stopped, *every_scrape("woken", RELEASE)])


def test_no_report_of_the_deployed_release_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError, match="no lifecycle agent"):
        run_against_samples(monkeypatch, STOPPED)
