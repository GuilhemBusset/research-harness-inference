"""Spike S7: what scale-in and a gateway deployment do to a request, on the mock pool (system design,
sections 5.1, 8.5 and 12).

AWS's documentation does not settle whether a group waits the whole deregistration delay for a
target that has no connections, nor whether it drains at all before stopping an instance into the
warm pool (evidence E13). The design does not lean on a long delay (section 5.1), and gives the
gateway's target group 1800 seconds so that a held request or a long stream outlives a gateway
deployment (section 8.5). This job asks both of the mock pool, for cents, before any GPU depends on
them. **Pass:** an idle instance stops within a minute or two, which the verdict reads as
`LEAVE_PASS_SECONDS`, 120 seconds, on each path; in-flight requests finish; no wake is lost.

**What it does**, through the gateway only, as canary students (`common.canary_keys`), and reading
the pool only from the workspace and the pool-state table. It never changes the pool: the scaler
alone moves it, by load and by idleness (section 5.1).

1. **The stream across a gateway deployment** (`Run.across_deployment`). It notes the gateway's
   tasks, opens a held stream and prints when the hold begins and how long it may last. The owner
   starts the deployment, by releasing the pipeline again: the job starts none. The stream is held
   until the rollout has reached the tasks the job began with (`rollout_under`), or for
   `RHI_S7_DEPLOYMENT_HOLD_SECONDS` at most. It counts only when the task under it was one the
   deployment drained (below).
2. **Two instances.** It holds twice the pool's `target_concurrency` of short streams until the
   scaler asks for two instances and reads two in service, then stops the load.
3. **The departures.** It sends nothing more but the stream across a scale-in (`Run.relay`), as one
   canary student, from while the scaler still asks for two until the first instance the scaler
   takes away has stopped. It watches the scaler's note: each time the note asks for fewer
   instances, the scaler has chosen an idle one to leave. The note's `written_at` is when the
   scaler's run began; it asked for fewer during that run, by the time the job read the note.
4. **The wakes** (`Run.send_wakes`). As many times as `RHI_S7_WAKES` gives, it makes sure the pool
   is awake, waits until the scaler asks for no instance and reads none in service, which it does
   only after the pool's idle period, and sends one request at once. The reading of none in service
   counts only when it is at least as recent as the scaler's note (`Pool.gauge`): an older zero may
   be from before the pool last woke. A wake is lost when its
   request does not complete (below) within the pool's hold and `WAKE_MARGIN_SECONDS`. After a lost
   wake, it sends requests to bring the pool back, up to `RECOVERIES` of them, which are not wakes
   and are not counted, and goes on to the next.

A stream completed when it was answered 200 and its closing event, `message_stop`, arrived, with no
error event. The mock streams its canned turn in well under a second, so no one inference request
lasts as long as a scale-in or a deployment; each stream is held open where the event it spans acts.

**The stream across a gateway deployment** (`slow_stream`) is held on the gateway task that took it,
which the deployment replaces. The gateway reads a request's whole body before it does anything
with it, so the job sends the body but its last byte, then one byte of padding every
`TRICKLE_SECONDS`, white space that JSON allows before the closing brace, and the last byte once
the rollout has reached the old tasks. Until then its connection is open on that task, and its
answer, forwarded to the pool then, comes from a task the deployment is draining.

**The stream across a scale-in** (`Run.relay`) is held on the pool, whose instance the scale-in
removes: inference must be in flight there when it happens. The mock answers its canned turn in
about half a second at its rate (`rhi_mock_engine.app`), and the job does not change the pool, so
no one request lasts as long as the moments it must span. So the relay is `RELAY_STREAMS` chains of
whole streamed requests, each chain sending its next once the one before has ended, side by side so
that one is in flight while another starts. They run from while the scaler still asks for two
instances until the job has seen the first departure over, as one canary student, one under the
student's limit and below the load that keeps an instance in service. The job sees a departure over
only `QUIET_SECONDS` after the instance stopped, so the streams across the scale-in are told
afterwards, from the departure as measured (`across_scale_in`). Neither event of a departure is seen
at its moment, only between two bounds (`Departure.events`): the scaler asks for fewer after its run
began, the note's `written_at`, and before it writes the note (`rhi_scaler.aws_io.apply`), which the
job read by the time it saw it; the instance stopped after its last write and within a scrape of it.
A request is in flight, as the student sees it, from its first event from the engine
(`INFERENCE_EVENT`) to its stream's end. The relay spanned an event when its completed streams were
in flight, together, over every moment between the event's bounds (`covered`): wherever the event
happened, a request was in flight on the pool then, and it completed. Streams that leave a moment of
the bounds uncovered do not span it, so neither does one that only crosses a bound. A stream that did
not complete counts against the scale-in only when it ended while the instance was surely leaving:
after the latest the scaler may have asked for fewer and by the latest it may have stopped. One cut
or refused earlier, before the scaler surely asked, says nothing of the scale-in. The number is 1
when the streams spanned an event and none of those failed, 0 when one did. When none spanned an
event, or no stop was seen, the next cycle holds them again, and a run in which they never did is
not measured, and is run again.

**When the gateway was deployed** (`rollout_under`). The gateway's collector writes the series `up`
of its scrape, job `gateway`, labelled with the task's ID, every `SCRAPE_SECONDS` while the task
runs. A task appearing is not a deployment: a deployment replaces tasks. The job reads the tasks
writing just before it opens the stream, and the stream is timed from when it opened, its first part
sent. The rollout has reached the old tasks under the stream when one of the tasks writing as the
stream opened has stopped since (silent for `QUIET_SECONDS`), by the time the stream ended, while a
task new to the job had begun by then and still wrote when the old one stopped: the job then sends
the last byte.

**Which task the stream was on.** The service starts its new tasks first and then stops the old
ones (`min_healthy_percent` 100, `max_healthy_percent` 200) once the new ones are healthy. ECS stops
an old task only once its target has deregistered, which waits for the requests on it: the task
under the stream drains, kept running by the stream, and stops once it has ended. So the task a
deployment drained under the stream is one of those the job began with that ran until the stream
ended and stopped within `HOLDER_STOP_SECONDS` after (`drained_under`). That a task stopped then
does not say the stream was on it: a replacement, which may serve before its collector first
writes, may have taken it while an old task stopped for another reason. The gateway counts each
request it settles, per task (`SETTLED`, its input tokens' histogram, labelled with the task's ID),
and the job sends nothing else meanwhile: the task whose count rose after the last byte was sent
(`settled_after`) is the one that answered the stream. A completed stream is 1 only when that task
is the only one whose count rose, and the deployment drained it (`across_rollout`). A task that
stopped before its collector scraped the count leaves the stream untold. A stream cut short settles
nothing: it is 0 when exactly one task the job began with stopped as it was cut, and no replacement
may have held the stream as it opened (`replacement_may_hold`): a task new to the job that wrote by
then, or first wrote within `COLLECTOR_LAG_SECONDS` after, may have been serving as it opened, its
collector starting a moment after it. Otherwise the job saw no deployment under the stream, on a
task it drained, and the run is not measured.

**When an instance has left.** The scaler's gauges cannot say it: an instance stops counting as in
service as soon as its target is draining, and the warm count holds an instance from the moment it
enters the warm pool's wait state, before it is stopped (`rhi_scaler.aws_io`). An instance has
stopped, terminated or stopped into the warm pool, when its node's collector stops writing. The
collector's Prometheus receiver writes the series `up` for each of its scrape targets every
`SCRAPE_SECONDS`, the DCGM target's included even where nothing listens there, labelled with the
instance's ID and its pool; it runs until the instance shuts down, after the engine's unit, which
the lifecycle agent stops only once the termination hook is complete (`rhi_lifecycle.termination`).
So the instance that left is the one, among those writing when the note asked for fewer, whose
last write is `QUIET_SECONDS` old. It took at most `leave_seconds` to leave: from the note's
`written_at`, when the scaler's run began, to its last write, plus one scrape, by which it had
stopped. It took at least the seconds from when the job read the note, by which the scaler had
asked, to its last write. The scaler's run may take most of a minute before it asks, so when these
bounds lie on either side of `LEAVE_PASS_SECONDS` (`straddles`), the time is not told: that
departure has no path, and is not a number. When none has stopped `LEAVE_WAIT_SECONDS` after the
note, no stop was seen: that departure has no path either.

**Which path it took** (`path_of`), from the scaler's readings of the warm count, each at the time
the scaler wrote it (`Pool.warm_counts`), which counts an instance going back to the warm pool from
its first wait state. The path is told only from the reading the decision was made on and one at or
after the instance stopped (`warm_around`). The scaler reads the pool, decides, acts, then writes
its readings, as they were before it acted, stamped in milliseconds within the second its note's
`written_at` gives in whole seconds: the reading the decision was made on is that run's
(`decided_on`). When its push was lost, an earlier run's reading stands for it only if it read zero:
a warm instance read then may have left the warm pool before the decision and the departing one
refilled its place, which no later reading tells from no change. With them, the warm count rose and
no instance new to the pool, none writing when that reading was made, wrote, so the instance went
back to the warm pool; the warm count did not rise, so it was terminated. Without them, or with a
rise while a new instance wrote (a fresh instance launched into the warm pool in place of a
terminated one), that departure has no path, and is not a number.

The readings of a departure end where the scaler's next decision acts (`warm_bound`): once a note
asks for another number of instances, a reading after that run may show the next instance going to
the warm pool, so only the readings up to the last run seen to ask for the same number count. When
the instance stopped after that, the departures overlap and that one has no path. Each reading is
its own value at its own time: the values and the times are two range queries, and a step where a
newer reading became visible between them is dropped (`Pool.readings`). A
run whose departures did not take both paths takes the pool to two instances again, up to `CYCLES`
times, and is not measured when a path is still missing. A path seen twice keeps its longer time.

**The numbers** (`Number`): the seconds to leave service on each path, whether each held stream
completed (1 or 0), and the wakes sent and lost.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s7_drain`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_S7_WAKES` | The wakes it sends, each at the moment the pool reaches zero; `DEFAULT_WAKES` unless given |
| `RHI_S7_DEPLOYMENT_HOLD_SECONDS` | The longest the stream across the gateway deployment is held; `DEFAULT_DEPLOYMENT_HOLD_SECONDS` unless given |
| `RHI_SPIKE_POOL` | The pool, `mock` unless given; a pool whose engine is not the mock is refused |
| `RHI_RUN_LABEL` | The run label (`common.run_label`), to which the job adds the build's number |

The pool's `target_concurrency`, `hold_seconds` and `idle_minutes` come from the deployed
configuration (`rhi.config.read_ssm`), and the region from CodeBuild's `AWS_REGION`. Run it while
nothing else sends to the pool: other load keeps instances from going idle.

**Each run its own label** (`run_label`). A status replaces the one of the same context, and a run
that ends unmeasured posts one part where a measured one posts more: a retry under the same label
would leave the parts of two runs that `rhi_reporter.measure.decode` refuses together. So the label
ends with CodeBuild's `CODEBUILD_BUILD_NUMBER`, which each build of the project has its own of, or
with random hex where it is not given, and every run is under its own contexts.
"""

