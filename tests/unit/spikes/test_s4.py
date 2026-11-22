"""Spike S4's verdict (`spikes/s4_weights.py`): the median weights stage on recorded timings, which
launch's timing a start counts, and how the gateway is reached. No number here is a measurement; the
instance names are made up, and the key is generated.
"""

from __future__ import annotations

import io
import urllib.request
from dataclasses import dataclass
from email.message import Message
from types import SimpleNamespace
from urllib.response import addinfourl

import pytest

import common
import s4_weights as s4
from rhi_common.keys import generate_key
from rhi_reporter.measure import Verdict, measurement_event

WAKE = 1000.0


def test_a_median_of_80_seconds_passes_and_one_of_120_fails() -> None:
    passed = s4.numbers_of([70.0, 80.0, 95.0])
    assert passed == {"starts": 3, "weights_seconds_median": 80.0}
    assert s4.SPIKE.verdict(passed) is Verdict.PASS
    assert s4.SPIKE.verdict(s4.numbers_of([110.0, 120.0, 130.0])) is Verdict.FAIL
    # The numbers are the declared names, in the shape the reporter takes, under the setting's label.
    names = [name.value for name in s4.Number]
    measurement_event("S4", "stream", Verdict.PASS, passed, names=names, commit="a" * 40, bus="bus")


@dataclass(frozen=True)
class Sample:
    """A stage sample of a launch (or, with the stage `release`, its instance's release sample): its
    value, when it was taken, and from when the workspace holds it: its time `available`, and then
    after `after_reads` reads that end there or later."""

    instance: str
    stage: str
    value: float
    taken: float
    available: float = 0.0
    after_reads: int = 0
    transition: str = "promotion"


