"""Spike S7: what scale-in and a gateway deployment do to a request, on the mock pool (system design,
sections 5.1, 8.5 and 12).

AWS's documentation does not settle whether a group waits the whole deregistration delay for a
target that has no connections, nor whether it drains at all before stopping an instance into the
warm pool (evidence E13). This job runs the scenario on the mock pool and **records what it
observed, with times, and judges nothing**: the pass condition (an idle instance gone within a
minute or two, in-flight requests finished, no wake lost) is read later, from the first real run's
lines, by an issue written then (section 12).

**What it records.** One JSON object per line on standard output, which the spike jobs' log group
keeps (`Recorder`). Each has `at`, the UTC time to the millisecond, and `kind`; every other time in
it is a UTC time in the same form, or null when it was not seen. An instance is named by its order
of appearance in the run, `instance-1`, `instance-2`, ... (`Names`), never by its AWS identifier.
No key and no text that AWS or the network returned is written: an error is recorded as one of the
fixed kinds of `ErrorKind`, an answer refused as its status number, and a gateway's error event as
its cause only when that is one of the gateway's own (`rhi_common.causes.Cause`).

**What it does**, through the gateway only, as canary students (`common.canary_keys`), and reading
the pool only from the workspace and the pool-state table (`Pool`). It never moves the pool: the
scaler alone does, by load and by idleness (section 5.1). The steps, in order (`Run.run`):

1. **The stream across a gateway deployment** (`Run.across_deployment`). It writes `hold_started`,
   with how long it holds, and holds one slow stream (`slow_stream`) for that long: the owner
   releases the pipeline again meanwhile, and the job starts no deployment. The gateway reads a
   request's whole body before it forwards it, so the job sends the body but its last byte, then
   one byte of white space every `TRICKLE_SECONDS`, which JSON allows before the closing brace, and
   the last byte when the hold is over: the stream's connection stays on the gateway task that took
   it while the deployment replaces that task. Then one `stream` line.
2. **The scale-in paths** (`Run.cycle`). It holds twice the pool's `target_concurrency` of short
   streams until the scaler asks for two instances and reads two in service (`load_started`,
   `instances`, `load_stopped`), then lets the pool go idle and watches the scaler take it to none,
   from two to one and then, after its idle period, to none, or from two to none at once. Each
   reading is kept apart and read again at every poll, so that a gap in the workspace loses
   nothing it shows later:
   - `scale_in_asked`: each note that asks for fewer than the last (a decision), with its
     `written_at`, when it was read and how many fewer;
   - `out_of_service`: when the job first reads the scaler's in-service gauge, as fresh as the
     note, below what it read before; each instance fewer is one owed to the decisions, in order;
   - `gone`: when the job first reads an instance silent for `QUIET_SECONDS` that was writing as
     the watch began or since, the instance stopped, terminated or stopped into the warm pool. Its
     node's collector writes the series `up` of each of its scrape targets every `SCRAPE_SECONDS`
     until the instance shuts down. Each stop is one departure; the departures, in the order their
     instances stopped, are given to the decisions in order. An instance that returns from the warm
     pool and stops again leaves again;
   - `gone_withdrawn`: a stop that a later read contradicts, a write within `QUIET_SECONDS` after it
     that the workspace showed late: the instance never stopped, and the departures are given to
     the decisions again;
   - `scale_in`: each departure whole, with its path (`path_of`), once every decision has its
     instances out of service and gone and the warm count has been read `WARM_SETTLE_SECONDS`
     past the last stop.
   A cycle in which both paths were not seen is run again, up to `CYCLES` times; once both were,
   the job goes on to the wakes.
3. **The stream across a scale-in**, in the first cycle. As the load stops, before the scaler acts,
   it opens one slow stream, as across the deployment, and holds its body open at the gateway
   through the whole scale-in: the scaler's first decision, the departing instance out of service
   and the instance gone. The gateway reads a body whole before it counts the request as demand or
   forwards it, so the held stream keeps no instance busy and delays no scale-in; and the scaler
   removes only instances that report nothing running (section 5.1), so a request on the mock pool
   can be in flight across a scale-in only there. Once the job reads every instance of the first
   decision gone, it sends the last byte and reads the answer to its end: whether the request held
   across the scale-in was then served, with an error event or cut, is what the line records. When
   that decision asked for none, the stream is held to the cycle's end instead, so that it wakes no
   instance while the paths are told. Its `stream` line holds its times, among them `body_sent_at`,
   when it was forwarded, and its end, with the first decision's number; the departures' lines hold
   theirs, so a reader places one against the other.
4. **The wakes** (`Run.send_wakes`). As many times as `RHI_S7_WAKES` gives, it waits for a fresh
   transition of the pool to zero: a note that asks for an instance after the last zero it used,
   then a note asking for none and an in-service reading of zero as fresh as it, which the scaler
   writes only after the pool's idle period. It then sends one request at once, and writes a `wake`
   line: the pool's zero, the request, the scaler's next note asking for capacity, and the engine's
   first event, or `none_within_hold` when none came within the pool's `hold_seconds`. When the
   pool is at zero with no transition to use, a lost wake that left it asleep among them, it sends
   one request to wake it first (`rewake`), which is not a wake: no two wakes share an idle period.

**A stream's line** (`Read`): when the request was sent, its first part, and when its body was sent
whole; when its first event and its last arrived, and the events it received. The gateway writes a
keep-alive, `ping` on Messages, every few seconds while it holds a stream for a pool that is waking
(`rhi_gateway.hold`), and ends a hold with an error event: the keep-alives are counted among the
events and as `pings`, and `first_engine_event_at` is the first event that is neither, the
engine's first, which says the request reached a pool that served it. It ended (`Ending`) with the format's
closing event, `message_stop`; with an error event; with the connection closed, the body ending
without either or the read failing; with a status other than 200; or, for a wake, at the job's own
bound, the pool's hold and `WAKE_MARGIN_SECONDS`.

**Which path a departure took** (`path_of`) is told only when the readings agree on it within that
one decision, and is `unknown`, with the reason, otherwise. The in-service and the warm gauges are
pool totals, so the departure's instance is the one whose collector stopped, the in-service
reading must have dropped, and the warm count is compared between the reading the scaler decided on
(`decided_on`) and every reading after it up to the scaler's next decision (`warm_bound`), that
decision's own reading included: the scaler read it before it acted (`in_window`). The path is told once `WARM_SETTLE_SECONDS` have passed
since the decision's last stop, or at the next decision, whichever comes first, from every reading
of the window the workspace then shows. An instance stopped into the warm pool may be counted there
a while after its collector falls silent, so the count may rise late but never fall, not even
before the instance stopped (a slot emptied then may be refilled by it): once every
instance of the decision has stopped, ending as many more as left, `warm_pool`; the same all along
a window that ran at least `WARM_SETTLE_SECONDS`, its readings reaching its end, `terminated`. A
new instance writing, a decision whose instances the count does not tell apart, no reading on
either side, readings that end early, a window the next decision cut short, or readings that
differ, leave it `unknown`. The readings cover the window (`covers`) when they reach its end, the
settle time or the next decision's run, and no two that follow each other in it are further apart
than `COVER_SECONDS`, one run of the scaler and a step: a later reading the workspace has not shown
yet may hold the rise. A reading missing from the workspace, or readings that do not cover the
window, are read again for `FRESH_SECONDS` before the path is told without them.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s7_drain`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, reached over HTTPS (`s4_weights.gateway_address`) |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_S7_WAKES` | The wakes it sends; `DEFAULT_WAKES` unless given |
| `RHI_S7_DEPLOYMENT_HOLD_SECONDS` | How long the stream across the gateway deployment is held; `DEFAULT_DEPLOYMENT_HOLD_SECONDS` unless given |

The pool is the one named `mock` (`POOL`), and the job refuses it when its engine is not the mock.
Its `target_concurrency`, `hold_seconds` and `idle_minutes` come from the deployed configuration
(`rhi.config.read_ssm`), and the region from CodeBuild's `AWS_REGION`. Run it while nothing else
sends to the pool: other load keeps instances from going idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import math
import os
import secrets
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol

import boto3
import botocore.exceptions
import httpx

import common
import loadgen
import s4_weights
from rhi import config
from rhi_admin.roster import DEFAULT_CONCURRENCY_LIMIT
from rhi_common.causes import Cause
from rhi_common.records import NOTE_RECORD, ScalerNote
from rhi_gateway.sse import SseReader
from rhi_scaler.metrics import INSTANCES_IN_SERVICE, WARM_INSTANCES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable, Mapping, Sequence

JOB = "s7_drain"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_STATE_TABLE_VARIABLE = "RHI_SPIKE_POOL_STATE_TABLE"
WAKES_VARIABLE = "RHI_S7_WAKES"
DEPLOYMENT_HOLD_VARIABLE = "RHI_S7_DEPLOYMENT_HOLD_SECONDS"
# The pool the job runs on, the mock pool, and the engine it must have (`rhi_common.pool_config`).
POOL = "mock"
MOCK_ENGINE = "mock"
DEFAULT_WAKES = 3
# Under the gateway target group's 1800 seconds of deregistration delay (section 8.5), and long
# enough for a release of the pipeline to reach the gateway once the owner has started it.
DEFAULT_DEPLOYMENT_HOLD_SECONDS = 1500

# Seconds. The collectors scrape, and write `up`, every 15 seconds; an instance whose last write is
# this old has stopped.
SCRAPE_SECONDS = 15
QUIET_SECONDS = 90
# Seconds. The scaler runs once a minute; the job reads its note and gauges more often.
NOTE_POLL_SECONDS = 5
POLL_SECONDS = 15
# Seconds between two steps of a range query.
STEP_SECONDS = 15
# Seconds a gauge's last sample may be old and still be the scaler's last reading: one run's period,
# its 50-second timeout and a margin (as in `spikes/s8_stickiness.py`).
FRESH_SECONDS = 120
# Seconds. The scaler's note keeps its run's time in whole seconds, its gauges in milliseconds: the
# readings of the run that wrote a note are stamped within this second after its `written_at`.
NOTE_SECOND = 1
# Seconds. The scaler adds an instance after 60 seconds above target, and the mock starts in about
# a minute; it removes one after 10 minutes of load that one fits, and 15 since the scale-out, and
# the last after the pool's idle period (section 5.1).
SCALE_OUT_WAIT_SECONDS = 20 * 60
SCALE_IN_WAIT_SECONDS = 30 * 60
# Seconds the job waits for an instance to stop once the scaler asked for fewer.
LEAVE_WAIT_SECONDS = 15 * 60
# Seconds after an instance stops over which the warm count is read at least before its path is
# told: an instance stopped into the warm pool may be counted there some time after its collector
# falls silent.
WARM_SETTLE_SECONDS = 5 * 60
# Seconds: the most two warm readings that follow each other may be apart and still cover the time
# between them, one run of the scaler and a step of the query; otherwise the workspace has not shown
# a reading between them yet.
COVER_SECONDS = 60 + STEP_SECONDS
# Seconds a reading may take to show in the workspace once the scaler has written it.
INGEST_SECONDS = 30
# The instances the job brings the pool to (`max_instances` of the mock pool), and how many times
# it does, to see both paths.
INSTANCES = 2
CYCLES = 2
# Seconds between two bytes of a slow stream's body: the edge's idle timeout is far longer.
TRICKLE_SECONDS = 5
PADDING = b" "
# Seconds a wake's request is read beyond the pool's hold.
WAKE_MARGIN_SECONDS = 120
# The streams one student of the load holds: one under a student's limit (as in S8).
STREAMS_PER_STUDENT = DEFAULT_CONCURRENCY_LIMIT - 1
START_TOKENS = 200
CLOSING_EVENT = "message_stop"
ERROR_EVENT = "error"
# The gateway's keep-alive on a Messages stream it holds (`rhi_gateway.hold`).
PING_EVENT = "ping"


# The clock and the lines.


class Clock(Protocol):
    """What the job reads the time from and waits on: the system's, or a test's."""

    def now(self) -> float:
        """Seconds since the epoch."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Waits `seconds`."""
        ...