from __future__ import annotations

import asyncio
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import boto3
import httpx

import common
import loadgen
from rhi import config
from rhi_admin.roster import DEFAULT_CONCURRENCY_LIMIT
from rhi_common.records import NOTE_RECORD, ScalerNote
from rhi_gateway.experience import INPUT_TOKENS
from rhi_gateway.sse import SseReader
from rhi_scaler.metrics import INSTANCES_IN_SERVICE, WARM_INSTANCES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping, Sequence

JOB = "s7_drain"
MEASUREMENT = "S7"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_STATE_TABLE_VARIABLE = "RHI_SPIKE_POOL_STATE_TABLE"
WAKES_VARIABLE = "RHI_S7_WAKES"
DEPLOYMENT_HOLD_VARIABLE = "RHI_S7_DEPLOYMENT_HOLD_SECONDS"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "mock"
# The engine of the only pools the job runs on (`rhi_common.pool_config.ENGINES`).
MOCK_ENGINE = "mock"
# Each build of a CodeBuild project has its own number.
BUILD_NUMBER_VARIABLE = "CODEBUILD_BUILD_NUMBER"
DEFAULT_WAKES = 3
# Under the gateway target group's 1800 seconds of deregistration delay (section 8.5), and long
# enough for a release of the pipeline to reach the gateway once the owner has started it.
DEFAULT_DEPLOYMENT_HOLD_SECONDS = 1500

