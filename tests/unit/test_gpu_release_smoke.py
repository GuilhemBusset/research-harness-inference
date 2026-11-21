"""The GPU release smoke test (tests/smoke/deployed/test_gpu_release.py) takes as served only a
release reported after the wake: a stopped instance's report from before it stopped, still
returned by an instant query, cannot pass it. A fake workspace stands in for Prometheus, keeping a
series for 5 minutes after its last sample, and a fake clock for time: no account, no network.
"""

from __future__ import annotations

import runpy
import warnings
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


def run_check(monkeypatch: pytest.MonkeyPatch, served: tuple[str, str]) -> None:
    """The smoke test against a coursework pool woken from cold, whose stopped instance reported
    the deployed release before it stopped and whose woken instance reports `served`."""
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
        result = []
        if clock.now - stopped_at <= STALE:
            result.append({"instance": "stopped", "digest": RELEASE})
        if woken_at and clock.now >= woken_at[0] + SCRAPE:
            result.append({"instance": "woken", "digest": served})
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