class SystemClock:
    """The system's clock."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def utc(moment: float | None) -> str | None:
    """`moment`, seconds since the epoch, as a UTC time to the millisecond; None stays None."""
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _print_line(line: str) -> None:
    print(line, flush=True)


@dataclass(frozen=True)
class Recorder:
    """Writes the job's lines: one JSON object each, with its time, `at`, and its `kind`."""

    clock: Clock
    write: Callable[[str], None] = _print_line

    def line(self, kind: str, **fields: object) -> None:
        self.write(json.dumps({"at": utc(self.clock.now()), "kind": kind, **fields}))


@dataclass
class Names:
    """The name of each instance in the lines: `instance-<n>`, by order of first appearance."""

    names: dict[str, str] = field(default_factory=dict)

    def __call__(self, instance: str) -> str:
        if instance not in self.names:
            self.names[instance] = f"instance-{len(self.names) + 1}"
        return self.names[instance]

    def ordered(self, instances: Iterable[str]) -> list[str]:
        """The names of `instances`, in their order of appearance."""
        order = list(self.names)
        return [self(instance) for instance in sorted(instances, key=lambda one: order.index(one) if one in order else len(order))]


class ErrorKind(StrEnum):
    """What the lines say of an error: a fixed kind, never the error's text."""

    TIMEOUT = "timeout"
    CONNECT_ERROR = "connect_error"
    READ_ERROR = "read_error"
    PROTOCOL_ERROR = "protocol_error"
    HTTP_ERROR = "http_error"
    AWS_ERROR = "aws_error"
    INVALID = "invalid"
    UNEXPECTED = "unexpected"


