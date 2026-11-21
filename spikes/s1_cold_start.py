"""Spike S1: how long a stopped coursework instance takes to serve again, and a cold one (system
design, sections 4.3, 5.2, 5.3 and 12).

Cold-start time is the central number of the design: 10 to 13 minutes measured on stock setups, an
estimated 2 to 6 minutes from the warm pool, and 3 to 13 when the pool is empty or its instance
fails to start (section 5.2). No source measured a stopped GPU instance returning to service, or how
often one fails to restart (evidence E10). **Pass:** a warm start under 5 minutes at the 90th
percentile over 20 starts. If it fails: bake the image, lower expectations for homework, rely on
pre-warm for sessions.

**One run is one kind of start**, the kind the job is given (`RHI_S1_KIND`): `warm`, a promotion of
a stopped instance from the warm pool, on the pool as configured; or `cold`, a launch straight into
service, on the pool the owner deployed with `warm_instances` 0 for the run. The job never changes
the configuration. The kind labels the run, with CodeBuild's build number after it (`label_of`):
each build is posted as its own run, `measure/S1/warm-<build>/<part>` or
`measure/S1/cold-<build>/<part>`, so that a second build on one commit, which may post its numbers
in fewer parts, never leaves a part of the first behind.

**What it does**, through the gateway only, as a canary student (`common.canary_keys`), reading the
pool only from the workspace. It never sets a group's capacity: the wake goes to the gateway, the
scaler alone starts an instance (section 5.1), and the pool falls idle until the scaler returns it.
For each of the starts it is given, 20 unless told otherwise:

1. **Cold.** It waits until the gateway's status says the pool is `cold`, and then until no
   instance of the pool writes the lifecycle agent's stage metric (`s4_weights.Pool.quiet`).
2. **The wake.** It notes the time and sends `POST /wake` for the pool, and repeats it every minute
   while the pool is not ready, as a student's `rhi wait` does: an explicit wake counts as demand
   for 10 minutes only (section 5.3). It reads the status every `POLL_SECONDS` until the pool is
   `ready`, or until `READY_WAIT_SECONDS` have passed; then it waits `LATE_SECONDS` more before it
   reads the launches, for the samples taken before the deadline to reach the workspace, and reads
   only those: a stage sampled after the deadline is not one the start reached. A status of
   `unavailable` for `capacity_unavailable` on the way, the scaler's evidence that a launch found no capacity, is
   noted. **The whole start** is the seconds from the wake to `ready`: a healthy target behind the
   gateway, the end of section 5.3's wait. It holds what no stage does, the time before the kernel
   starts and the registration after the hook (`rhi_lifecycle.stages`).
3. **The launches of that start.** The agent sets `rhi_lifecycle_stage_seconds` for each stage of
   its launch, labelled with the launch's `transition` and the `stage`, and the node's collector
   adds `instance` and `instance_type`. The job reads every stage of the pool from the wake on,
   each value with the time of its own sample (`X and timestamp(X) > <wake>`), and the release
   metric an instance writes once Auto Scaling took it into service. No instance was writing at the
   wake, so these are launches that began after it. A launch into service is first exposed as a
   promotion, and moved to `cold-launch` once the host says its disk holds no image: **an
   instance's settled transition** is the one of its latest sample (`launches_of`). An entry to the
   warm pool (`entry`) is not a start, and is passed over.
4. **The launch that became ready** is the one launch into service that reached service (a hook
   and a release sample), was still scraped at readiness and had loaded its weights by then
   (`ready_launch`, the rule of `s4_weights.weights_of_launch`). Every other launch into service of
   the start that did not reach service failed: it was abandoned, or Auto Scaling did not take its
   completion.

**Which starts count** (`outcome`). A start failed when a launch of the run's kind (`promotion`
for a warm run, `cold-launch` for a cold one) failed, or the gateway reported
`capacity_unavailable`, even when a replacement then served: the replacement's timing is not this
kind's start. It failed too when the pool was not ready in time and a launch of the run's kind, or
none at all, was seen. A failed start is counted by why, the last stage, in launch order, its
launch of the run's kind reached (`last_stage`), served or not; with no stage series, `capacity`
when the gateway said so and `unknown` otherwise. A start whose launches are all of the other kind
is logged and not counted, and is in no number but the starts run, whether it served or not: a
cold launch in a warm run (no warm instance, or one whose disk had lost the image) is not a warm
start. Any other start counts when the launch that became ready settled as the run's kind.

**The numbers** (`Number`), posted with the verdict through `common.run_spike`: each stage and the
whole start at the median and the 90th percentile, over the counted starts; `starts_run`,
`starts_counted` and `starts_failed`, with `starts_failed_<reason>` for each reason; and
`warm_starts_image_zero`, in a warm run only, the starts of its kind with a promotion that passed
its image stage, sampled at a later stage (`image_held`): the host held the engine image after the
stop and start, so the image stage pulled nothing and only read the metadata and asked the host. A
promotion abandoned in its image stage, a host that did not answer, still has the stage's seconds,
and is no evidence that the image was on disk. Each start's outcome, transition, instance type and seconds are
printed, and the instance types of the counted starts, for the record's labels.

**The verdict** (`StartSpike`). A warm run with fewer than 20 counted starts gives none. A warm run
passes when its whole start's 90th percentile is under 300 seconds. The condition judges warm starts
only, and so does its count of 20: a cold run, whatever its counted and failed starts, carries the
verdict the warm runs of the record `docs/measurements/s1.md` give at the commit it runs from
(`ColdSpike`, `warm_verdict`), so that the record's verdict is the warm runs' and the cold run's
numbers, its timings and its failures, reach it as theirs do. With no warm run recorded there, it
has none, and cold numbers alone never make the record pass: record the warm run first.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s1_cold_start`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, or an `https://` address |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_S1_KIND` | `warm` or `cold`: the kind of start the pool, as deployed, makes |
| `RHI_S1_STARTS` | The starts to make, 20 unless given |
| `RHI_SPIKE_POOL` | The pool, `coursework` unless given |

`RHI_RUN_LABEL`, if given, must be the kind. The region comes from CodeBuild's `AWS_REGION`, the
build number from `CODEBUILD_BUILD_NUMBER`. Run it
while nothing else sends to the pool: the pool has to go cold between starts.
"""