# Seconds: "an idle instance stops within a minute or two" (section 12), read as two minutes.
LEAVE_PASS_SECONDS = 120
# Seconds. The collectors scrape, and write `up`, every 15 seconds; an instance whose last write is
# this old has stopped.
SCRAPE_SECONDS = 15
QUIET_SECONDS = 90
# Seconds the job waits for an instance to stop after the scaler asked for fewer.
LEAVE_WAIT_SECONDS = 15 * 60
# Seconds. The scaler runs once a minute; its note is polled more often, so a decision is seen soon.
NOTE_POLL_SECONDS = 5
POLL_SECONDS = 15
# Seconds between two steps of a range query.
STEP_SECONDS = 15
# Seconds a gauge's last sample may be old and still be the scaler's last reading: one run's period,
# its 50-second timeout and a margin (as in `spikes/s8_stickiness.py`).
FRESH_SECONDS = 120
# Seconds. The scaler adds an instance after 60 seconds above target, and the mock starts in about
# a minute; it removes one after 10 minutes of load that one fits, and 15 since the scale-out, and
# the last after the pool's idle period (section 5.1).
SCALE_OUT_WAIT_SECONDS = 20 * 60
SCALE_IN_WAIT_SECONDS = 30 * 60
# The instances the job brings the pool to (`max_instances` of the mock pool), and how many times
# it does, to see both paths.
INSTANCES = 2
CYCLES = 2
# Seconds between two bytes of the body of the stream across a gateway deployment: the edge's idle
# timeout is far longer.
TRICKLE_SECONDS = 5
PADDING = b" "
# The longest the stream across a scale-in is held: the first departure's longest watch.
SCALE_IN_HOLD_SECONDS = SCALE_IN_WAIT_SECONDS + LEAVE_WAIT_SECONDS + 3 * QUIET_SECONDS
# Seconds. The scaler's note keeps its run's time in whole seconds, its gauges in milliseconds: the
# readings of the run that wrote a note are stamped within this second after its `written_at`.
NOTE_SECOND = 1
# Seconds within which a gateway task the deployment drained under the stream stops once the stream
# has ended: its target finishes deregistering when it has no request left, then ECS stops it.
HOLDER_STOP_SECONDS = 60
# Seconds after the stream across the gateway deployment ended during which the job looks for that
# stop: until it would show, the task silent for `QUIET_SECONDS`.
HOLDER_WATCH_SECONDS = HOLDER_STOP_SECONDS + QUIET_SECONDS + SCRAPE_SECONDS
# The gateway's count of the requests each task settled (`rhi_gateway.experience`).
SETTLED = f"{INPUT_TOKENS}_count"
# Seconds by which a gateway task's collector first writes once the task has started: ECS starts the
# task's two containers together, and the collector scrapes every `SCRAPE_SECONDS` and pushes what it
# scraped within seconds. Four scrapes, so a slow start is still inside. A task new to the job that
# first wrote no later than this after the stream opened may have been serving as it opened, and
# have taken it (`replacement_may_hold`).
COLLECTOR_LAG_SECONDS = 4 * SCRAPE_SECONDS
GATEWAY_JOB = "gateway"
# Seconds a wake may take beyond the pool's hold before it counts as lost.
WAKE_MARGIN_SECONDS = 120
# The requests the job sends, one after another, to bring the pool back after a lost wake.
RECOVERIES = 3
# The streams one student of the load holds: one under a student's limit (as in S8).
STREAMS_PER_STUDENT = DEFAULT_CONCURRENCY_LIMIT - 1
# The chains of the stream across a scale-in, side by side, as one student.
RELAY_STREAMS = STREAMS_PER_STUDENT
START_TOKENS = 200
CLOSING_EVENT = "message_stop"
# The engine's first event of a Messages stream: the gateway writes none of its own, so its arrival
# says the request's inference has begun on the pool.
INFERENCE_EVENT = "message_start"
ERROR_EVENT = "error"


class Number(StrEnum):
    """The names of the numbers the job posts."""

    LEAVE_SECONDS_TERMINATED = "leave_seconds_terminated"
    LEAVE_SECONDS_WARM_POOL = "leave_seconds_warm_pool"
    STREAM_COMPLETED_SCALE_IN = "stream_completed_scale_in"
    STREAM_COMPLETED_GATEWAY_DEPLOYMENT = "stream_completed_gateway_deployment"
    WAKES_SENT = "wakes_sent"
    WAKES_LOST = "wakes_lost"


class Path(StrEnum):
    """The two ways an instance leaves service."""

    TERMINATED = "terminated"
    WARM_POOL = "warm_pool"


LEAVE_NUMBER = {Path.TERMINATED: Number.LEAVE_SECONDS_TERMINATED, Path.WARM_POOL: Number.LEAVE_SECONDS_WARM_POOL}


# The verdict.


def passes(numbers: common.Numbers) -> bool:
    """An idle instance gone within `LEAVE_PASS_SECONDS` on each path, both held streams complete,
    and at least one wake sent with none lost."""
    return (
        all(numbers[name] <= LEAVE_PASS_SECONDS for name in LEAVE_NUMBER.values())
        and numbers[Number.STREAM_COMPLETED_SCALE_IN] == 1
        and numbers[Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT] == 1
        and numbers[Number.WAKES_SENT] >= 1
        and numbers[Number.WAKES_LOST] == 0
    )


SPIKE = common.Spike(MEASUREMENT, Number, passes)


# A departure.


def last_writes(series: Sequence[common.Series]) -> dict[str, float]:
    """The time each instance last wrote, from a range query of the time of its samples
    (`timestamp(...)`): the latest value of each instance's series."""
    return {instance: last for instance, (_, last) in spans(series).items()}


def spans(series: Sequence[common.Series]) -> dict[str, tuple[float, float]]:
    """The first and the last time each instance wrote, from a range query of the time of its
    samples (`timestamp(...)`)."""
    found: dict[str, tuple[float, float]] = {}
    for one in series:
        instance = one.labels.get("instance")
        if instance and one.samples:
            first, last = found.get(instance, (math.inf, 0.0))
            found[instance] = (min(first, *one.values()), max(last, *one.values()))
    return found


def stopped(written: Mapping[str, float], among: set[str], now: float) -> dict[str, float]:
    """The instances of `among` that have stopped by `now`, each with its last write: none in
    `QUIET_SECONDS`, or none at all in `written`, which then gives no time."""
    return {
        instance: written[instance]
        for instance in among
        if instance in written and written[instance] <= now - QUIET_SECONDS
    }


def leave_seconds(asked_at: float, last_written: float) -> float:
    """The seconds from the scaler asking for fewer to the instance having stopped: its last write
    and one scrape, by which the next write would have come."""
    return max(0.0, last_written + SCRAPE_SECONDS - asked_at)


def straddles(asked_at: float, seen_at: float, last_written: float) -> bool:
    """Whether the seconds to leave, which lie between the least, from `seen_at`, when the job read
    the note asking for fewer, to the last write, and the most (`leave_seconds`), may be on either
    side of `LEAVE_PASS_SECONDS`: then they do not say whether the departure passed."""
    return last_written - seen_at <= LEAVE_PASS_SECONDS < leave_seconds(asked_at, last_written)


def path_of(warm_before: int, warm_after: int, newcomer: bool) -> Path | None:
    """The path a departure took, from the warm count before the note and the highest after it,
    and whether an instance new to the pool wrote since: `None` when they do not tell."""
    if warm_after <= warm_before:
        return Path.TERMINATED
    return None if newcomer else Path.WARM_POOL


def decided_on(samples: Sequence[tuple[float, float]], at: float) -> tuple[float, int] | None:
    """The warm count the scaler decided on in its run of `at`, a note's `written_at`, and when it
    was read. It is that run's own reading, stamped within `NOTE_SECOND` of `at`. When that run's
    reading was not written, the last one before stands for it only if it read zero: a warm
    instance read then may have left the warm pool before the decision and the departing one
    taken its place, but an empty warm pool has none to lose. `None` otherwise."""
    own = [(moment, value) for moment, value in samples if at <= moment < at + NOTE_SECOND]
    if own:
        return own[-1][0], round(own[-1][1])
    earlier = [(moment, value) for moment, value in samples if moment < at]
    if earlier and round(earlier[-1][1]) == 0:
        return earlier[-1][0], 0
    return None