def error_kind(error: BaseException) -> ErrorKind:
    """The kind of `error`. An error of the AWS SDK, whatever its class, is `aws_error`: its class
    may be named after a code AWS returned."""
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ErrorKind.TIMEOUT
    if isinstance(error, (httpx.ConnectError, urllib.error.URLError)) and not isinstance(error, urllib.error.HTTPError):
        return ErrorKind.CONNECT_ERROR
    if isinstance(error, httpx.ReadError):
        return ErrorKind.READ_ERROR
    if isinstance(error, httpx.RemoteProtocolError):
        return ErrorKind.PROTOCOL_ERROR
    if isinstance(error, (httpx.HTTPError, urllib.error.HTTPError)):
        return ErrorKind.HTTP_ERROR
    if isinstance(error, (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError)):
        return ErrorKind.AWS_ERROR
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return ErrorKind.INVALID
    return ErrorKind.UNEXPECTED


# The pool, as the workspace and the pool-state table show it.


class SeriesReader(Protocol):
    """What reads the workspace: `common.Workspace`, or a test's."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]: ...


@dataclass(frozen=True)
class Pool:
    """The pool under test, read from the workspace and the pool-state table. Every method calls
    the AWS SDK or the workspace and may raise: the run reads it through `Run.read`."""

    name: str
    workspace: SeriesReader
    table: Any

    def note(self) -> ScalerNote | None:
        """The scaler's note, read strongly consistent."""
        item = self.table.get_item(Key={"pool": self.name, "record": NOTE_RECORD}, ConsistentRead=True).get("Item")
        return None if item is None else ScalerNote.from_item(item)

    def readings(self, name: str, start: float, end: float) -> list[tuple[float, float]]:
        """The scaler's readings of the gauge `name` from `start` to `end`, each with the time it
        wrote it, in order. A range query repeats a reading at every step after it, and
        `timestamp()` says when it was written; they are two queries, between which a newer reading
        may become visible. So the times are read before and after the values, and a step is kept
        only when both agree."""
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

    def gauge(self, name: str, now: float, note: ScalerNote) -> tuple[float, int] | None:
        """The scaler's last reading of the gauge `name` at `now`, with when it wrote it. None when
        it wrote none in `FRESH_SECONDS`, or none since the run that wrote `note`: an older one may
        be from before the pool last changed."""
        found = self.readings(name, now - FRESH_SECONDS, now)
        if not found or found[-1][0] < max(now - FRESH_SECONDS, note.written_at):
            return None
        return found[-1][0], round(found[-1][1])

    def writes(self, start: float, end: float) -> dict[str, set[float]]:
        """Each instance of the pool that wrote from `start` to `end`, with the times of the writes
        read in that time: at each step, the time of the collector's last write."""
        query = f'max by (instance) (timestamp(up{{pool="{self.name}"}}))'
        found: dict[str, set[float]] = {}
        for one in self.workspace.read_series(query, start=start, end=end, step=STEP_SECONDS):
            instance = one.labels.get("instance")
            if instance and one.samples:
                found.setdefault(instance, set()).update(one.values())
        return found


# The paths.


class Path(StrEnum):
    """How an instance left service, as the job read it."""

    TERMINATED = "terminated"
    WARM_POOL = "warm_pool"
    UNKNOWN = "unknown"


class Unknown(StrEnum):
    """Why a departure's path is `unknown`."""

    STOPPED_BEFORE_ASK = "stopped_before_ask"
    NOT_READ_OUT_OF_SERVICE = "not_read_out_of_service"
    NOT_ASKED = "not_asked"
    ANOTHER_INSTANCE_STOPPED = "another_instance_stopped"
    NEW_INSTANCE = "new_instance"
    NO_DECISION_READING = "no_decision_reading"
    NO_READING_AFTER_STOP = "no_reading_after_stop"
    NOT_SETTLED = "not_settled"
    WARM_COUNT_DIFFERS = "warm_count_differs"
    WARM_READINGS_END_EARLY = "warm_readings_end_early"


# The reasons that say a reading was missing: the job reads the warm count again before it gives one.
MISSING = frozenset({Unknown.NO_DECISION_READING, Unknown.NO_READING_AFTER_STOP, Unknown.WARM_READINGS_END_EARLY})


def decided_on(samples: Sequence[tuple[float, float]], at: float) -> tuple[float, int] | None:
    """The warm count the scaler decided on in its run of `at`, a note's `written_at`, and when it
    was read: that run's own reading, stamped within `NOTE_SECOND` of `at`. When that run's reading
    was not written, the last one before stands for it only if it read zero: a warm instance read
    then may have left the warm pool before the decision and the departing one taken its place, but
    an empty warm pool has none to lose. None otherwise."""
    own = [(moment, value) for moment, value in samples if at <= moment < at + NOTE_SECOND]
    if own:
        return own[-1][0], round(own[-1][1])
    earlier = [(moment, value) for moment, value in samples if moment < at]
    if earlier and round(earlier[-1][1]) == 0:
        return earlier[-1][0], 0
    return None


def warm_bound(asked: int, notes: Sequence[ScalerNote], decided_at: float) -> tuple[float, float | None]:
    """The time before which the scaler's readings of the warm count belong to the departure it
    decided at `decided_at`, asking for `asked`, from the notes the job read since, in order; and
    the `written_at` of the next decision, a note that asks for another number, or None. The next
    decision acts on the pool too, so the readings end with the last run seen to ask for `asked`
    before it; with none, they do not end. The next decision's own reading was made before it acted
    (`in_window`)."""
    confirmed = decided_at
    for note in notes:
        if note.capacity_asked != asked:
            return confirmed + NOTE_SECOND, float(note.written_at)
        confirmed = max(confirmed, float(note.written_at))
    return math.inf, None


def covers(readings: Sequence[tuple[float, float]], *, since: float, until: float) -> bool:
    """Whether the warm readings, each with its time, cover the window from `since`, the reading the
    scaler decided on, to `until`: one of them made at `until` or after, and none of those from
    `since` on further from the next than `COVER_SECONDS`."""
    moments = [moment for moment, _ in readings if moment >= since]
    if not moments or moments[-1] < until:
        return False
    return all(later - earlier <= COVER_SECONDS for earlier, later in itertools.pairwise(moments))


def in_window(
    samples: Sequence[tuple[float, float]], end: float, next_at: float | None
) -> list[tuple[float, float]]:
    """The warm readings that belong to a decision whose readings end at `end`: those before it,
    and the next decision's own reading, made at `next_at` before that run acted, when no run came
    between the two that the job did not read (no reading between them): such a run could have been
    the next decision."""
    kept = [(moment, value) for moment, value in samples if moment < end]
    if next_at is not None and not any(end <= moment < next_at for moment, _ in samples):
        kept += [(moment, value) for moment, value in samples if next_at <= moment < next_at + NOTE_SECOND]
    return kept


