"""Spike S1's numbers and verdict (`spikes/s1_cold_start.py`) on recorded timings: which launch a
start is, which starts count for a run of one kind, and why a failed one failed. No number here is
a measurement; the instance names are made up.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import common
import s1_cold_start as s1
from rhi_reporter.measure import Verdict, measurement_event

WAKE = 1000.0
STAGES = {"boot": 40.0, "image": 2.0, "release-check": 1.0, "container-start": 30.0, "weights": 70.0, "warm-up": 20.0}


def launch(transition: str = "promotion", *, served: bool = True, reached: str = "hook") -> s1.Launch:
    """A launch into service with the stages of `STAGES` up to `reached`."""
    order = [*STAGES, "hook"]
    stages = {stage: STAGES.get(stage, 5.0) for stage in order[: order.index(reached) + 1]}
    return s1.Launch("instance-a", transition, "g7e.2xlarge", stages, frozenset(stages), served, WAKE, WAKE, WAKE)


def warm_starts(seconds: list[float]) -> list[s1.Start]:
    return [s1.Start(one, (launch(),), launch()) for one in seconds]


def test_warm_starts_at_4_minutes_at_the_90th_percentile_pass_and_at_6_fail() -> None:
    passed = s1.numbers_of(warm_starts([180.0] * 17 + [240.0] * 3), s1.Kind.WARM)
    assert passed[s1.Number.START_SECONDS_P90] == 240.0
    assert passed[s1.Number.WEIGHTS_SECONDS_MEDIAN] == 70.0
    assert passed[s1.Number.HOOK_SECONDS_P90] == 5.0
    assert passed[s1.Number.STARTS_COUNTED] == passed[s1.Number.WARM_STARTS_IMAGE_ZERO] == 20
    assert s1.SPIKES[s1.Kind.WARM].verdict(passed) is Verdict.PASS
    failed = s1.numbers_of(warm_starts([180.0] * 17 + [360.0] * 3), s1.Kind.WARM)
    assert s1.SPIKES[s1.Kind.WARM].verdict(failed) is Verdict.FAIL
    # The numbers are the declared names, in the shape the reporter takes.
    names = [name.value for name in s1.Number]
    measurement_event("S1", "warm", Verdict.PASS, passed, names=names, commit="a" * 40, bus="bus")


def test_fewer_than_20_warm_starts_give_no_verdict_and_still_post_their_numbers() -> None:
    sent: list[dict] = []
    events = SimpleNamespace(put_events=lambda Entries: sent.extend(Entries) or {"FailedEntryCount": 0})
    environ = {"RHI_MEASUREMENT_BUS": "bus", "CODEBUILD_RESOLVED_SOURCE_VERSION": "a" * 40, "RHI_RUN_LABEL": "warm"}
    numbers = s1.numbers_of(warm_starts([180.0] * 19), s1.Kind.WARM)
    verdict = common.run_spike(s1.SPIKES[s1.Kind.WARM], s1.JOB, lambda: numbers, environ=environ, events=events)
    assert verdict is Verdict.NOT_MEASURED
    detail = json.loads(sent[0]["Detail"])
    assert detail["verdict"] == "error"
    assert detail["numbers"]["starts_counted"] == 19


def test_a_cold_start_in_a_warm_run_is_not_counted() -> None:
    starts = [*warm_starts([200.0] * 20), s1.Start(600.0, (launch("cold-launch"),), launch("cold-launch"))]
    numbers = s1.numbers_of(starts, s1.Kind.WARM)
    assert numbers[s1.Number.STARTS_RUN] == 21
    assert numbers[s1.Number.STARTS_COUNTED] == 20
    assert numbers[s1.Number.STARTS_FAILED] == 0
    assert numbers[s1.Number.START_SECONDS_P90] == 200.0
    # In a cold run, it is the one that counts.
    assert s1.numbers_of(starts, s1.Kind.COLD)[s1.Number.STARTS_COUNTED] == 1


def test_failed_starts_are_counted_by_the_last_stage_reached_and_a_replacement_is_not_timed() -> None:
    starts = [
        # A warm instance abandoned while its engine loaded, replaced by a cold launch that served.
        s1.Start(700.0, (launch(served=False, reached="weights"), launch("cold-launch")), launch("cold-launch")),
        # A warm instance that never started: the gateway said no capacity, then a replacement served.
        s1.Start(650.0, (launch("cold-launch"),), launch("cold-launch"), capacity=True),
        # Not ready in time, and nothing ever wrote a stage.
        s1.Start(None),
        *warm_starts([200.0] * 2),
    ]
    numbers = s1.numbers_of(starts, s1.Kind.WARM)
    assert numbers[s1.Number.STARTS_FAILED] == 3
    assert numbers[s1.Number.STARTS_FAILED_WEIGHTS] == 1
    assert numbers[s1.Number.STARTS_FAILED_CAPACITY] == 1
    assert numbers[s1.Number.STARTS_FAILED_UNKNOWN] == 1
    assert numbers[s1.Number.STARTS_COUNTED] == 2
    assert numbers[s1.Number.START_SECONDS_P90] == 200.0


def _series(instance: str, transition: str, stages: dict[str, float], first: float) -> tuple[list, list]:
    """The value and timestamp series of a launch whose stages end one after the other from `first`."""
    values, stamps, at = [], [], first
    for stage, seconds in stages.items():
        at += seconds
        labels = {"instance": instance, "transition": transition, "stage": stage, "instance_type": "g7e.2xlarge"}
        values.append(common.Series(labels, ((at, seconds),)))
        stamps.append(common.Series(labels, ((at, at), (at + 15, at + 15))))
    return values, stamps


def test_a_launch_is_read_under_its_settled_transition_and_warm_pool_entries_are_not_starts() -> None:
    # A warm-pool refill enters first; the launch into service is exposed as a promotion until the
    # host says the disk has no image, then as a cold launch.
    entry = _series("instance-x", "entry", {"boot": 40.0, "image": 300.0, "release-check": 1.0, "hook": 1.0}, WAKE)
    early = _series("instance-a", "promotion", {"boot": 45.0}, WAKE + 10)
    cold = _series("instance-a", "cold-launch", {**STAGES, "image": 240.0, "hook": 8.0}, WAKE + 10)
    old = common.Series(
        {"instance": "instance-b", "transition": "promotion", "stage": "weights"}, ((WAKE - 300, WAKE - 300),)
    )
    serving = [common.Series({"instance": "instance-a"}, ((WAKE + 500, 1.0),))]
    values = entry[0] + early[0] + cold[0]
    stamps = [*entry[1], *early[1], *cold[1], old]
    (only,) = s1.launches_of(values, stamps, serving, WAKE)
    assert (only.instance, only.transition, only.served) == ("instance-a", "cold-launch", True)
    assert only.stages["image"] == 240.0
    assert s1.ready_launch([only], ready_at=only.last_seen) == only
    # Without its release sample it did not reach service: a failed launch, stopped at its hook.
    (unserved,) = s1.launches_of(values, stamps, [], WAKE)
    assert not unserved.served
    assert unserved.last_stage == "hook"
    assert s1.outcome(s1.Start(None, (unserved,)), s1.Kind.COLD) == (s1.Outcome.FAILED, "hook")


class Gateway:
    """Cold until woken, then ready `ready_after` seconds after the first wake."""

    def __init__(self, wall: list[float], ready_after: float) -> None:
        self.wall, self.ready_after, self.wakes = wall, ready_after, []

    def state(self) -> str:
        return self.status()[0]

    def status(self) -> tuple[str, str]:
        if self.wakes and self.wall[0] >= self.wakes[0] + self.ready_after:
            return "ready", ""
        return ("warming" if self.wakes else "cold"), ""

    def wake(self) -> None:
        self.wakes.append(self.wall[0])


def test_a_start_is_timed_from_the_wake_to_ready_with_the_wake_repeated_meanwhile() -> None:
    wall = [WAKE]

    def sleep(seconds: float) -> None:
        wall[0] += seconds

    gateway = Gateway(wall, ready_after=230)
    # Sampled until readiness, its weights loaded before it.
    served = replace(launch(), last_seen=WAKE + 225, weights_seen=WAKE + 200)
    pool = SimpleNamespace(quiet=lambda _now: True, launches=lambda _woke, _now: [served])
    run = s1.Run(gateway, pool, 1, s1.Kind.WARM, wall=lambda: wall[0], sleep=sleep, log=lambda _: None)
    start = run.start()
    assert start.seconds == 230.0
    assert start.ready == served
    assert gateway.wakes == [WAKE, WAKE + 60, WAKE + 120, WAKE + 180]


def test_the_kind_is_the_run_label_and_20_starts_are_the_default() -> None:
    assert s1.kind_of({"RHI_S1_KIND": "cold"}) is s1.Kind.COLD
    assert s1.starts_of({}) == 20
    with pytest.raises(ValueError, match="run label"):
        s1.kind_of({"RHI_S1_KIND": "warm", "RHI_RUN_LABEL": "cold"})
    with pytest.raises(ValueError, match="kind of start"):
        s1.kind_of({})


def test_a_promotion_abandoned_in_its_image_stage_fails_there_and_is_no_evidence_of_the_image() -> None:
    stopped = launch(served=False, reached="image")
    numbers = s1.numbers_of([s1.Start(None, (stopped,)), *warm_starts([200.0] * 2)], s1.Kind.WARM)
    assert numbers[s1.Number.STARTS_FAILED] == numbers[s1.Number.STARTS_FAILED_IMAGE] == 1
    assert numbers[s1.Number.WARM_STARTS_IMAGE_ZERO] == 2


def test_a_cold_run_carries_the_verdict_of_the_records_warm_runs_whatever_its_starts(tmp_path) -> None:
    front = 'id = "S1"\npass_condition = "Warm start under 5 min at the 90th percentile over 20 starts"\n'
    warm = '[[runs]]\nrun = "warm-3"\ndate = 2026-01-15\ncommit = "' + "a" * 40 + '"\nverdict = "pass"\n'
    pending, passed = tmp_path / "pending.md", tmp_path / "passed.md"
    pending.write_text(f'+++\n{front}verdict = "pending"\n+++\n', encoding="utf-8")
    passed.write_text(f'+++\n{front}verdict = "pass"\n\n{warm}+++\n', encoding="utf-8")
    # Three starts, one of them failed: the cold run's count judges nothing.
    cold = [s1.Start(600.0, (launch("cold-launch"),), launch("cold-launch"))] * 2
    numbers = s1.numbers_of(
        [*cold, s1.Start(None, (launch("cold-launch", served=False, reached="boot"),))], s1.Kind.COLD
    )
    assert numbers[s1.Number.STARTS_FAILED_BOOT] == 1
    assert replace(s1.SPIKES[s1.Kind.COLD], record=pending).verdict(numbers) is Verdict.NOT_MEASURED
    assert replace(s1.SPIKES[s1.Kind.COLD], record=passed).verdict(numbers) is Verdict.PASS


def test_a_start_not_ready_in_time_reads_only_the_stages_sampled_by_the_deadline() -> None:
    wall = [WAKE]

    def sleep(seconds: float) -> None:
        wall[0] += seconds

    read: list[tuple[float, float]] = []
    stopped = launch(served=False, reached="weights")
    pool = SimpleNamespace(quiet=lambda _now: True, launches=lambda woke, now: read.append((woke, now)) or [stopped])
    run = s1.Run(Gateway(wall, ready_after=10**6), pool, 1, s1.Kind.WARM, wall=lambda: wall[0], sleep=sleep, log=print)
    start = run.start()
    assert read == [(WAKE, WAKE + s1.READY_WAIT_SECONDS)]
    assert wall[0] > WAKE + s1.READY_WAIT_SECONDS
    assert s1.outcome(start, s1.Kind.WARM) == (s1.Outcome.FAILED, "weights")


def test_a_pool_ready_only_after_the_deadline_is_a_failed_start() -> None:
    wall = [WAKE]

    def sleep(seconds: float) -> None:
        # Each poll lands a little late, so an unclamped poll would straddle the deadline.
        wall[0] += seconds + 0.01

    stopped = launch(served=False, reached="weights")
    pool = SimpleNamespace(
        quiet=lambda _now: True,
        launches=lambda _woke, now: [stopped if now <= WAKE + s1.READY_WAIT_SECONDS else launch()],
    )
    gateway = Gateway(wall, ready_after=s1.READY_WAIT_SECONDS + 1)
    run = s1.Run(gateway, pool, 1, s1.Kind.WARM, wall=lambda: wall[0], sleep=sleep, log=lambda _: None)
    numbers = run.measure()
    assert (numbers[s1.Number.STARTS_COUNTED], numbers[s1.Number.STARTS_FAILED_WEIGHTS]) == (0, 1)