def warm_around(
    samples: Sequence[tuple[float, float]], at: float, through: float | None = None
) -> tuple[int, int] | None:
    """The warm count the scaler decided on in its run of `at` (`decided_on`), and the highest it
    read after that run. `None` when there is no such decision reading, or, `through` given, no
    reading is at or after `through`: the count was not observed across the departure."""
    decided = decided_on(samples, at)
    if decided is None or (through is not None and not any(moment >= through for moment, _ in samples)):
        return None
    first = decided[1]
    return first, max([first, *(round(value) for moment, value in samples if moment >= at + NOTE_SECOND)])


@dataclass(frozen=True)
class Departure:
    """One instance's way out of service: its path, `None` when it was not observed, its seconds,
    and the instance, `None` when none stopped in the time the job waits. `asked_at` is when the
    scaler's run that asked for fewer began, and `stopped_at` when the instance had stopped,
    `asked_at` and its seconds: `None` when not seen.

    Neither event's moment is observed, only bounds of it (`events`). The scaler asks for fewer
    after its run began and before it writes its note, which the job read at `seen_at`; the
    instance stopped after its last write, `last_written`, and by `stopped_at`. Without them, the
    bound is the moment given."""

    path: Path | None
    seconds: float
    instance: str | None = None
    asked_at: float | None = None
    stopped_at: float | None = None
    seen_at: float | None = None
    last_written: float | None = None

    def events(self) -> list[tuple[float, float]]:
        """The earliest and the latest each event of the departure may have happened, the scaler
        asking for fewer and the instance stopping: none for one not seen."""
        found: list[tuple[float, float]] = []
        if self.asked_at is not None:
            found.append((self.asked_at, max(self.asked_at, self.seen_at or self.asked_at)))
        if self.stopped_at is not None:
            found.append((min(self.stopped_at, self.last_written or self.stopped_at), self.stopped_at))
        return found


def warm_bound(asked: int, notes: Sequence[ScalerNote], decided_at: float) -> float:
    """The time before which the scaler's readings of the warm count are those of a departure the
    scaler decided at `decided_at`, asking for `asked` instances, from the notes the job read since,
    in order. A later decision (a note that asks for another number) acts on the pool too: a
    reading after its run may show its own instance entering the warm pool. So the readings are
    bounded by the last run seen to ask for `asked` before it, whose readings were made before any
    later decision acted; none is, while no note asks for another number."""
    confirmed = decided_at
    for note in notes:
        if note.capacity_asked != asked:
            return confirmed + NOTE_SECOND
        confirmed = max(confirmed, float(note.written_at))
    return math.inf


@dataclass(frozen=True)
class Streamed:
    """One stream of the stream across a scale-in (`Run.relay`): when its inference began, its first
    event from the engine arrived, or, when none did, when it was sent; when it ended; and how."""

    began: float
    ended: float
    answer: Answer


def covered(spans: Sequence[tuple[float, float]], earliest: float, latest: float) -> bool:
    """Whether the spans, each from when a stream was first in flight to when it ended, hold
    together every moment from `earliest` to `latest`, both included."""
    reach, touched = earliest, False
    for began, ended in sorted(spans):
        if began > reach:
            break
        if ended >= reach:
            reach, touched = ended, True
    return touched and reach >= latest


def across_scale_in(streams: Sequence[Streamed], departure: Departure | None) -> bool | None:
    """The stream across a scale-in, from the streams of the relay and the departure it spanned. The
    relay spanned the departure when its completed streams were in flight, together, over every
    moment between the bounds of one of its two events, the scaler asking for fewer or the instance
    stopping (`Departure.events`, `covered`): wherever the event happened, a stream was in flight.
    False when a stream that did not complete ended while the instance was surely leaving, after the
    latest the scaler may have asked and by the latest the instance may have stopped: one cut or
    refused earlier says nothing of the scale-in. Otherwise True when the streams spanned an event,
    and `None` when they did not, or when no stop was seen.

    A departure that does not say when it began (`asked_at`) is bounded by when it was seen over:
    the stream across it is the one whose inference began before then and ended after, every one
    before having completed, as `Answer` read it."""
    if departure is not None and departure.asked_at is not None:
        if departure.stopped_at is None:
            return None
        events = departure.events()
        asked_by, stopped_by = events[0][1], departure.stopped_at
        if any(not stream.answer.completed and asked_by <= stream.ended <= stopped_by for stream in streams):
            return False
        spans = [(stream.began, stream.ended) for stream in streams if stream.answer.completed]
        return True if any(covered(spans, earliest, latest) for earliest, latest in events) else None
    for stream in streams:
        if not stream.answer.completed:
            return False
        if stream.answer.over_as_ended:
            return True if stream.answer.over_as_begun is False else None
    return None


def numbers_of(
    departures: Sequence[Departure], streams: Mapping[Number, bool], wakes_sent: int, wakes_lost: int
) -> dict[str, int | float]:
    """The numbers the job posts. ValueError when a path has no departure: the run is not measured."""
    numbers: dict[str, int | float] = {}
    for path, name in LEAVE_NUMBER.items():
        seconds = [departure.seconds for departure in departures if departure.path is path]
        if not seconds:
            raise ValueError(f"no departure took the path {path}")
        numbers[name] = max(seconds)
    for name, completed in streams.items():
        numbers[name] = int(completed)
    numbers[Number.WAKES_SENT] = wakes_sent
    numbers[Number.WAKES_LOST] = wakes_lost
    return numbers


# A gateway deployment.


def rollout_at(before: set[str], tasks: Mapping[str, tuple[float, float]]) -> float | None:
    """When the first gateway task not in `before` first wrote, from each task's first and last
    write: `None` when none did. A task appearing is not yet a rollout (`rollout_under`)."""
    return min((first for task, (first, _) in tasks.items() if task not in before), default=None)


def replacement_at_open(before: set[str], tasks: Mapping[str, tuple[float, float]], began: float) -> bool:
    """Whether a gateway task not in `before` may have been running as a stream opened at `began`:
    it had first written by then, and had written within a scrape before. One that first wrote
    after may still have been serving, its collector having started late: `held_under` tells that."""
    return any(
        first <= began and last + SCRAPE_SECONDS >= began for task, (first, last) in tasks.items() if task not in before
    )


def replacement_may_hold(before: set[str], tasks: Mapping[str, tuple[float, float]], began: float) -> bool:
    """Whether the stream opened at `began` may be on a gateway task not in `before`, as the tasks'
    first and last writes show: one was writing as it opened (`replacement_at_open`), or first wrote
    within `COLLECTOR_LAG_SECONDS` after, so may have been serving before its collector first wrote.
    Then neither the stream's end nor its cut says what a rollout does to a stream on a drained task:
    the stream may have been on the replacement, while an old task went on running for another
    reason (the rollout waiting for a second replacement)."""
    return replacement_at_open(before, tasks, began) or any(
        began < first <= began + COLLECTOR_LAG_SECONDS for task, (first, _) in tasks.items() if task not in before
    )