@dataclass(frozen=True)
class Told:
    """A decision's path, why it is unknown when it is, and the warm counts it was told from."""

    path: Path
    unknown: Unknown | None = None
    warm_before: int | None = None
    warm_after: tuple[int, ...] = ()


def path_of(
    *,
    asked_at: float,
    stops: Sequence[float],
    leaving: int,
    out_of_service: bool,
    warm: Sequence[tuple[float, float]],
    end: float,
    settled: bool,
    newcomer: bool,
    next_at: float | None = None,
) -> Told:
    """The path of the instances that left on the decision the scaler made at `asked_at`, asking for
    `leaving` fewer, whose collectors last wrote at `stops`, from the warm readings `warm`, each
    with its time, read up to `end`, and the next decision's own reading, made at `next_at`
    (`in_window`): `settled` when `end` is `WARM_SETTLE_SECONDS` after the last stop rather than
    another decision's, and has passed. `out_of_service` says whether the in-service reading
    dropped, `newcomer` whether an instance new to the pool wrote meanwhile.

    The warm count is a pool total that may rise some time after an instance stops. From the
    decision's reading, those made before the instances stopped included, it must never fall and
    never pass `leaving` more: a slot that empties and fills again may have been filled by the
    departing instance or by a new one, so the readings cannot tell. Once every instance has
    stopped, a last reading `leaving` more is `warm_pool`. Short of that, the readings must cover
    the window (`covers`) up to `WARM_SETTLE_SECONDS` past the last stop, or up to the last run that
    asked for as many before the next decision when `end` comes first, or the workspace has not
    shown the rest yet. Then every reading since the decision's the same, over a settled window, is
    `terminated`. Anything else is `unknown`, with its reason: the same count over a window another
    decision cut short cannot tell a terminated instance from a late return, and a count between the
    two, after more than one stop, cannot tell which went where."""
    if not stops or min(stops) < asked_at:
        return Told(Path.UNKNOWN, Unknown.STOPPED_BEFORE_ASK)
    if not out_of_service:
        return Told(Path.UNKNOWN, Unknown.NOT_READ_OUT_OF_SERVICE)
    if newcomer:
        return Told(Path.UNKNOWN, Unknown.NEW_INSTANCE)
    readings = in_window(warm, end, next_at)
    decided = decided_on(readings, asked_at)
    if decided is None:
        return Told(Path.UNKNOWN, Unknown.NO_DECISION_READING)
    before = decided[1]
    # Every count from the decision's reading on, those made while the instance still wrote
    # included: a warm slot emptied then may have been refilled by the departing instance.
    counts = tuple(round(value) for moment, value in readings if moment >= decided[0])
    after = tuple(round(value) for moment, value in readings if moment >= min(stops) + SCRAPE_SECONDS)
    last = [round(value) for moment, value in readings if moment >= max(stops) + SCRAPE_SECONDS]
    if not after or not last:
        return Told(Path.UNKNOWN, Unknown.NO_READING_AFTER_STOP, before)
    steady = all(earlier <= later <= before + leaving for earlier, later in itertools.pairwise((before, *counts)))
    if steady and last[-1] == before + leaving:
        return Told(Path.WARM_POOL, None, before, after)
    if not covers(readings, since=decided[0], until=min(max(stops) + WARM_SETTLE_SECONDS, end - NOTE_SECOND)):
        return Told(Path.UNKNOWN, Unknown.WARM_READINGS_END_EARLY, before, after)
    if set(counts) == {before} and settled:
        return Told(Path.TERMINATED, None, before, after)
    if len(stops) > 1 or leaving > 1:
        return Told(Path.UNKNOWN, Unknown.ANOTHER_INSTANCE_STOPPED, before, after)
    if set(counts) == {before}:
        return Told(Path.UNKNOWN, Unknown.NOT_SETTLED, before, after)
    return Told(Path.UNKNOWN, Unknown.WARM_COUNT_DIFFERS, before, after)


@dataclass
class Departure:
    """One instance leaving service, as the job reads it: its collector silent for `QUIET_SECONDS`.
    `instance` is its AWS identifier, never written in a line."""

    number: int
    instance: str
    last_write: float
    gone_read_at: float
    decision: Decision | None = None
    path: Path | None = None


@dataclass
class Decision:
    """One note of the scaler asking for `fewer` instances fewer, and what the job read of it: the
    notes read since, the in-service drops it read for it (when each was read, and the reading's
    time), and the departures it read for it, in order."""

    number: int
    asked: int
    fewer: int
    asked_at: float
    asked_read_at: float
    in_service_before: int
    notes: list[ScalerNote] = field(default_factory=list)
    drops: list[tuple[float, float]] = field(default_factory=list)
    departures: list[Departure] = field(default_factory=list)

    def window(self, now: float) -> tuple[float, float, bool]:
        """Up to when the warm readings belong to this decision's departures, the scaler's next
        decision (`warm_bound`); when the job may tell their path, `WARM_SETTLE_SECONDS` past the
        last stop or that next decision, whichever comes first; and whether the window ran its
        whole `WARM_SETTLE_SECONDS` and the readings of it can have reached the workspace
        (`path_of`). A reading made after the settle time, before the next decision, still belongs
        to this one: a return to the warm pool may be counted late."""
        if not self.departures:
            return math.inf, math.inf, False
        settle = max(departure.last_write for departure in self.departures) + WARM_SETTLE_SECONDS
        bound, _ = warm_bound(self.asked, self.notes, self.asked_at)
        return bound, min(settle, bound), settle <= bound and now >= settle + INGEST_SECONDS


def stops(writes: set[float], *, since: float, now: float) -> list[float]:
    """The times an instance stopped, from the times of its writes read so far: a write followed by
    none for `QUIET_SECONDS`, and that is not older than `since`. A later read that shows a write
    within `QUIET_SECONDS` after it, a batch the workspace showed late, withdraws it."""
    ordered = sorted(writes)
    following = [*ordered[1:], math.inf]
    return [
        write
        for write, after in zip(ordered, following, strict=True)
        if write >= since and after - write > QUIET_SECONDS and now - write >= QUIET_SECONDS
    ]


@dataclass
class Watch:
    """What one cycle reads from the time its load stopped, `start`: the number the scaler asks
    for and the in-service count last read, the note last read, the times of each instance's writes
    read so far (by AWS identifier, never written), the decisions, and the departures read and not
    withdrawn, by instance and last write."""

    start: float
    asked: int
    in_service: int
    note: ScalerNote | None = None
    writes: dict[str, set[float]] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    departures: dict[tuple[str, float], Departure] = field(default_factory=dict)
    ready_at: float | None = None

    @property
    def since(self) -> float:
        return self.start - FRESH_SECONDS

    def span(self, instance: str) -> tuple[float, float]:
        """The first and the last write read of `instance`."""
        return min(self.writes[instance]), max(self.writes[instance])

    def attribute(self) -> list[Departure]:
        """Gives the departures, in the order their instances stopped, to the decisions, in order,
        each as many as it asked fewer: the departures left over, which no decision took."""
        for decision in self.decisions:
            decision.departures = []
        left: list[Departure] = []
        for departure in sorted(self.departures.values(), key=lambda one: (one.last_write, one.number)):
            owing = next((one for one in self.decisions if len(one.departures) < one.fewer), None)
            departure.decision = owing
            if owing is None:
                left.append(departure)
            else:
                owing.departures.append(departure)
        return left

    def waiting_for(self) -> str:
        if not self.decisions or self.asked != 0:
            return "scale_in_asked"
        if any(len(one.departures) < one.fewer for one in self.decisions):
            return "gone"
        if any(len(one.drops) < one.fewer for one in self.decisions):
            return "out_of_service"
        return "warm_readings"


