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