from __future__ import annotations

import os
import time
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

import common
import s4_weights
from rhi_lifecycle.serving import RELEASE_METRIC
from rhi_lifecycle.stages import Stage, Transition
from rhi_reporter.measure import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

JOB = "s1_cold_start"
MEASUREMENT = "S1"

KIND_VARIABLE = "RHI_S1_KIND"
STARTS_VARIABLE = "RHI_S1_STARTS"
DEFAULT_STARTS = 20
BUILD_NUMBER_VARIABLE = "CODEBUILD_BUILD_NUMBER"
# The record, at the commit the job runs from: a cold run reads the verdict of its warm runs.
RECORD = Path(__file__).resolve().parents[1] / "docs" / "measurements" / "s1.md"
FENCE = "+++"

READY, COLD, CAPACITY_UNAVAILABLE = "ready", "cold", "capacity_unavailable"
# The pass condition (section 12): the whole warm start under this many seconds at the 90th
# percentile, over this many starts.
PASS_SECONDS = 300.0
VERDICT_STARTS = 20

# Seconds. The status is read this often while the pool wakes, and the wake repeated this often,
# as `rhi wait` repeats it.
POLL_SECONDS = 5
WAKE_EVERY_SECONDS = 60
# Seconds. A cold launch may pull the image for 7 minutes; the hook's own timeout is 15 (section 17).
READY_WAIT_SECONDS = 40 * 60
# Seconds. How long after the deadline a stage sample taken before it may take to reach the
# workspace: the collector scrapes every 15 seconds and writes after.
LATE_SECONDS = 2 * 60

# The stages of a launch into service, in the order a launch goes through them.
STAGES = (
    Stage.BOOT,
    Stage.IMAGE,
    Stage.RELEASE_CHECK,
    Stage.CONTAINER_START,
    Stage.WEIGHTS,
    Stage.WARM_UP,
    Stage.HOOK,
)
_ORDER = {stage.value: place for place, stage in enumerate(STAGES)}
INTO_SERVICE = frozenset({Transition.PROMOTION.value, Transition.COLD_LAUNCH.value})
# Why a start failed, when no launch of it left a stage series.
CAPACITY, UNKNOWN = "capacity", "unknown"
REASONS = (*(stage.value for stage in STAGES), CAPACITY, UNKNOWN)