# The requests.


class Ending(StrEnum):
    """How a stream ended."""

    CLOSING_EVENT = "closing_event"
    ERROR_EVENT = "error_event"
    CONNECTION_CLOSED = "connection_closed"
    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Read:
    """One streamed request, as the job read it: when it was sent and its body sent whole, its
    status, when its first and its last event arrived, keep-alives included, and its first event
    from the engine, neither a keep-alive nor an error event; the events it received and the
    keep-alives among them, how it ended, the gateway's cause when it ended with an error event that
    names one, and the kind of the error that ended it, if any."""

    sent_at: float
    body_sent_at: float | None
    status: int | None
    first_event_at: float | None
    first_engine_event_at: float | None
    last_event_at: float | None
    events: int
    pings: int
    ended: Ending
    cause: str | None = None
    error: ErrorKind | None = None

    def fields(self) -> dict[str, object]:
        return {
            "sent_at": utc(self.sent_at),
            "body_sent_at": utc(self.body_sent_at),
            "status": self.status,
            "first_event_at": utc(self.first_event_at),
            "first_engine_event_at": utc(self.first_engine_event_at),
            "last_event_at": utc(self.last_event_at),
            "events": self.events,
            "pings": self.pings,
            "ended": self.ended.value,
            "cause": self.cause,
            "error": None if self.error is None else self.error.value,
        }


def _cause(data: str) -> str | None:
    """The gateway's cause an error event carries, when it is one of the gateway's own."""
    try:
        cause = json.loads(data)["error"]["type"]
    except (ValueError, TypeError, KeyError):
        return None
    return cause if cause in {member.value for member in Cause} else None


def _request(model: str) -> loadgen.Request:
    """A short streamed Messages request with a fresh conversation."""
    session = loadgen.SyntheticSession(model, START_TOKENS, (), 64, secrets.randbits(48))
    return next(session.requests())[1]


