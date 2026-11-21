"""Spike S4's verdict (`spikes/s4_weights.py`): the median weights stage on recorded timings, and
which launch's timing a start counts. No number here is a measurement; the instance names are made up.
"""

from __future__ import annotations

import pytest

import common
import s4_weights as s4
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


def _stage(instance: str, transition: str, stage: str, samples: list[tuple[float, float, float]]) -> tuple:
    """A stage's value series and its `timestamp()` series, from (step, value, taken) samples."""
    labels = {"instance": instance, "transition": transition, "stage": stage, "pool": "coursework"}
    values = common.Series({"__name__": "rhi_lifecycle_stage_seconds", **labels}, tuple((s, v) for s, v, _ in samples))
    stamps = common.Series(labels, tuple((s, t) for s, _, t in samples))
    return values, stamps


def _launch(*stages: tuple) -> s4.Launch:
    return s4.weights_of_launch([v for v, _ in stages], [t for _, t in stages], WAKE)


def test_the_launch_that_completed_after_the_wake_is_the_one_counted() -> None:
    launch = _launch(
        # The warm instance promoted by the wake: its earlier launch's sample, taken before the wake
        # and still in the query's lookback, does not count; the new one does.
        _stage("instance-a", "promotion", "weights", [(WAKE, 40.0, WAKE - 200), (WAKE + 150, 84.5, WAKE + 140)]),
        _stage("instance-a", "promotion", "hook", [(WAKE + 150, 1.0, WAKE + 140)]),
        # A launch abandoned after its weights loaded: no hook, not counted.
        _stage("instance-b", "cold-launch", "weights", [(WAKE + 90, 60.0, WAKE + 80)]),
    )
    assert launch == s4.Launch("instance-a", "promotion", 84.5)


def test_a_launch_written_only_before_the_wake_is_no_start() -> None:
    with pytest.raises(s4.NoLaunch):
        _launch(
            _stage("instance-a", "promotion", "weights", [(WAKE + 15, 40.0, WAKE - 10)]),
            _stage("instance-a", "promotion", "hook", [(WAKE + 15, 1.0, WAKE - 10)]),
        )


class Gateway:
    def __init__(self, wall: list[float]) -> None:
        self.wall, self.woken, self.ready_after = wall, 0, 0.0

    def state(self) -> str:
        # Ready 120 seconds after the wake, and cold again 10 minutes later.
        ready = self.woken and self.ready_after <= self.wall[0] < self.ready_after + 600
        return "ready" if ready else "cold"

    def wake(self) -> None:
        self.woken += 1
        self.ready_after = self.wall[0] + 120


class Workspace:
    """Each start's launch writes its weights and hook stages 100 seconds after its wake."""

    def __init__(self, gateway: Gateway, weights: list[float]) -> None:
        self.gateway, self.weights = gateway, weights

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if not self.gateway.woken or start == end - s4.QUIET_SECONDS:
            return []
        taken = self.gateway.ready_after - 20
        value = self.weights[self.gateway.woken - 1]
        stages = [
            _stage("instance-a", "promotion", "weights", [(end, value, taken)]),
            _stage("instance-a", "promotion", "hook", [(end, 1.0, taken)]),
        ]
        return [stamps if query.startswith("timestamp(") else values for values, stamps in stages]


def test_a_run_wakes_the_pool_once_per_start_and_reads_each_start_s_weights() -> None:
    wall = [WAKE]

    def sleep(seconds: float) -> None:
        wall[0] += seconds

    gateway = Gateway(wall)
    pool = s4.Pool("coursework", Workspace(gateway, [70.0, 90.0, 85.0]))
    numbers = s4.Run(gateway, pool, 3, "stream", wall=lambda: wall[0], sleep=sleep, log=lambda _: None).measure()
    assert gateway.woken == 3
    assert numbers == {"starts": 3, "weights_seconds_median": 85.0}


def test_the_run_label_is_the_loader_setting() -> None:
    assert s4.run_label({"RHI_S4_LOADER": "download"}) == "download"
    for environ in ({}, {"RHI_S4_LOADER": "Stream"}, {"RHI_S4_LOADER": "stream", "RHI_RUN_LABEL": "other"}):
        with pytest.raises(ValueError):
            s4.run_label(environ)