class Kind(StrEnum):
    """The kind of start a run measures, and its label."""

    WARM = "warm"
    COLD = "cold"


# The settled transition of a start of each kind.
TRANSITION = {Kind.WARM: Transition.PROMOTION.value, Kind.COLD: Transition.COLD_LAUNCH.value}


class Number(StrEnum):
    """The names of the numbers a run posts, one per row of the record."""

    START_SECONDS_MEDIAN = "start_seconds_median"
    START_SECONDS_P90 = "start_seconds_p90"
    BOOT_SECONDS_MEDIAN = "boot_seconds_median"
    BOOT_SECONDS_P90 = "boot_seconds_p90"
    IMAGE_SECONDS_MEDIAN = "image_seconds_median"
    IMAGE_SECONDS_P90 = "image_seconds_p90"
    RELEASE_CHECK_SECONDS_MEDIAN = "release_check_seconds_median"
    RELEASE_CHECK_SECONDS_P90 = "release_check_seconds_p90"
    CONTAINER_START_SECONDS_MEDIAN = "container_start_seconds_median"
    CONTAINER_START_SECONDS_P90 = "container_start_seconds_p90"
    WEIGHTS_SECONDS_MEDIAN = "weights_seconds_median"
    WEIGHTS_SECONDS_P90 = "weights_seconds_p90"
    WARM_UP_SECONDS_MEDIAN = "warm_up_seconds_median"
    WARM_UP_SECONDS_P90 = "warm_up_seconds_p90"
    HOOK_SECONDS_MEDIAN = "hook_seconds_median"
    HOOK_SECONDS_P90 = "hook_seconds_p90"
    STARTS_RUN = "starts_run"
    STARTS_COUNTED = "starts_counted"
    STARTS_FAILED = "starts_failed"
    STARTS_FAILED_BOOT = "starts_failed_boot"
    STARTS_FAILED_IMAGE = "starts_failed_image"
    STARTS_FAILED_RELEASE_CHECK = "starts_failed_release_check"
    STARTS_FAILED_CONTAINER_START = "starts_failed_container_start"
    STARTS_FAILED_WEIGHTS = "starts_failed_weights"
    STARTS_FAILED_WARM_UP = "starts_failed_warm_up"
    STARTS_FAILED_HOOK = "starts_failed_hook"
    STARTS_FAILED_CAPACITY = "starts_failed_capacity"
    STARTS_FAILED_UNKNOWN = "starts_failed_unknown"
    WARM_STARTS_IMAGE_ZERO = "warm_starts_image_zero"


def seconds_number(part: str, statistic: str) -> Number:
    """The name of a stage's seconds (or, with `start`, the whole start's) at `median` or `p90`."""
    return Number(f"{part.replace('-', '_')}_seconds_{statistic}")


def failed_number(reason: str) -> Number:
    """The name of the starts that failed for `reason`, one of `REASONS`."""
    return Number(f"starts_failed_{reason.replace('-', '_')}")


# One start.


@dataclass(frozen=True)
class Launch:
    """One launch into service after a wake, seen in the workspace: its instance, its settled
    transition, its instance type, the seconds of each stage it exposed a value for, the stages it
    was sampled at, whether it reached service, when it was first and last sampled, and when its
    weights stage was first sampled (None if never)."""

    instance: str
    transition: str
    instance_type: str
    stages: Mapping[str, float]
    sampled: frozenset[str]
    served: bool
    first_seen: float
    last_seen: float
    weights_seen: float | None

    @property
    def last_stage(self) -> str:
        """The last stage, in launch order, the launch was sampled at: where an abandoned launch
        stopped, since the agent sets a stage's value when the stage ends."""
        return max(self.sampled, key=_ORDER.__getitem__)