async def send(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    *,
    clock: Clock,
    content: bytes | AsyncIterator[bytes] | None = None,
    body_sent: Callable[[], float | None] | None = None,
    bound: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Read:
    """Sends `request`, with `content` as its body or its own, as the student whose key is given,
    reads its answer as it comes and returns what it read (`Read`). `body_sent` says when the body
    was sent whole, for a body sent in parts; `bound` is the most seconds it is read for."""
    headers = {name: value for name, value in request.headers.items() if name.lower() != "content-length"}
    headers["authorization"] = f"Bearer {key}"
    reader = SseReader()
    sent_at = clock.now()
    status: int | None = None
    first: float | None = None
    engine: float | None = None
    last: float | None = None
    events = pings = 0
    ended: Ending | None = None
    cause: str | None = None
    error: ErrorKind | None = None
    try:
        async with (
            asyncio.timeout(bound) if bound is not None else contextlib.nullcontext(),
            httpx.AsyncClient(base_url=endpoint, timeout=loadgen.TIMEOUT, transport=transport, trust_env=False) as http,
            http.stream(
                request.method, request.path, headers=headers, content=request.body if content is None else content
            ) as answer,
        ):
            status = answer.status_code
            if status != HTTPStatus.OK:
                ended = Ending.HTTP_STATUS
            else:
                async for chunk in answer.aiter_bytes():
                    now = clock.now()
                    for event in reader.feed(chunk):
                        events, last = events + 1, now
                        first = now if first is None else first
                        if event.comment or event.name == PING_EVENT:
                            pings += 1
                        elif event.name == ERROR_EVENT:
                            ended, cause = Ending.ERROR_EVENT, _cause(event.data)
                        else:
                            engine = now if engine is None else engine
                            if event.name == CLOSING_EVENT and ended is None:
                                ended = Ending.CLOSING_EVENT
    except TimeoutError:
        ended = ended or Ending.TIMEOUT
    except httpx.HTTPError as failure:
        error = error_kind(failure)
        ended = ended or (Ending.TIMEOUT if error is ErrorKind.TIMEOUT else Ending.CONNECTION_CLOSED)
    if ended is None:
        # The body ended without the format's closing event or an error event.
        ended = Ending.CONNECTION_CLOSED
    whole = sent_at if body_sent is None else body_sent()
    return Read(sent_at, whole, status, first, engine, last, events, pings, ended, cause, error)


async def slow_stream(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    *,
    clock: Clock,
    hold: float,
    release: asyncio.Event | None = None,
    after: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Read:
    """Sends `request` as a held stream: its body but the last byte, then a byte of padding every
    `TRICKLE_SECONDS` for `hold` seconds or until `release` is set, then the last byte; and reads
    its answer as it comes (`send`), for at most `after` seconds once the body is whole."""
    body = request.body
    if not body.endswith(b"}"):
        raise ValueError("a held stream's body is a JSON object")
    whole: list[float] = []

    async def held() -> AsyncIterator[bytes]:
        yield body[:-1]
        until = clock.now() + hold
        while clock.now() < until and not (release is not None and release.is_set()):
            await clock.sleep(min(TRICKLE_SECONDS, until - clock.now()))
            yield PADDING
        whole.append(clock.now())
        yield body[-1:]

    return await send(
        endpoint,
        key,
        request,
        clock=clock,
        content=held(),
        body_sent=lambda: whole[0] if whole else None,
        bound=None if after is None else hold + after,
        transport=transport,
    )


@dataclass(frozen=True)
class HeldStream:
    """A session that sends one short stream after another, until `stop` is set."""

    request: loadgen.Request
    stop: asyncio.Event

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        while not self.stop.is_set():
            yield 0.0, self.request


# The run.


async def _off_loop(action: Callable[[], Any]) -> Any:
    """Runs `action`, a blocking read, in a thread."""
    return await asyncio.to_thread(action)


@dataclass
class Run:
    """One run of the spike against the pool, through the gateway at `endpoint`, as the canary
    students whose keys are given. `transport` and `offload`, which runs a blocking read of the pool
    off the event loop, are for the tests: the job gives neither."""

    pool: Pool
    endpoint: str
    keys: Sequence[str]
    settings: config.Pool
    wakes: int
    deployment_hold_seconds: float
    recorder: Recorder
    clock: Clock = field(default_factory=SystemClock)
    transport: httpx.AsyncBaseTransport | None = None
    offload: Callable[[Callable[[], Any]], Awaitable[Any]] = _off_loop
    names: Names = field(default_factory=Names)
    decisions: list[Decision] = field(default_factory=list, init=False)
    # The departures whose `scale_in` line was written, and how many departures were read.
    departures: list[Departure] = field(default_factory=list, init=False)
    departures_read: int = field(default=0, init=False)
    scale_in_streamed: bool = field(default=False, init=False)

    @property
    def student(self) -> str:
        """The canary student of the held streams and the wakes; the load runs as the others."""
        return self.keys[0]

    async def read[Value](self, source: str, action: Callable[[], Value]) -> Value | None:
        """`action`, a read of the pool, run off the loop: None, and a `read_failed` line with the
        error's kind, when it fails."""
        try:
            return await self.offload(action)
        except Exception as error:  # noqa: BLE001 - any failure of a read is recorded by its kind, never its text
            self.recorder.line("read_failed", source=source, error=error_kind(error).value)
            return None

    async def note(self) -> ScalerNote | None:
        return await self.read("pool_state", self.pool.note)

    async def gauge(self, name: str, note: ScalerNote) -> tuple[float, int] | None:
        now = self.clock.now()
        return await self.read("workspace", lambda: self.pool.gauge(name, now, note))

    async def writes(self, start: float, end: float) -> dict[str, set[float]] | None:
        return await self.read("workspace", lambda: self.pool.writes(start, end))

    def name_new(self, writes: Mapping[str, set[float]]) -> None:
        """Names each instance not named yet, in the order of its first write read: an instance is
        named as the job first reads it, and keeps its name for the run."""
        new = [(min(times), max(times), instance) for instance, times in writes.items() if times]
        for _, _, instance in sorted(new):
            self.names(instance)

    def stream_line(self, stream: str, read: Read, **fields: object) -> None:
        self.recorder.line("stream", stream=stream, **fields, **read.fields())

    async def across_deployment(self) -> None:
        """Holds one slow stream for `deployment_hold_seconds`, while the owner releases the pipeline."""
        now = self.clock.now()
        self.recorder.line(
            "hold_started",
            stream="gateway_deployment",
            hold_seconds=self.deployment_hold_seconds,
            until=utc(now + self.deployment_hold_seconds),
        )
        read = await slow_stream(
            self.endpoint,
            self.student,
            _request(self.pool.name),
            clock=self.clock,
            hold=self.deployment_hold_seconds,
            transport=self.transport,
        )
        self.stream_line("gateway_deployment", read)

    async def two_instances(self) -> bool:
        """Holds the load until the scaler asks for two instances and reads two in service, then
        stops it: whether it read them."""
        streams = 2 * self.settings.target_concurrency
        load_keys = self.keys[1:]
        if len(load_keys) < math.ceil(streams / STREAMS_PER_STUDENT):
            raise ValueError("the load needs more canary students")
        stop = asyncio.Event()
        load = loadgen.Load(
            tuple(HeldStream(_request(self.pool.name), stop) for _ in range(streams)),
            sessions_per_student=STREAMS_PER_STUDENT,
        )
        held = asyncio.create_task(
            loadgen.replay(load, load_keys, self.endpoint, at_once=streams, transport=self.transport)
        )
        self.recorder.line("load_started", streams=streams)
        deadline, reached = self.clock.now() + SCALE_OUT_WAIT_SECONDS, False
        try:
            while not reached and not held.done() and self.clock.now() < deadline:
                note = await self.note()
                if note is not None and note.capacity_asked == INSTANCES:
                    reading = await self.gauge(INSTANCES_IN_SERVICE, note)
                    reached = reading is not None and reading[1] == INSTANCES
                if not reached:
                    await self.clock.sleep(POLL_SECONDS)
        finally:
            stop.set()
            try:
                await held
            except Exception as error:  # noqa: BLE001 - recorded by its kind; the load's end is not the run's
                failure = error.exceptions[0] if isinstance(error, ExceptionGroup) else error
                self.recorder.line("load_failed", error=error_kind(failure).value)
        self.recorder.line("load_stopped", two_in_service=reached)
        if reached:
            now = self.clock.now()
            written = await self.writes(now - FRESH_SECONDS, now) or {}
            self.name_new(written)
            writing = [instance for instance, times in written.items() if max(times) >= now - 2 * SCRAPE_SECONDS]
            self.recorder.line("instances", writing=self.names.ordered(writing))
        return reached

    async def _read_note(self, watch: Watch) -> None:
        """Reads the scaler's note: a note that asks for fewer than the last is a decision, with its
        `scale_in_asked` line and the instances writing as it was read."""
        note = await self.note()
        if note is None:
            return
        now = self.clock.now()
        watch.note = note
        for decision in watch.decisions:
            decision.notes.append(note)
        if note.capacity_asked < watch.asked:
            decision = Decision(
                number=len(self.decisions) + 1,
                asked=note.capacity_asked,
                fewer=watch.asked - note.capacity_asked,
                asked_at=float(note.written_at),
                asked_read_at=now,
                in_service_before=watch.in_service,
            )
            self.decisions.append(decision)
            watch.decisions.append(decision)
            writing = [
                instance
                for instance in watch.writes
                if watch.span(instance)[1] >= note.written_at - SCRAPE_SECONDS
                and (instance, watch.span(instance)[1]) not in watch.departures
            ]
            self.recorder.line(
                "scale_in_asked",
                decision=decision.number,
                capacity_asked=note.capacity_asked,
                fewer=decision.fewer,
                written_at=utc(decision.asked_at),
                read_at=utc(now),
                writing=self.names.ordered(writing),
            )
            watch.attribute()
        watch.asked = note.capacity_asked

    async def _read_in_service(self, watch: Watch) -> None:
        """While a decision is owed an instance out of service, reads the scaler's in-service gauge,
        as fresh as the note: each instance fewer than the last reading is one decision's, in order,
        with an `out_of_service` line."""
        if watch.note is None or not any(len(one.drops) < one.fewer for one in watch.decisions):
            return
        reading = await self.gauge(INSTANCES_IN_SERVICE, watch.note)
        read_at = self.clock.now()
        if reading is None:
            return
        reading_at, in_service = reading
        if in_service < watch.in_service:
            owed = [one for one in watch.decisions if len(one.drops) < one.fewer]
            self.recorder.line(
                "out_of_service",
                decision=owed[0].number,
                in_service=in_service,
                reading_at=utc(reading_at),
                read_at=utc(read_at),
            )
            for _ in range(watch.in_service - in_service):
                owing = next((one for one in watch.decisions if len(one.drops) < one.fewer), None)
                if owing is None:
                    break
                owing.drops.append((read_at, reading_at))
        watch.in_service = in_service

    async def _read_writes(self, watch: Watch) -> None:
        """Reads every collector's writes since the watch began, kept with those read before, and
        names an instance new to the run. An instance that stopped since the watch began
        (`stops`) has left, with a `gone` line; an instance that returns to service and stops again
        leaves again. A stop that a later read contradicts, a write within `QUIET_SECONDS` after it
        that the workspace showed late, is withdrawn, with a `gone_withdrawn` line, and the
        departures are given to the decisions again (`Watch.attribute`)."""
        now = self.clock.now()
        written = await self.writes(watch.since, now)
        read_at = self.clock.now()
        if written is None:
            return
        self.name_new(written)
        for instance, times in written.items():
            watch.writes.setdefault(instance, set()).update(times)
        stopped = {
            (instance, write)
            for instance, times in watch.writes.items()
            for write in stops(times, since=watch.start - 2 * SCRAPE_SECONDS, now=now)
        }
        for key in sorted(set(watch.departures) - stopped, key=lambda key: watch.departures[key].number):
            departure = watch.departures.pop(key)
            self.recorder.line(
                "gone_withdrawn",
                departure=departure.number,
                instance=self.names(departure.instance),
                last_write_at=utc(departure.last_write),
                read_at=utc(read_at),
            )
        new = sorted(stopped - set(watch.departures), key=lambda key: (key[1], self.names(key[0])))
        for instance, last in new:
            self.departures_read += 1
            watch.departures[instance, last] = Departure(self.departures_read, instance, last, read_at)
        watch.attribute()
        for key in new:
            departure = watch.departures[key]
            self.recorder.line(
                "gone",
                departure=departure.number,
                decision=None if departure.decision is None else departure.decision.number,
                instance=self.names(departure.instance),
                last_write_at=utc(departure.last_write),
                read_at=utc(read_at),
            )

    async def _settled(self, watch: Watch) -> bool:
        """Whether the cycle is over: the scaler asks for none, every decision has its instances out
        of service and gone, and the warm readings of each are whole; then its `scale_in` lines."""
        now = self.clock.now()
        if watch.waiting_for() != "warm_readings":
            return False
        if any(now < decision.window(now)[1] + INGEST_SECONDS for decision in watch.decisions):
            return False
        watch.ready_at = now if watch.ready_at is None else watch.ready_at
        return await self._tell(watch, final=now >= watch.ready_at + FRESH_SECONDS)

    async def _tell(self, watch: Watch, *, final: bool) -> bool:
        """Tells the path of each departure of the watch from the warm readings and writes its
        `scale_in` line. Unless `final`, writes nothing and says False when a reading was missing:
        the workspace may show it a moment later."""
        now = self.clock.now()
        warm = await self.read("workspace", lambda: self.pool.readings(WARM_INSTANCES, watch.since, now)) or []
        told: dict[int, Told] = {}
        for decision in watch.decisions:
            if not decision.departures:
                continue
            end, _, settled = decision.window(now)
            newcomer = any(decision.asked_at < min(times) < end for times in watch.writes.values())
            told[decision.number] = path_of(
                asked_at=decision.asked_at,
                stops=[departure.last_write for departure in decision.departures],
                leaving=decision.fewer,
                out_of_service=len(decision.drops) >= len(decision.departures),
                warm=warm,
                end=end,
                settled=settled,
                newcomer=newcomer,
                next_at=warm_bound(decision.asked, decision.notes, decision.asked_at)[1],
            )
        if not final and any(one.unknown in MISSING for one in told.values()):
            return False
        left = watch.attribute()
        for decision in watch.decisions:
            for index, departure in enumerate(sorted(decision.departures, key=lambda one: one.last_write)):
                drop = decision.drops[index] if index < len(decision.drops) else (None, None)
                self._scale_in_line(departure, told[decision.number], decision, drop)
        for departure in left:
            self._scale_in_line(departure, Told(Path.UNKNOWN, Unknown.NOT_ASKED), None, (None, None))
        return True

    def _scale_in_line(
        self, departure: Departure, told: Told, decision: Decision | None, drop: tuple[float | None, float | None]
    ) -> None:
        departure.path = told.path
        self.departures.append(departure)
        self.recorder.line(
            "scale_in",
            departure=departure.number,
            decision=None if decision is None else decision.number,
            path=told.path.value,
            unknown=None if told.unknown is None else told.unknown.value,
            instance=self.names(departure.instance),
            asked_at=None if decision is None else utc(decision.asked_at),
            asked_read_at=None if decision is None else utc(decision.asked_read_at),
            out_of_service_read_at=utc(drop[0]),
            out_of_service_reading_at=utc(drop[1]),
            last_write_at=utc(departure.last_write),
            gone_read_at=utc(departure.gone_read_at),
            warm_before=told.warm_before,
            warm_after=list(told.warm_after),
        )

    async def cycle(self) -> None:
        """Two instances, then the departures down to none, each with its lines. In the first cycle,
        the stream across a scale-in: a slow stream sent as the load stops, before the scaler asks
        for fewer, held open while the job watches the pool and sent whole once it reads the first
        decision's instances gone (`_scaled_in`), then read to its end."""
        if not await self.two_instances():
            self.recorder.line("timeout", waiting_for="two_in_service")
            return
        watch = Watch(start=self.clock.now(), asked=INSTANCES, in_service=INSTANCES)
        deadline = watch.start + SCALE_IN_WAIT_SECONDS + self.settings.idle_minutes * 60 + LEAVE_WAIT_SECONDS
        release = asyncio.Event()
        streaming = None
        if not self.scale_in_streamed:
            streaming = asyncio.create_task(self._across_scale_in(release, deadline - watch.start))

        def stream_line(read: Read) -> None:
            first = watch.decisions[0].number if watch.decisions else None
            self.stream_line("scale_in", read, decision=first)
            self.scale_in_streamed = True

        try:
            while self.clock.now() < deadline:
                await self._read_note(watch)
                await self._read_in_service(watch)
                await self._read_writes(watch)
                if self._scaled_in(watch):
                    release.set()
                if streaming is not None and streaming.done() and not self.scale_in_streamed:
                    stream_line(streaming.result())
                if await self._settled(watch):
                    return
                await self.clock.sleep(NOTE_POLL_SECONDS)
            self.recorder.line("timeout", waiting_for=watch.waiting_for())
            await self._tell(watch, final=True)
        finally:
            release.set()
            if streaming is not None and not self.scale_in_streamed:
                stream_line(await streaming)

    @staticmethod
    def _scaled_in(watch: Watch) -> bool:
        """Whether the stream across a scale-in may end: the job has read every instance of the
        scaler's first decision gone, and that decision left an instance in service to serve it.
        A first decision that asked for none holds it to the cycle's end, when the paths are told:
        sent before, it would wake the pool and change the warm count the paths are told from."""
        if not watch.decisions:
            return False
        first = watch.decisions[0]
        return first.asked > 0 and len(first.departures) >= first.fewer

    async def _across_scale_in(self, release: asyncio.Event, hold: float) -> Read:
        """The stream across a scale-in: one slow stream (`slow_stream`), its body held open from
        now, before the scaler asks for fewer, until `release` is set, once the job has read the
        departing instance gone (`_scaled_in`), or for `hold` seconds; then read to its end. Its
        request is open, at the gateway, through the whole scale-in: the decision, the instance
        out of service and the instance gone."""
        return await slow_stream(
            self.endpoint,
            self.student,
            _request(self.pool.name),
            clock=self.clock,
            hold=hold,
            release=release,
            after=self.settings.hold_seconds + WAKE_MARGIN_SECONDS,
            transport=self.transport,
        )

    async def _awake(self, after: float | None) -> float | None:
        """The `written_at` of a note that asks for an instance, written after `after`: the pool
        awake since the last zero. When the note asks for none, one request (`rewake`), not a wake,
        is sent to wake it. None when the scaler asks for none in `SCALE_OUT_WAIT_SECONDS`."""
        deadline, sent = self.clock.now() + SCALE_OUT_WAIT_SECONDS, False
        while self.clock.now() < deadline:
            note = await self.note()
            if note is not None and note.capacity_asked > 0 and (after is None or note.written_at > after):
                return float(note.written_at)
            if note is not None and note.capacity_asked == 0 and not sent:
                sent = True
                read = await send(
                    self.endpoint,
                    self.student,
                    _request(self.pool.name),
                    clock=self.clock,
                    bound=self.settings.hold_seconds + WAKE_MARGIN_SECONDS,
                    transport=self.transport,
                )
                self.recorder.line("rewake", **read.fields())
                continue
            await self.clock.sleep(NOTE_POLL_SECONDS)
        return None

    async def _zero(self, after: float) -> tuple[float, float] | None:
        """The note's `written_at` and the time the job read the pool at zero, after `after`: a note
        asking for none written after it, and an in-service reading of zero as fresh as that note.
        None when it does not in the pool's idle period and `SCALE_IN_WAIT_SECONDS`."""
        deadline = self.clock.now() + self.settings.idle_minutes * 60 + SCALE_IN_WAIT_SECONDS
        while self.clock.now() < deadline:
            note = await self.note()
            if note is not None and note.capacity_asked == 0 and note.written_at > after:
                reading = await self.gauge(INSTANCES_IN_SERVICE, note)
                if reading is not None and reading[1] == 0:
                    return float(note.written_at), self.clock.now()
            await self.clock.sleep(NOTE_POLL_SECONDS)
        return None

    async def _next_ask(self, after: float, done: asyncio.Event, until: float) -> tuple[float, float] | None:
        """The `written_at` of the first note asking for an instance written after `after`, and when
        it was read; None when none is read once `done` is set and the time is past `until`."""
        while not (done.is_set() and self.clock.now() >= until):
            note = await self.note()
            if note is not None and note.capacity_asked > 0 and note.written_at > after:
                return float(note.written_at), self.clock.now()
            await self.clock.sleep(NOTE_POLL_SECONDS)
        return None

    async def send_wakes(self) -> None:
        """Sends `wakes` requests, each at a fresh transition of the pool to zero, each with its line."""
        hold = self.settings.hold_seconds
        last_zero: float | None = None
        # The note asking for capacity that the last wake read: the pool woke after its zero.
        woke: float | None = None
        for number in range(1, self.wakes + 1):
            awake = woke if woke is not None else await self._awake(last_zero)
            if awake is None:
                self.recorder.line("timeout", waiting_for="pool_awake", wake=number)
                return
            zero = await self._zero(awake)
            if zero is None:
                self.recorder.line("timeout", waiting_for="pool_zero", wake=number)
                return
            zero_at, zero_read_at = zero
            done = asyncio.Event()
            bound = hold + WAKE_MARGIN_SECONDS
            asking = asyncio.create_task(self._next_ask(zero_at, done, zero_read_at + bound))
            try:
                read = await send(
                    self.endpoint,
                    self.student,
                    _request(self.pool.name),
                    clock=self.clock,
                    bound=bound,
                    transport=self.transport,
                )
            finally:
                done.set()
            asked = await asking
            within = read.first_engine_event_at is not None and read.first_engine_event_at <= read.sent_at + hold
            self.recorder.line(
                "wake",
                wake=number,
                zero_written_at=utc(zero_at),
                zero_read_at=utc(zero_read_at),
                capacity_asked_at=None if asked is None else utc(asked[0]),
                capacity_asked_read_at=None if asked is None else utc(asked[1]),
                none_within_hold=not within,
                hold_seconds=hold,
                **read.fields(),
            )
            last_zero, woke = zero_at, None if asked is None else asked[0]

    async def run(self) -> None:
        """The stream across a gateway deployment, the cycles of scale-in, then the wakes."""
        self.recorder.line(
            "run_started", pool=self.pool.name, wakes=self.wakes, deployment_hold_seconds=self.deployment_hold_seconds
        )
        await self.across_deployment()
        for number in range(1, CYCLES + 1):
            self.recorder.line("cycle_started", cycle=number)
            await self.cycle()
            paths = {departure.path for departure in self.departures}
            if {Path.TERMINATED, Path.WARM_POOL} <= paths and self.scale_in_streamed:
                break
        await self.send_wakes()
        self.recorder.line("run_ended")


def main(environ: Mapping[str, str] = os.environ, clock: Clock | None = None) -> int:
    """Runs the spike on the mock pool with the job's environment and credentials, writing its
    lines on standard output: 0 once the run has ended, 1 when it could not run or failed, with a
    `failed` line that gives the step and the error's kind, and no traceback."""
    clock = SystemClock() if clock is None else clock
    recorder = Recorder(clock)
    step = "configuration"
    try:
        deployed = config.read_ssm()
        (settings,) = [pool for pool in deployed.pools if pool.name == POOL]
        if settings.engine != MOCK_ENGINE:
            recorder.line("refused", reason="not_the_mock_engine")
            return 1
        wakes = int(environ.get(WAKES_VARIABLE, DEFAULT_WAKES))
        hold = float(environ.get(DEPLOYMENT_HOLD_VARIABLE, DEFAULT_DEPLOYMENT_HOLD_SECONDS))
        if wakes < 1 or hold <= 0:
            recorder.line("refused", reason="wakes_or_hold_out_of_range")
            return 1
        # The gateway stack's output is a host name: every request goes to it over HTTPS.
        endpoint = s4_weights.gateway_address(environ[ENDPOINT_VARIABLE])
        step = "clients"
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        table = session.resource("dynamodb").Table(environ[POOL_STATE_TABLE_VARIABLE])
        step = "canary_keys"
        keys = common.canary_keys().students
        step = "run"
        run = Run(Pool(POOL, workspace, table), endpoint, keys, settings, wakes, hold, recorder, clock)
        asyncio.run(run.run())
    except Exception as error:  # noqa: BLE001 - recorded by its kind: no text of AWS's reaches the log
        recorder.line("failed", step=step, error=error_kind(error).value)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