def rollout_under(
    before: set[str], tasks: Mapping[str, tuple[float, float]], began: float, ended: float, now: float
) -> float | None:
    """When a rollout replaced the gateway's tasks under a stream open from `began` to `ended`, as
    the tasks' first and last writes show at `now`: the last write of the first task of `before`,
    the tasks writing as it opened, that stopped, having written since `began`, been silent for
    `QUIET_SECONDS` and stopped by `ended` and one scrape, while a task not in `before` had begun by
    `ended` (`rollout_at`) and still wrote when it stopped. `None` when none did: a task that
    appeared while those of `before` went on writing is no rollout. `None` as well when a task not
    in `before` was already writing as the stream opened (`replacement_at_open`): the stream may be
    on that one, which the rollout does not replace."""
    if replacement_at_open(before, tasks, began):
        return None
    latest = min(ended + SCRAPE_SECONDS, now - QUIET_SECONDS)
    stops = sorted(last for task, (_, last) in tasks.items() if task in before and began < last <= latest)
    for stop in stops:
        successor = rollout_at(before, {task: span for task, span in tasks.items() if span[1] >= stop})
        if successor is not None and successor <= ended:
            return stop
    return None


def drained_under(
    before: set[str], tasks: Mapping[str, tuple[float, float]], ended: float, now: float
) -> set[str] | None:
    """The tasks of `before` that the deployment may have drained under the stream that ended at
    `ended`, as the tasks' first and last writes show at `now`: each ran until the stream ended,
    having written within a scrape of its end, and stopped within `HOLDER_STOP_SECONDS` after,
    silent since for `QUIET_SECONDS`. A task that wrote later ran on. `None` while one that ran
    until the stream ended may still go either way."""
    ran = {task: last for task, (_, last) in tasks.items() if task in before and last >= ended - SCRAPE_SECONDS}
    if any(now - QUIET_SECONDS < last <= ended + HOLDER_STOP_SECONDS for last in ran.values()):
        return None
    return {task for task, last in ran.items() if last <= ended + HOLDER_STOP_SECONDS}


def settled_after(counts: Mapping[str, Sequence[tuple[float, float]]], released: float) -> set[str]:
    """The gateway tasks that settled a request after `released`, from each task's count of the
    requests it settled at the steps of a range query (`Pool.gateway_settled`): those whose last
    count is above the one at the last step by `released`, or above zero when it had none then. The
    count at a step is the last one written by then, so it holds no request settled after."""
    found: set[str] = set()
    for task, samples in counts.items():
        then = [value for at, value in samples if at <= released]
        if samples and samples[-1][1] > (then[-1] if then else 0.0):
            found.add(task)
    return found


def across_rollout(completed: bool, drained: set[str], settled: set[str], *, replaced: bool, may_hold: bool) -> bool:
    """The stream across a gateway deployment, as a number, once a task new to the job had replaced
    the old ones (`replaced`, `rollout_at`). True when it completed on a task the deployment drained:
    the only task that settled a request after its last byte (`settled_after`) is one of `drained`
    (`drained_under`). False when it was cut while exactly one old task stopped with it, and no
    replacement may have held it as it opened (`may_hold`, `replacement_may_hold`). ValueError
    otherwise: no deployment was seen under the stream, on a task it drained, and the run is not
    measured."""
    if replaced and completed and len(settled) == 1 and settled <= drained:
        return True
    if replaced and not completed and len(drained) == 1 and not may_hold:
        return False
    raise ValueError("no gateway deployment was seen under the stream, on a task it drained")


# The pool, as the workspace and the pool-state table show it.


@dataclass(frozen=True)
class Pool:
    """The pool under test, read from the workspace and the pool-state table."""

    name: str
    workspace: common.Workspace
    table: Any

    def note(self) -> ScalerNote | None:
        """The scaler's note, read strongly consistent."""
        item = self.table.get_item(Key={"pool": self.name, "record": NOTE_RECORD}, ConsistentRead=True).get("Item")
        return None if item is None else ScalerNote.from_item(item)

    def asked(self) -> int | None:
        note = self.note()
        return None if note is None else note.capacity_asked

    def readings(self, name: str, start: float, end: float) -> list[tuple[float, float]]:
        """The scaler's readings of the gauge `name` from `start` to `end`, each with the time it
        wrote it, in order. A range query repeats a reading at every step after it, and
        `timestamp()` says when it was written; they are two queries, between which a newer
        reading may become visible. So the times are read before and after the values, and a step
        is kept only when both agree: no newer reading arrived there while its value was read."""
        selector = f'{name}{{pool="{self.name}"}}'

        def stamps() -> dict[float, float]:
            found = self.workspace.read_series(f"timestamp({selector})", start=start, end=end, step=STEP_SECONDS)
            return {step: stamp for one in found for step, stamp in one.samples}

        first = stamps()
        values = {
            step: value
            for one in self.workspace.read_series(selector, start=start, end=end, step=STEP_SECONDS)
            for step, value in one.samples
        }
        again = stamps()
        return sorted(
            {(stamp, values[step]) for step, stamp in again.items() if first.get(step) == stamp and step in values}
        )

    def gauge(self, name: str) -> int | None:
        """The scaler's last reading of the gauge `name`. `None` when it wrote none in
        `FRESH_SECONDS`, or none since the run that wrote its note, the latest: that run's reading
        is not visible yet, and an older one may be from before the pool last changed (a zero from
        before a wake, read as the zero after it)."""
        note = self.note()
        end = time.time()
        found = self.readings(name, end - FRESH_SECONDS, end)
        if note is None or not found:
            return None
        stamp, value = found[-1]
        return round(value) if stamp >= max(end - FRESH_SECONDS, note.written_at) else None

    def warm_counts(self, start: float, end: float) -> list[tuple[float, float]]:
        """The scaler's readings of the warm count from `start` to `end`, each at the time it wrote
        it (`readings`)."""
        return self.readings(WARM_INSTANCES, start, end)

    def writes(self, start: float, end: float) -> dict[str, float]:
        """Each instance of the pool that wrote from `start` to `end`, with its last write."""
        query = f'max by (instance) (timestamp(up{{pool="{self.name}"}}))'
        return last_writes(self.workspace.read_series(query, start=start, end=end, step=STEP_SECONDS))

    def gateway_tasks(self, start: float, end: float) -> dict[str, tuple[float, float]]:
        """Each gateway task that wrote from `start` to `end`, with its first and last write."""
        query = f'max by (instance) (timestamp(up{{job="{GATEWAY_JOB}"}}))'
        return spans(self.workspace.read_series(query, start=start, end=end, step=STEP_SECONDS))

    def gateway_settled(self, start: float, end: float) -> dict[str, list[tuple[float, float]]]:
        """Each gateway task's count of the pool's requests it settled from `start` to `end`, at each
        step of the range query."""
        query = f'sum by (instance) ({SETTLED}{{job="{GATEWAY_JOB}",pool="{self.name}"}})'
        return {
            one.labels["instance"]: list(one.samples)
            for one in self.workspace.read_series(query, start=start, end=end, step=STEP_SECONDS)
            if one.labels.get("instance")
        }


# The requests.