def launches_of(
    values: Sequence[common.Series], stamps: Sequence[common.Series], serving: Sequence[common.Series], woke_at: float
) -> list[Launch]:
    """The launches into service after the wake at `woke_at`, one per instance under its settled
    transition, from the pool's stage values sampled after the wake (`values`), the times of the
    pool's stage samples (`stamps`) and its release samples taken after the wake (`serving`). An
    instance's settled transition is the one of its latest stage sample: what the agent first
    exposed as a promotion moves to `cold-launch` once the host says the disk has no image."""
    taken: dict[tuple[str, str], dict[str, list[float]]] = {}
    types: dict[str, str] = {}
    for one in stamps:
        instance, transition, stage = (one.labels.get(key, "") for key in ("instance", "transition", "stage"))
        times = [at for at in one.values() if at > woke_at]
        if not instance or transition not in INTO_SERVICE or stage not in _ORDER or not times:
            continue
        taken.setdefault((instance, transition), {}).setdefault(stage, []).extend(times)
        types[instance] = one.labels.get("instance_type", "") or types.get(instance, "")
    settled: dict[str, str] = {}
    for (instance, transition), stages in taken.items():
        latest = max(max(times) for times in stages.values())
        held = settled.get(instance)
        if held is None or latest > max(max(times) for times in taken[(instance, held)].values()):
            settled[instance] = transition
    seconds: dict[tuple[str, str], dict[str, float]] = {}
    for one in values:
        key = (one.labels.get("instance", ""), one.labels.get("transition", ""))
        if one.samples and one.labels.get("stage", "") in _ORDER:
            seconds.setdefault(key, {})[one.labels["stage"]] = one.samples[-1][1]
    in_service = {one.labels.get("instance", "") for one in serving if one.samples}
    launches = []
    for instance, transition in sorted(settled.items()):
        stages = taken[(instance, transition)]
        values_of = seconds.get((instance, transition), {})
        every = [at for times in stages.values() for at in times]
        weights = stages.get(Stage.WEIGHTS.value)
        launches.append(
            Launch(
                instance=instance,
                transition=transition,
                instance_type=types.get(instance, ""),
                stages=values_of,
                sampled=frozenset(stages),
                served=Stage.HOOK.value in values_of and instance in in_service,
                first_seen=min(every),
                last_seen=max(every),
                weights_seen=None if weights is None else min(weights),
            )
        )
    return launches


def ready_launch(launches: Sequence[Launch], ready_at: float) -> Launch | None:
    """The launch that became ready at `ready_at`: the one that reached service, was still sampled
    no more than `FRESH_SECONDS` before readiness, and had its weights first sampled no more than
    `FRESH_SECONDS` after it (the weights load before the engine serves). None for none, or two."""
    fresh = s4_weights.FRESH_SECONDS
    ready = [
        launch
        for launch in launches
        if launch.served
        and launch.last_seen >= ready_at - fresh
        and launch.weights_seen is not None
        and launch.weights_seen <= ready_at + fresh
    ]
    return ready[0] if len(ready) == 1 else None


@dataclass(frozen=True)
class Start:
    """One start: the seconds from the wake to readiness (None when the pool was not ready in time),
    its launches into service, the one that became ready (None when none could be told), and whether
    the gateway reported `capacity_unavailable` on the way."""

    seconds: float | None
    launches: tuple[Launch, ...] = ()
    ready: Launch | None = None
    capacity: bool = False

    @property
    def failed(self) -> list[Launch]:
        """Its launches that did not reach service, in the order they were first sampled."""
        return sorted((launch for launch in self.launches if not launch.served), key=lambda one: one.first_seen)


class Outcome(StrEnum):
    COUNTED = "counted"
    FAILED = "failed"
    OTHER = "not counted"


def outcome(start: Start, kind: Kind) -> tuple[Outcome, str | None]:
    """Whether a start counts for a run of `kind`, failed (with its reason, one of `REASONS`), or is
    of another kind and is not counted. A start whose launches are all of the other kind is of
    another kind, whether or not the pool was ready in time: it is a wake, and nothing else, of the
    run."""
    transition = TRANSITION[kind]
    abandoned = [launch for launch in start.failed if launch.transition == transition]
    if abandoned:
        return Outcome.FAILED, abandoned[-1].last_stage
    if start.seconds is None:
        # Not ready in time: a launch of the run's kind stopped where it was last seen, served or not
        # (Auto Scaling may have taken its hook, and the gateway still not seen it healthy).
        of_kind = sorted(
            (launch for launch in start.launches if launch.transition == transition), key=lambda one: one.first_seen
        )
        if of_kind:
            return Outcome.FAILED, of_kind[-1].last_stage
        if start.capacity:
            return Outcome.FAILED, CAPACITY
        return (Outcome.OTHER, None) if start.launches else (Outcome.FAILED, UNKNOWN)
    if start.capacity:
        return Outcome.FAILED, CAPACITY
    if start.ready is None or start.ready.transition != transition:
        return Outcome.OTHER, None
    return Outcome.COUNTED, None