class Workspace:
    """Evaluates the job's queries on `samples`: at each step, each series' latest sample taken by
    then, its value or (`timestamp()`) its time, and with `> t` only where that time is after t. The
    release metric's queries read the `release` samples, the stage metric's the others."""

    def __init__(self, samples: list[Sample]) -> None:
        self.samples = samples
        self.ends: list[float] = []

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        self.ends.append(end)
        after = float(query.rsplit(">", 1)[1]) if ">" in query else None
        reads = [s for s in self.samples if s.available <= end]
        held = [s for s in reads if s.after_reads < sum(e >= s.available for e in self.ends)]
        series: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
        for index in range(int((end - start) // step) + 1):
            at = start + index * step
            latest: dict[tuple[str, str, str], Sample] = {}
            for one in sorted(held, key=lambda s: s.taken):
                release = query.startswith(s4.RELEASE_METRIC)
                if (
                    one.taken <= at
                    and (one.stage == "release") == release
                    and ("stage=~" not in query or one.stage in query)
                ):
                    latest[(one.instance, one.transition, one.stage)] = one
            for key, one in latest.items():
                if after is None or one.taken > after:
                    value = one.taken if query.startswith("timestamp(") else one.value
                    series.setdefault(key, []).append((at, value))
        return [
            common.Series({"instance": i, "transition": t, "stage": s, "pool": "coursework"}, tuple(points))
            for (i, t, s), points in series.items()
        ]


class Gateway:
    """The pool is cold until woken, then ready `ready_after` seconds later."""

    def __init__(self, wall: list[float], ready_after: float) -> None:
        self.wall, self.ready_after, self.woken_at = wall, ready_after, None

    def state(self) -> str:
        return "ready" if self.woken_at is not None and self.wall[0] >= self.woken_at + self.ready_after else "cold"

    def wake(self) -> None:
        self.woken_at = self.wall[0]


def _one_start(samples: list[Sample], ready_after: float) -> s4.Launch:
    wall = [WAKE]

    def sleep(seconds: float) -> None:
        wall[0] += seconds

    gateway = Gateway(wall, ready_after)
    pool = s4.Pool("coursework", Workspace(samples))
    return s4.Run(gateway, pool, 1, "stream", wall=lambda: wall[0], sleep=sleep, log=lambda _: None).start()


def test_an_earlier_launch_s_weights_are_not_this_start_s_even_when_a_scrape_is_late() -> None:
    # A warm instance's previous launch took 80 s; the promotion the wake brings takes 120 s, and
    # its sample reaches the workspace between the two queries of the first read.
    launch = _one_start(
        [
            Sample("instance-a", "weights", 80.0, WAKE - 100),
            Sample("instance-a", "hook", 1.0, WAKE - 100),
            Sample("instance-a", "weights", 120.0, WAKE + 135, available=WAKE + 150, after_reads=1),
            Sample("instance-a", "hook", 2.0, WAKE + 135, available=WAKE + 150, after_reads=1),
            Sample("instance-a", "release", 1.0, WAKE + 140, available=WAKE + 150, after_reads=1),
        ],
        ready_after=150,
    )
    assert launch == s4.Launch("instance-a", "promotion", 120.0)


def test_a_launch_gone_before_readiness_is_not_the_one_that_became_ready() -> None:
    # A launch whose completion Auto Scaling did not take still has its hook sample, and its instance
    # is gone when the hook times out; the replacement's samples arrive after readiness.
    ready = WAKE + 900
    launch = _one_start(
        [
            Sample("instance-b", "weights", 60.0, WAKE + 120, available=WAKE + 120),
            Sample("instance-b", "hook", 3.0, WAKE + 120, available=WAKE + 120),
            # A launch abandoned after its weights loaded: no hook.
            Sample("instance-d", "weights", 50.0, ready - 10, transition="cold-launch"),
            Sample("instance-c", "weights", 120.0, ready - 15, available=ready + 20),
            Sample("instance-c", "hook", 3.0, ready - 15, available=ready + 20),
            Sample("instance-c", "release", 1.0, ready - 10, available=ready + 20),
        ],
        ready_after=900,
    )
    assert launch == s4.Launch("instance-c", "promotion", 120.0)


def test_a_launch_whose_completion_was_not_taken_is_not_counted_while_its_instance_is_still_scraped() -> None:
    # The agent ends the hook stage whatever Auto Scaling answered, and the instance is still scraped
    # while it waits out the hook; only the replacement, which reached service, writes the release.
    ready = WAKE + 900
    launch = _one_start(
        [
            *(
                Sample("instance-b", stage, value, at)
                for at in (WAKE + 120, ready - 5)
                for stage, value in (("weights", 60.0), ("hook", 3.0))
            ),
            Sample("instance-c", "weights", 120.0, ready - 15, available=ready + 20),
            Sample("instance-c", "hook", 3.0, ready - 15, available=ready + 20),
            Sample("instance-c", "release", 1.0, ready - 10, available=ready + 20),
        ],
        ready_after=900,
    )
    assert launch == s4.Launch("instance-c", "promotion", 120.0)


def test_a_replacement_started_after_readiness_is_not_the_launch_that_became_ready() -> None:
    # The launch that made the pool ready loaded for 120 s, but its samples reach the workspace late;
    # a replacement started after readiness loads for 60 s and its samples arrive first.
    ready = WAKE + 150
    launch = _one_start(
        [
            *(
                Sample("ready-node", stage, value, ready - 5, available=ready + 140)
                for stage, value in (("weights", 120.0), ("hook", 1.0), ("release", 1.0))
            ),
            *(
                Sample("replacement-node", stage, value, ready + 100, available=ready + 100)
                for stage, value in (("weights", 60.0), ("hook", 1.0), ("release", 1.0))
            ),
        ],
        ready_after=150,
    )
    assert launch == s4.Launch("ready-node", "promotion", 120.0)


def test_a_launch_written_only_before_the_wake_is_no_start() -> None:
    samples = [Sample("instance-a", "weights", 40.0, WAKE - 10), Sample("instance-a", "hook", 1.0, WAKE - 10)]
    with pytest.raises(s4.NoLaunch):
        s4.Pool("coursework", Workspace(samples)).launch(WAKE, WAKE + 15, WAKE + 30)


def test_a_run_wakes_the_pool_once_per_start_and_reads_each_start_s_weights() -> None:
    wall = [WAKE]
    samples: list[Sample] = []
    weights = iter([70.0, 90.0, 85.0])

    class Cycling(Gateway):
        # Cold again 10 minutes after ready; each wake's launch writes its stages 100 s after it.
        def state(self) -> str:
            if self.woken_at is not None and self.wall[0] >= self.woken_at + 720:
                self.woken_at = None
            return super().state()

        def wake(self) -> None:
            super().wake()
            at = self.wall[0] + 100
            samples.extend(
                [
                    Sample("instance-a", stage, value, at)
                    for stage, value in (("weights", next(weights)), ("hook", 1.0), ("release", 1.0))
                ]
            )

    def sleep(seconds: float) -> None:
        wall[0] += seconds

    gateway = Cycling(wall, 120)
    pool = s4.Pool("coursework", Workspace(samples))
    numbers = s4.Run(gateway, pool, 3, "stream", wall=lambda: wall[0], sleep=sleep, log=lambda _: None).measure()
    assert numbers == {"starts": 3, "weights_seconds_median": 85.0}


def test_the_gateway_stack_s_host_name_reaches_the_gateway_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        calls.append((request.get_method(), request.full_url))
        return io.BytesIO(b'{"pools":[{"model":"coursework","state":"cold"}]}')

    def measure(run: s4.Run) -> dict[str, int | float]:
        assert run.gateway.state() == "cold"
        run.gateway.wake()
        return s4.numbers_of([80.0])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(s4.Run, "measure", measure)
    monkeypatch.setattr(s4.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(common, "canary_keys", lambda: SimpleNamespace(students=(generate_key(),)))
    monkeypatch.setattr(common, "run_spike", lambda spike, job, measure, **_: spike.verdict(measure()))
    environ = {"AWS_REGION": "us-east-1", "RHI_SPIKE_WORKSPACE_URL": "https://example.com/workspace"}
    environ |= {"RHI_SPIKE_ENDPOINT": "example.com", "RHI_S4_LOADER": "stream", "RHI_S4_STARTS": "1"}
    assert s4.main(environ) is Verdict.PASS
    assert calls == [("GET", "https://example.com/status"), ("POST", "https://example.com/wake")]


def test_the_canary_key_is_never_sent_over_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(*_: object, **__: object) -> None:
        raise AssertionError("a request was sent")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    for endpoint in ("http://example.com", "ftp://example.com", ""):
        with pytest.raises(ValueError):
            s4.Gateway(endpoint, "coursework", generate_key()).state()


def test_the_canary_key_does_not_follow_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []

    def transport(_: object, __: object, request: urllib.request.Request, **___: object) -> addinfourl:
        sent.append((request.full_url, request.has_header("Authorization")))
        headers, body = Message(), b'{"pools":[{"model":"coursework","state":"cold"}]}'
        if request.type == "https":
            headers["Location"] = "http://example.com/status"
            response = addinfourl(io.BytesIO(), headers, request.full_url, 302)
        else:
            response = addinfourl(io.BytesIO(body), headers, request.full_url, 200)
        # urllib reads the reason of a real HTTP answer from `msg`, which the stub of addinfourl leaves out.
        response.msg = ""  # ty: ignore[missing-attribute]
        return response

    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", transport)
    s4.Gateway("example.com", "coursework", generate_key()).state()
    assert sent == [("https://example.com/status", True), ("http://example.com/status", False)]


def test_the_run_label_is_the_loader_setting() -> None:
    assert s4.run_label({"RHI_S4_LOADER": "download"}) == "download"
    for environ in ({}, {"RHI_S4_LOADER": "Stream"}, {"RHI_S4_LOADER": "stream", "RHI_RUN_LABEL": "other"}):
        with pytest.raises(ValueError):
            s4.run_label(environ)