def _request(model: str) -> loadgen.Request:
    """A short streamed Messages request with a fresh conversation."""
    session = loadgen.SyntheticSession(model, START_TOKENS, (), 64, secrets.randbits(48))
    return next(session.requests())[1]


@dataclass(frozen=True)
class Answer:
    """How a streamed request ended: whether it completed and, when a condition was watched,
    whether it held as the request's inference began (its first event, `INFERENCE_EVENT`, arrived;
    `None` when none did) and as its stream ended; and when its inference began, `None` when it did
    not."""

    completed: bool
    over_as_begun: bool | None = None
    over_as_ended: bool | None = None
    begun_at: float | None = None


async def _answer(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    content: bytes | AsyncIterator[bytes],
    transport: httpx.AsyncBaseTransport | None,
    over: Callable[[], bool] | None = None,
    begun: Callable[[], None] | None = None,
) -> Answer:
    """Sends `request` with `content` as its body, as the student whose key is given: whether it
    completed, answered 200, with its closing event and no error event, and, `over` given, whether
    it held as its inference began and as its stream ended (`Answer`). `begun` is called as its
    inference begins. Raises httpx's errors."""
    headers = {name: value for name, value in request.headers.items() if name.lower() != "content-length"}
    headers["authorization"] = f"Bearer {key}"
    reader, closed, failed = SseReader(), False, False
    as_begun: bool | None = None
    as_ended: bool | None = None
    begun_at: float | None = None
    async with (
        httpx.AsyncClient(base_url=endpoint, timeout=loadgen.TIMEOUT, transport=transport, trust_env=False) as http,
        http.stream(request.method, request.path, headers=headers, content=content) as answer,
    ):
        if answer.status_code != HTTPStatus.OK:
            return Answer(completed=False)
        async for chunk in answer.aiter_bytes():
            for event in reader.feed(chunk):
                if begun_at is None and event.name == INFERENCE_EVENT:
                    begun_at = time.time()
                    as_begun = None if over is None else over()
                    if begun is not None:
                        begun()
                closed = closed or event.name == CLOSING_EVENT
                failed = failed or event.name == ERROR_EVENT
        # Read as the stream's end arrives, before the connection is let go.
        as_ended = None if over is None else over()
    return Answer(closed and not failed, as_begun, as_ended, begun_at)


async def _completes(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    content: bytes | AsyncIterator[bytes],
    transport: httpx.AsyncBaseTransport | None,
) -> bool:
    """Sends `request` with `content` as its body, as the student whose key is given, and says
    whether it completed (`_answer`). Raises httpx's errors."""
    return (await _answer(endpoint, key, request, content, transport)).completed