def image_held(start: Start) -> bool:
    """Whether a promotion of the start passed its image stage, to a later one: the host said it held
    the engine image after the stop and start, so the agent kept the launch a promotion. A promotion
    abandoned in its image stage (the host did not answer) has the stage's seconds, and is no
    evidence of the image."""
    image = _ORDER[Stage.IMAGE.value]
    return any(
        launch.transition == Transition.PROMOTION.value and any(_ORDER[stage] > image for stage in launch.sampled)
        for launch in start.launches
    )


def numbers_of(starts: Sequence[Start], kind: Kind) -> dict[str, int | float]:
    """The numbers of a run of `kind` that made `starts`. A stage no counted start has a value for
    has no number, and a cold run has no warm start whose image stage was zero. A start of the other
    kind is one of the starts run, and in no other number."""
    outcomes = [outcome(start, kind) for start in starts]
    counted = [start for start, (result, _) in zip(starts, outcomes, strict=True) if result is Outcome.COUNTED]
    reasons = [reason for result, reason in outcomes if result is Outcome.FAILED]
    numbers: dict[str, int | float] = {
        Number.STARTS_RUN: len(starts),
        Number.STARTS_COUNTED: len(counted),
        Number.STARTS_FAILED: len(reasons),
        **{failed_number(reason): reasons.count(reason) for reason in REASONS},
    }
    if kind is Kind.WARM:
        numbers[Number.WARM_STARTS_IMAGE_ZERO] = sum(
            result is not Outcome.OTHER and image_held(start)
            for start, (result, _) in zip(starts, outcomes, strict=True)
        )
    samples: dict[str, list[float]] = {"start": [start.seconds for start in counted if start.seconds is not None]}
    for stage in STAGES:
        samples[stage.value] = [
            s.ready.stages[stage.value] for s in counted if s.ready and stage.value in s.ready.stages
        ]
    for part, held in samples.items():
        if held:
            numbers[seconds_number(part, "median")] = common.median(held)
            numbers[seconds_number(part, "p90")] = common.percentile(held, 90)
    return numbers


# The verdict.


def passes_warm(numbers: common.Numbers) -> bool:
    """Whether the whole warm start's 90th percentile is under the pass condition's seconds."""
    return numbers[Number.START_SECONDS_P90] < PASS_SECONDS


def is_warm_label(label: object) -> bool:
    """Whether a run label is a warm run's (`label_of`)."""
    return isinstance(label, str) and (label == Kind.WARM.value or label.startswith(f"{Kind.WARM.value}-"))


def warm_verdict(record: Path | None = None) -> Verdict:
    """The verdict the warm runs of the record give, by the record's rule (docs/measurements/README.md):
    fail when one failed; otherwise none (not measured) when one is pending or there is none;
    otherwise pass. A record that cannot be read gives none. `RECORD` unless given."""
    try:
        lines = (RECORD if record is None else record).read_text(encoding="utf-8").splitlines()
        runs = tomllib.loads("\n".join(lines[1 : lines.index(FENCE, 1)])).get("runs", [])
        verdicts = [run.get("verdict") for run in runs if is_warm_label(run.get("run"))]
    except (OSError, ValueError, AttributeError, TypeError):
        return Verdict.NOT_MEASURED
    if "fail" in verdicts:
        return Verdict.FAIL
    if not verdicts or any(verdict != "pass" for verdict in verdicts):
        return Verdict.NOT_MEASURED
    return Verdict.PASS


@dataclass(frozen=True)
class StartSpike(common.Spike):
    """A warm run: no verdict with fewer than `VERDICT_STARTS` counted starts."""

    def verdict(self, numbers: common.Numbers) -> Verdict:
        if numbers.get(Number.STARTS_COUNTED, 0) < VERDICT_STARTS:
            return Verdict.NOT_MEASURED
        return super().verdict(numbers)


@dataclass(frozen=True)
class ColdSpike(common.Spike):
    """A cold run. The condition and its 20 starts judge warm starts only, so a cold run, whatever
    its counted and failed starts, carries the verdict the record's warm runs give at the commit it
    runs from (`warm_verdict`): its numbers are recorded beside theirs. With no warm run recorded
    yet it has none, and cannot make the record pass."""

    record: Path | None = None

    def verdict(self, numbers: common.Numbers) -> Verdict:
        return warm_verdict(self.record)


SPIKES: dict[Kind, common.Spike] = {
    Kind.WARM: StartSpike(MEASUREMENT, Number, passes_warm),
    Kind.COLD: ColdSpike(MEASUREMENT, Number, lambda _numbers: True),
}


# The pool, through the gateway and the workspace.


class Gateway(s4_weights.Gateway):
    """The gateway, asked about the pool as a canary student (`s4_weights.Gateway`)."""

    def status(self) -> tuple[str, str]:
        """The pool's state and its cause as the status gives them; empty when not read."""
        try:
            pools = self._ask("GET", "/status")["pools"]
            record = next(record for record in pools if record["model"] == self.pool)
            return str(record["state"]), str(record.get("cause", ""))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
            return "", ""


class Pool(s4_weights.Pool):
    """The lifecycle agent's stage metric of the pool, in the workspace (`s4_weights.Pool`)."""

    def launches(self, woke_at: float, now: float) -> list[Launch]:
        """The launches into service from the wake at `woke_at` to `now` (`launches_of`)."""
        stages = self._selector()
        span = {"start": woke_at, "end": now, "step": s4_weights.SCRAPE_SECONDS}
        # One query: each value is kept on the test of its own sample's time, both read together.
        values = self.workspace.read_series(f"{stages} and timestamp({stages}) > {woke_at:.3f}", **span)
        stamps = self.workspace.read_series(f"timestamp({stages})", **span)
        release = f'{RELEASE_METRIC}{{pool="{self.name}"}}'
        serving = self.workspace.read_series(f"{release} and timestamp({release}) > {woke_at:.3f}", **span)
        return launches_of(values, stamps, serving, woke_at)