async def slow_stream(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    seconds: float,
    *,
    until: Callable[[], Awaitable[bool]] | None = None,
    opened: Callable[[], None] | None = None,
    released: Callable[[], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Sends `request` as a held stream: its body but the last byte, then a byte of padding every
    `TRICKLE_SECONDS` for `seconds`, or until `until` says that the event the stream spans is over,
    then the last byte. `opened` is called once the stream is open, its first part sent, and
    `released` as its last byte goes. Says whether it completed (`_completes`)."""
    body = request.body
    if not body.endswith(b"}"):
        raise ValueError("a held stream's body is a JSON object")
    steps = max(1, math.ceil(seconds / TRICKLE_SECONDS))

    async def held() -> AsyncIterator[bytes]:
        yield body[:-1]
        if opened is not None:
            opened()
        for _ in range(steps):
            if until is not None and await until():
                break
            await sleep(seconds / steps)
            yield PADDING
        if released is not None:
            released()
        yield body[-1:]

    try:
        return await _completes(endpoint, key, request, held(), transport)
    except httpx.HTTPError:
        return False


async def wake(
    endpoint: str, key: str, model: str, bound: float, *, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """Sends one streamed request as the student whose key is given: whether it completed within
    `bound` seconds. A refusal, an error event, a cut connection or no end in time is a lost wake."""
    request = _request(model)
    try:
        async with asyncio.timeout(bound):
            return await _completes(endpoint, key, request, request.body, transport)
    except (TimeoutError, httpx.HTTPError):
        return False


@dataclass(frozen=True)
class HeldStream:
    """A session that sends one short stream after another, until `stop` is set."""

    request: loadgen.Request
    stop: asyncio.Event

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        while not self.stop.is_set():
            yield 0.0, self.request


async def _wait(condition: Callable[[], bool], seconds: float, what: str, every: float = POLL_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while not await asyncio.to_thread(condition):
        if time.monotonic() > deadline:
            raise TimeoutError(f"waited {seconds:.0f} seconds for {what}")
        await asyncio.sleep(every)


@dataclass
class Run:
    """One run of the spike against the pool, through the gateway at `endpoint`. `transport` and
    `sleep`, with which the job waits between two reads of the gateway's tasks, are for the tests:
    the job gives neither."""

    pool: Pool
    endpoint: str
    keys: Sequence[str]
    settings: config.Pool
    wakes: int
    deployment_hold_seconds: float
    transport: httpx.AsyncBaseTransport | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    departures: list[Departure] = field(default_factory=list, init=False)
    # Every departure `departure` returned, as it returned it: the relay reads the one it spanned.
    leaving: list[Departure] = field(default_factory=list, init=False)

    @property
    def student(self) -> str:
        """The canary student of the held streams and the wakes; the load runs as the others."""
        return self.keys[0]

    async def across_deployment(self) -> bool:
        """Holds a stream across a gateway deployment (`across_rollout`): whether it completed. The
        rollout is told from the gateway's tasks, which `Pool.gateway_tasks` reads (`rollout_under`),
        and the task that answered the stream from their counts of settled requests
        (`Pool.gateway_settled`)."""
        tasks = self.pool.gateway_tasks
        read_at = time.time()
        before = set(await asyncio.to_thread(tasks, read_at - FRESH_SECONDS, read_at))
        if not before:
            raise RuntimeError("no gateway task was writing before the stream was opened")
        # When the stream opened, its first part sent: a task of `before` that stopped earlier, while
        # the job was still reading them, was replaced before the stream and is no rollout under it.
        # And when its last byte went: a request settled after was sent after.
        opened: list[float] = []
        released: list[float] = []

        async def read() -> tuple[float, Mapping[str, tuple[float, float]]]:
            now = time.time()
            return now, await asyncio.to_thread(tasks, read_at - FRESH_SECONDS, now)

        async def rolled_out() -> bool:
            if not opened:
                return False
            now, current = await read()
            return rollout_under(before, current, opened[0], now, now) is not None

        start = time.strftime("%H:%M:%S UTC", time.gmtime())
        print(f"holding a stream for up to {self.deployment_hold_seconds:.0f} s from {start}: release the pipeline")
        request = _request(self.pool.name)
        completed = await slow_stream(
            self.endpoint,
            self.student,
            request,
            self.deployment_hold_seconds,
            until=rolled_out,
            opened=lambda: opened.append(time.time()),
            released=lambda: released.append(time.time()),
            transport=self.transport,
        )
        ended = time.time()
        # The old tasks that ran until the stream ended and stopped soon after: the task under it,
        # if the deployment drained it. Polled a bounded number of times.
        drained: set[str] = set()
        current: Mapping[str, tuple[float, float]] = {}
        for _ in range(math.ceil(HOLDER_WATCH_SECONDS / POLL_SECONDS) + 1):
            now, current = await read()
            found = drained_under(before, current, ended, now)
            if found is not None:
                drained = found
                break
            await self.sleep(POLL_SECONDS)
        first_new = rollout_at(before, current)
        replaced = first_new is not None and first_new <= ended + COLLECTOR_LAG_SECONDS
        settled: set[str] = set()
        if completed and drained and replaced:
            counts = await asyncio.to_thread(self.pool.gateway_settled, read_at - FRESH_SECONDS, time.time())
            settled = settled_after(counts, released[0] if released else ended)
        may_hold = not opened or replacement_may_hold(before, current, opened[0])
        try:
            survived = across_rollout(completed, drained, settled, replaced=replaced, may_hold=may_hold)
        except ValueError:
            message = (
                "no gateway deployment was seen under the stream, on a task it drained: release the pipeline"
                " while it is held, and not while a deployment is under way"
            )
            raise RuntimeError(message) from None
        print(f"the stream across the gateway deployment {'completed' if survived else 'did not complete'}")
        return survived

    async def two_instances(self) -> None:
        """Holds the load until the scaler asks for two instances and reads two in service."""
        streams = 2 * self.settings.target_concurrency
        load_keys = self.keys[1:]
        if len(load_keys) < math.ceil(streams / STREAMS_PER_STUDENT):
            raise ValueError("the load needs more canary students")
        stop = asyncio.Event()
        load = loadgen.Load(
            tuple(HeldStream(_request(self.pool.name), stop) for _ in range(streams)),
            sessions_per_student=STREAMS_PER_STUDENT,
        )
        held = asyncio.create_task(loadgen.replay(load, load_keys, self.endpoint, at_once=streams))

        def two() -> bool:
            if held.done():
                raise RuntimeError("the load ended before two instances served: a held stream was refused")
            return self.pool.asked() == INSTANCES and self.pool.gauge(INSTANCES_IN_SERVICE) == INSTANCES

        try:
            await _wait(two, SCALE_OUT_WAIT_SECONDS, "two instances in service")
        finally:
            stop.set()
            await held
        print("two instances in service: the load has stopped")

    async def departure(self, asked: int, wait: float, after: asyncio.Task[Departure] | None = None) -> Departure:
        """Waits until the scaler asks for fewer than `asked` instances, then until one of those
        writing at that moment has stopped, and returns its departure. The instance of the departure
        `after`, the one before, is not among them: it may still have been writing a moment before.

        The scaler writes its note on every run, so the note's `written_at` is when it began to ask
        for fewer only for the first note that does: the watch starts while the scaler still asks
        for `asked`, and reads the note every `NOTE_POLL_SECONDS`, well within one of its runs."""
        notes: list[ScalerNote] = []
        # When the job read the note that asks for fewer: the scaler had asked by then.
        seen: list[float] = []

        def fewer() -> bool:
            note = self.pool.note()
            seen[:] = [time.time()]
            notes[:] = [] if note is None else [note]
            return bool(notes) and notes[0].capacity_asked < asked

        await _wait(fewer, wait, f"the scaler to ask for fewer than {asked}", NOTE_POLL_SECONDS)
        asked_at = float(notes[0].written_at)
        now_asked = notes[0].capacity_asked
        print(f"the scaler asked for {now_asked} instance(s)")
        # The notes read from now on, which bound the warm count's readings of this departure.
        later: list[ScalerNote] = []

        def read_note() -> None:
            note = self.pool.note()
            if note is not None:
                later.append(note)

        earlier = None if after is None else (await after).instance
        before = await asyncio.to_thread(self.pool.writes, asked_at - FRESH_SECONDS, asked_at)
        writing = {
            instance for instance, at in before.items() if at >= asked_at - QUIET_SECONDS and instance != earlier
        }
        if not writing:
            raise RuntimeError("no instance of the pool was writing when the scaler asked for fewer")
        gone: dict[str, float] = {}

        def left() -> bool:
            read_note()
            now = time.time()
            gone.update(stopped(self.pool.writes(asked_at - FRESH_SECONDS, now), writing, now))
            return bool(gone) or now - asked_at > LEAVE_WAIT_SECONDS

        await _wait(left, LEAVE_WAIT_SECONDS + 2 * QUIET_SECONDS, "an instance to stop")
        now = time.time()
        if not gone:
            print(f"no instance stopped in {LEAVE_WAIT_SECONDS} s: the departure is not measured")
            return self._left(Departure(None, now - asked_at, asked_at=asked_at, seen_at=seen[0]))
        instance = min(gone, key=gone.__getitem__)
        seconds = leave_seconds(asked_at, gone[instance])
        since = await asyncio.to_thread(self.pool.writes, asked_at, now)
        samples = await asyncio.to_thread(self.pool.warm_counts, asked_at - FRESH_SECONDS, now)
        # A note read after the readings bounds them all: a later decision is in it or after them.
        await asyncio.to_thread(read_note)
        bound = warm_bound(now_asked, later, asked_at)
        samples = [(moment, value) for moment, value in samples if moment < bound]
        warm = warm_around(samples, asked_at, gone[instance] + SCRAPE_SECONDS)
        # An instance new to the pool is one that began writing after the reading the path is told
        # from, which may be an earlier run's (`decided_on`).
        decided = decided_on(samples, asked_at)
        read_at = asked_at if decided is None else min(decided[0], asked_at)
        known = (
            before
            if read_at >= asked_at
            else await asyncio.to_thread(self.pool.writes, asked_at - FRESH_SECONDS, read_at)
        )
        newcomer = bool((set(since) | set(before)) - set(known))
        path = None if warm is None else path_of(*warm, newcomer=newcomer)
        if straddles(asked_at, seen[0], gone[instance]):
            # The scaler's run may have asked late in it: the time is not told against the limit.
            print(f"an instance left in {gone[instance] - seen[0]:.0f} to {seconds:.0f} s: not told")
            path = None
        print(f"an instance left in {seconds:.0f} s at most, path {path or 'not told'}, warm {warm or 'not read'}")
        stop = asked_at + seconds
        return self._left(Departure(path, seconds, instance, asked_at, stop, seen[0], gone[instance]))

    def _left(self, departure: Departure) -> Departure:
        self.leaving.append(departure)
        return departure

    async def relay(self, over: Callable[[], bool]) -> bool | None:
        """The stream across a scale-in: `RELAY_STREAMS` chains, side by side, of whole streamed
        requests, each sent once the one before it in its chain has ended, until `over` says the scale-in is over, which is when the job has seen the first
        departure it watches from now over (`departure`). Whether the streams in flight while that
        instance left completed (`across_scale_in`): `None` when none was, or no stop was seen, and
        it is not measured. RuntimeError when the scale-in is not over after
        `SCALE_IN_HOLD_SECONDS`."""
        deadline = time.monotonic() + SCALE_IN_HOLD_SECONDS
        watched = len(self.leaving)
        streams: list[Streamed] = []

        # Each chain starts once the one before has a stream in flight, so that their streams do
        # not all end, and the next ones all start, together: or once that stream has ended, or
        # that chain has, so that none waits for good.
        in_flight = [asyncio.Event() for _ in range(RELAY_STREAMS)]

        async def chain(index: int) -> None:
            try:
                if index:
                    await in_flight[index - 1].wait()
                while not over():
                    request = _request(self.pool.name)
                    sent_at = time.time()
                    try:
                        answer = await _answer(
                            self.endpoint,
                            self.student,
                            request,
                            request.body,
                            self.transport,
                            over,
                            in_flight[index].set,
                        )
                    except httpx.HTTPError:
                        answer = Answer(completed=False)
                    in_flight[index].set()
                    began = sent_at if answer.begun_at is None else answer.begun_at
                    streams.append(Streamed(began, time.time(), answer))
                    if not answer.completed:
                        print(f"stream {len(streams)} of the relay did not complete")
                        # Not sent again at once: a refusal comes back fast.
                        await asyncio.sleep(TRICKLE_SECONDS)
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"the scale-in was not over after {SCALE_IN_HOLD_SECONDS} s")
            finally:
                in_flight[index].set()

        try:
            async with asyncio.TaskGroup() as chains:
                for index in range(RELAY_STREAMS):
                    chains.create_task(chain(index))
        except* RuntimeError as failed:
            raise failed.exceptions[0] from None
        departure = self.leaving[watched] if len(self.leaving) > watched else None
        completed = across_scale_in(streams, departure)
        if completed is None:
            print("no stream was in flight while the instance left: the stream across it is not measured")
        else:
            print(f"the streams in flight while the instance left {'completed' if completed else 'did not complete'}")
        return completed

    async def cycle(self, *, scale_in_stream: bool) -> bool | None:
        """Two instances, then both departures. With `scale_in_stream`, the stream across a scale-in
        (`relay`) runs from while the scaler asks for two until the first departure is over: whether
        it completed, `None` when no stream spanned the scale-in or none was held."""
        await self.two_instances()
        # Both departures are watched from now, while the scaler still asks for two: each sees the
        # first note that asks for fewer.
        idle = self.settings.idle_minutes * 60
        first = asyncio.create_task(self.departure(INSTANCES, SCALE_IN_WAIT_SECONDS))
        last = asyncio.create_task(self.departure(1, SCALE_IN_WAIT_SECONDS + LEAVE_WAIT_SECONDS + idle, first))
        streaming = asyncio.create_task(self.relay(first.done)) if scale_in_stream else None
        completed = None
        try:
            self.departures.append(await first)
        finally:
            if streaming is not None:
                completed = await streaming
        self.departures.append(await last)
        return completed

    def _at_zero(self) -> bool:
        return self.pool.asked() == 0 and self.pool.gauge(INSTANCES_IN_SERVICE) == 0

    async def send_wakes(self) -> tuple[int, int]:
        """Sends `wakes` requests, each at the moment the pool reaches zero after its idle period:
        the wakes sent and lost. Before each, the pool is brought back if the last request did not
        complete, by requests that are not counted."""
        bound = self.settings.hold_seconds + WAKE_MARGIN_SECONDS
        wait = self.settings.idle_minutes * 60 + SCALE_IN_WAIT_SECONDS
        awake, lost = False, 0
        for sent in range(1, self.wakes + 1):
            for _ in range(RECOVERIES):
                if awake:
                    break
                awake = await wake(self.endpoint, self.student, self.pool.name, bound, transport=self.transport)
            if not awake:
                print(f"the pool could not be brought back: {sent - 1} wake(s) sent")
                return sent - 1, lost
            await _wait(lambda: self.pool.asked() != 0, SCALE_OUT_WAIT_SECONDS, "the pool to wake", NOTE_POLL_SECONDS)
            await _wait(self._at_zero, wait, "the pool to reach zero", NOTE_POLL_SECONDS)
            awake = await wake(self.endpoint, self.student, self.pool.name, bound, transport=self.transport)
            lost += not awake
            print(f"wake {sent}: {'answered' if awake else 'lost'}")
        return self.wakes, lost

    async def measure(self) -> dict[str, int | float]:
        deployment = await self.across_deployment()
        # A cycle again while a path is missing, or no stream spanned a scale-in.
        scale_in: bool | None = None
        for _ in range(CYCLES):
            held = await self.cycle(scale_in_stream=scale_in is None)
            scale_in = held if scale_in is None else scale_in
            if scale_in is not None and {departure.path for departure in self.departures} >= set(Path):
                break
        if scale_in is None:
            raise RuntimeError(f"no stream spanned a scale-in in {CYCLES} cycles: the run is not measured")
        print(f"the stream across the scale-in {'completed' if scale_in else 'did not complete'}")
        streams = {
            Number.STREAM_COMPLETED_SCALE_IN: bool(scale_in),
            Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT: deployment,
        }
        # Checked before the wakes, which take the pool's idle period each.
        numbers_of(self.departures, streams, 0, 0)
        sent, lost = await self.send_wakes()
        return numbers_of(self.departures, streams, sent, lost)


def run_label(environ: Mapping[str, str]) -> str:
    """This run's own label: the job's (`common.run_label`), then the build's number, or random hex
    where the environment gives none. ValueError for a job's label of another shape."""
    build = environ.get(BUILD_NUMBER_VARIABLE, "")
    suffix = build if build.isascii() and build.isdigit() else secrets.token_hex(4)
    return f"{common.run_label(JOB, environ)}-{suffix}"


def main(environ: Mapping[str, str] = os.environ) -> None:
    """Runs the spike with the job's environment and credentials, and posts its numbers under this
    run's own label (`run_label`). ValueError, the run not measured, for a pool that is not a mock."""

    def measure() -> dict[str, int | float]:
        name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
        (settings,) = [pool for pool in config.read_ssm().pools if pool.name == name]
        if settings.engine != MOCK_ENGINE:
            raise ValueError("spike S7 runs on a mock pool only")
        wakes = int(environ.get(WAKES_VARIABLE, DEFAULT_WAKES))
        hold = float(environ.get(DEPLOYMENT_HOLD_VARIABLE, DEFAULT_DEPLOYMENT_HOLD_SECONDS))
        if wakes < 1 or hold <= 0:
            raise ValueError("the job sends one wake or more, and holds its stream for some time")
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        table = session.resource("dynamodb").Table(environ[POOL_STATE_TABLE_VARIABLE])
        keys = common.canary_keys().students
        run = Run(Pool(name, workspace, table), environ[ENDPOINT_VARIABLE], keys, settings, wakes, hold)
        return asyncio.run(run.measure())

    label = run_label(environ)
    print(f"run label {label}")
    common.run_spike(SPIKE, JOB, measure, environ={**environ, common.RUN_LABEL_VARIABLE: label})


if __name__ == "__main__":
    main()