class Run:
    """One run of the spike: `starts` starts of `kind` of the pool, each by a wake through
    `gateway`, each one's launches read from `pool`."""

    def __init__(
        self,
        gateway: Any,
        pool: Any,
        starts: int,
        kind: Kind,
        *,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        if starts < 1:
            raise ValueError("spike S1 makes one start or more")
        self.gateway, self.pool, self.starts, self.kind = gateway, pool, starts, kind
        self.wall, self.sleep, self.log = wall, sleep, log

    def _wait(self, done: Callable[[], bool], limit: float, every: float, what: str) -> None:
        started = self.wall()
        while not done():
            if self.wall() - started >= limit:
                raise TimeoutError(f"the pool {what} in time")
            self.sleep(every)

    def start(self) -> Start:
        """One start: a cold pool, the wake, readiness or the deadline, and the launches it brought."""
        self._wait(
            lambda: self.gateway.state() == COLD,
            s4_weights.COLD_WAIT_SECONDS,
            s4_weights.COLD_POLL_SECONDS,
            "did not go cold",
        )
        self._wait(
            lambda: self.pool.quiet(self.wall()),
            s4_weights.QUIET_WAIT_SECONDS,
            s4_weights.COLD_POLL_SECONDS,
            "kept writing",
        )
        woke_at = woken = self.wall()
        self.gateway.wake()
        capacity = False
        while True:
            state, cause = self.gateway.status()
            capacity = capacity or cause == CAPACITY_UNAVAILABLE
            now = self.wall()
            if state == READY or now - woke_at >= READY_WAIT_SECONDS:
                break
            if now - woken >= WAKE_EVERY_SECONDS:
                woken = now
                self.gateway.wake()
            self.sleep(POLL_SECONDS)
        if state != READY:
            # A sample taken before the deadline reaches the workspace up to `LATE_SECONDS` after it,
            # and one taken after it is not read.
            self.sleep(LATE_SECONDS)
            deadline = woke_at + READY_WAIT_SECONDS
            return Start(None, tuple(self.pool.launches(woke_at, deadline)), capacity=capacity)
        ready_at = self.wall()
        while True:
            launches = self.pool.launches(woke_at, self.wall())
            ready = ready_launch(launches, ready_at)
            if ready is not None or self.wall() - ready_at >= s4_weights.READ_WAIT_SECONDS:
                return Start(ready_at - woke_at, tuple(launches), ready, capacity)
            self.sleep(s4_weights.READ_POLL_SECONDS)

    def _describe(self, number: int, start: Start) -> str:
        result, reason = outcome(start, self.kind)
        line = f"start {number}: {result}"
        if reason is not None:
            line += f", last stage {reason}"
        if start.seconds is not None:
            line += f", ready after {start.seconds:.0f} s"
        if start.ready is not None:
            stages = ", ".join(
                f"{stage} {seconds:.1f} s"
                for stage, seconds in sorted(start.ready.stages.items(), key=lambda item: _ORDER[item[0]])
            )
            line += f", {start.ready.transition} on {start.ready.instance_type or 'an unlabelled type'}: {stages}"
        for launch in start.failed:
            line += f"; a {launch.transition} did not reach service, last stage {launch.last_stage}"
        return line

    def measure(self) -> dict[str, int | float]:
        starts = []
        for number in range(1, self.starts + 1):
            start = self.start()
            starts.append(start)
            self.log(self._describe(number, start))
        types: dict[str, int] = {}
        for start in starts:
            if outcome(start, self.kind)[0] is Outcome.COUNTED and start.ready is not None:
                types[start.ready.instance_type] = types.get(start.ready.instance_type, 0) + 1
        self.log(f"instance types of the counted starts: {types or 'none'}")
        return numbers_of(starts, self.kind)


# The job.


def kind_of(environ: Mapping[str, str]) -> Kind:
    """The run's kind, which is its label. ValueError when it is missing, not `warm` or `cold`, or
    when `RHI_RUN_LABEL` gives another label."""
    try:
        kind = Kind(environ.get(KIND_VARIABLE, ""))
    except ValueError:
        raise ValueError("spike S1 needs the kind of start, warm or cold") from None
    if environ.get(common.RUN_LABEL_VARIABLE, kind.value) != kind.value:
        raise ValueError("the run label of spike S1 is the kind of start")
    return kind


def label_of(kind: Kind, environ: Mapping[str, str]) -> str:
    """The run's label: the kind, and CodeBuild's number of the build after a hyphen (`warm-7`).
    A status replaces the one of its context, so a second build on one commit, which may post its
    numbers in fewer parts, never leaves a part of the first behind it. Without a build number,
    the kind alone."""
    build = environ.get(BUILD_NUMBER_VARIABLE, "")
    return f"{kind.value}-{build}" if build.isdigit() else kind.value


def starts_of(environ: Mapping[str, str]) -> int:
    """The starts to make, `DEFAULT_STARTS` unless given. ValueError for a count that is not a whole
    number of one or more."""
    starts = int(environ.get(STARTS_VARIABLE, str(DEFAULT_STARTS)))
    if starts < 1:
        raise ValueError("spike S1 makes one start or more")
    return starts


def main(environ: Mapping[str, str] = os.environ) -> Verdict:
    """Runs the spike with the job's environment and credentials, and posts its numbers under the
    kind of start."""
    kind = kind_of(environ)
    starts = starts_of(environ)
    # Refused before anything is measured: the canary key goes over HTTPS only.
    endpoint = s4_weights.gateway_address(environ.get(s4_weights.ENDPOINT_VARIABLE, ""))

    def measure() -> dict[str, int | float]:
        name = environ.get(s4_weights.POOL_VARIABLE, s4_weights.DEFAULT_POOL)
        session = boto3.Session()
        workspace = common.Workspace(
            environ[s4_weights.WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials()
        )
        gateway = Gateway(endpoint, name, common.canary_keys().students[0])
        print(f"pool {name}, {kind} starts, {starts} of them")
        return Run(gateway, Pool(name, workspace), starts, kind).measure()

    label = label_of(kind, environ)
    return common.run_spike(SPIKES[kind], JOB, measure, environ={**environ, common.RUN_LABEL_VARIABLE: label})


if __name__ == "__main__":
    main()
