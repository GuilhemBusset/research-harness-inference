"""Spike S7: how an instance of the mock pool leaves when its session is lowered or ends, and what
that and a gateway deployment do to a request (system design, sections 5.1, 8.5 and 12).

A pool has what its session asks for, and an instance leaves when the session is lowered or ends
(section 5.1). AWS's documentation does not settle whether a group waits the whole deregistration
delay for a target that has no connections, nor what it does with an instance still starting when
the number is lowered (evidence E13 and E32). This job runs the scenario on the mock pool and
**records what it observed, with times, and judges nothing**: the pass condition (an idle instance
gone within a minute or two, in-flight requests finished, an instance still starting gone before one
in service) is read later, from the first real run's lines, by an issue written then (section 12).

**What it records.** One JSON object per line on standard output, which the spike jobs' log group
keeps (`Recorder`). Each has `at`, the UTC time to the millisecond, and `kind`; every other time in
it is a UTC time in the same form, or null when it was not seen. An instance is named by its order
of appearance in the run, `instance-1`, `instance-2`, ... (`Names`), never by its AWS identifier.
No key and no text that AWS or the network returned is written: an error is recorded as one of the
fixed kinds of `ErrorKind`, an answer refused as its status number, and a gateway's error event as
its cause only when that is one of the gateway's own (`rhi_common.causes.Cause`).

**The session.** No request starts a machine, so the job books the pool itself, as section 12 says
of the jobs that run on the mock pool, through the module the pipeline's verification and spike S8
book with (`scripts/pipeline/booking.py`). Before it sends anything it books a session of two
instances, from now, for the minutes of `RHI_SPIKE_RUN_MINUTES`, under the name `spike/s7_drain`
(`SET_BY`). It does not start, with a `refused` line, nothing written and nothing sent, when the
pool has a session booked or active, which is left as it is, or when the pool's `max_instances` is
below two; and without the minutes it does not start at all. It moves the pool only by changing
that session (`Booking`): each write is a `session` line, with whether it was written. When it
ends, whatever came of the run, its last line, a wait that ran out or an error, it stops the
session it holds (`Run.in_session`). It changes and stops only a session that still reads as the
one it wrote: one that someone changed, stopped or replaced meanwhile is theirs and is left as it
is. A job cancelled while one of its writes is on its way waits for that write, then stops what it
wrote (`Run._write`). A job that dies leaves a session that ends by itself after its minutes.

**What it does**, through the gateway only, as a canary student (`common.canary_keys`), and reading
the pool only from the workspace and the pool-state table (`Pool`). Of the pool it writes its
session, and nothing else: the scaler alone moves the instances. The steps, in order (`Run.run`):

1. **The stream across a gateway deployment** (`Run.across_deployment`). It writes `hold_started`,
   with how long it holds, and holds one slow stream (`slow_stream`) for that long. The job starts
   no deployment and reads none: the owner starts it against a release that is already past the
   pipeline's hold ("Across a deployment", below). The gateway reads a request's whole body before
   it forwards it, so the job sends the body but its last byte, then one byte of white space every
   `TRICKLE_SECONDS`, which JSON allows before the closing brace, and the last byte when the time
   is over: the stream's connection stays on the gateway task that took it while the deployment
   replaces that task. Then one `stream` line, `gateway_deployment`.
2. **The departures** (`Run.departures`). It waits until the scaler's note asks for two instances
   and reads two in service, as does its in-service gauge (`instances`, the two that write). It
   lowers its session to one, and watches the instance that leaves; once that departure is whole,
   a note reads the instance out of the group and the instance that note still counts was read
   writing a minute after it (`Watch.left`, `read_staying`), or its wait ran out, it stops its
   session and watches the last one leave. A note counts instances without naming them, so a
   collector read silent is the one of the instance that left only once the other is read
   writing still: until then it may be the one that stays, at a time the workspace shows no write
   for, and the session is not stopped on it. Each reading is kept apart and read again at every
   poll, so that a gap in the workspace loses nothing it shows later:
   - `scale_in_asked`: each note that asks for fewer than the last (a decision), with its
     `written_at`, when it was read and how many fewer;
   - `out_of_service`: when the job first reads the scaler's in-service gauge, as fresh as the
     note, below what it read before; each instance fewer is one owed to the decisions, in order,
     and each decision given one has its line: a reading that shows the drops of two decisions
     gives two lines, with the same times;
   - `gone`: when the job first reads an instance silent for `QUIET_SECONDS` that was writing as
     the watch began or since. Its node's collector writes the series `up` of each of its scrape
     targets every `SCRAPE_SECONDS` until the instance shuts down. Each stop is one departure; the
     departures, in the order their instances stopped, are given to the decisions in order. A
     decision takes no instance that stopped before its note was written (`Decision.may_take`):
     the line of such a stop names no decision;
   - `gone_withdrawn`: a stop that a later read contradicts, with a write after it, which the
     workspace showed late or which followed a time it shows no write for: an instance that left
     never writes again, so this one never stopped, and the departures are given to the decisions
     again;
   - `scale_in`: each departure whole, the instance with its three times, when the scaler asked
     for fewer, when it was read out of service and when it was gone. The summaries are written
     as the step ends, once for each departure read and not withdrawn, with what was read: a line
     is not taken back, and until then a later read may still withdraw a stop.
   A wait that runs out is a `timeout` line that says what was waited for, and the step goes on.
3. **The stream across a release**, in the departures. Before it lowers its session, the job opens
   one slow stream, as across the deployment, and waits until its first part is sent (`Held.open`):
   the session is lowered only once the stream is held open at the gateway. It holds the body
   open while it watches the pool. The gateway reads a body whole before it counts the request or
   forwards it, so the held stream keeps no instance busy. Its last byte is sent when the job
   reads, after its own lowering, the scaler's note that asks for fewer, for the one instance the
   job lowered to, which is the scaler acting on the lowering; at once, without waiting for the
   next byte of padding, and at no other moment: the gateway
   forwards the request to the pool while the group takes the departing instance out of service
   and before that instance stops, the window in which a request may meet a deregistering target
   (section 5.1), and the job reads the answer to its end, for at most `ANSWER_SECONDS`. Whether
   it finished, ended with an error event or was cut is what its `stream` line, `release`,
   records, with its times, among them `body_sent_at`, when it was forwarded, and the decision's
   number: the departures' lines hold theirs, so a reader places one against the other. When the
   wait for that note runs out, or the first note that asks for fewer asks for another number,
   the session having ended or been changed before the scaler acted on the lowering, the last
   byte is never sent: the line then has no `body_sent_at` and no decision, and ends
   `body_unsent`.
4. **The starting instance** (`Run.starting_instance`), in a cycle of its own. It waits until the
   note reads the pool at zero, books two instances again, for what is left of its minutes, and
   polls the note: at the first one that reads one instance in service and one starting it lowers
   its session to one, at once. Then it watches which of the two leaves, and writes one line,
   `starting_instance` (`Starting.fields`): `left`, `starting` or `in_service`, or null with the
   `reason`; and when, the note's times and the collector's. It watches until a note reads one of
   the two out of the group and the other was read writing a minute after that note
   (`Starting.settled`, `read_staying`): a collector read stopped before that may be the one of
   the instance that stays, at a time the workspace shows no write for, and names no instance.
   When the wait runs out first, the line names none and the launches tell nothing: it holds
   what the notes told. The first note written after the
   lowering that reads fewer than two starting or in service gives `left_written_at`, and the
   first such note that reads none draining either gives `gone_written_at`: the scaler counts an
   instance as starting before its collector writes, so one that leaves then has no other time.
   The instance whose collector stopped is named, with its last write. Which of the two it was,
   the one the note read starting or the one it read in service, is told only where a reading
   tells it (`which_left`):
   - by the notes (`left_by_notes`), when that first note reads none in service and one starting:
     the one in service left, the other is starting still. A note that reads one in service and
     none starting does not tell: the starting one left, or it entered service and the other left
     between two notes a minute apart, and a note counts instances without naming them;
   - by the launches (`left_by_launches`), from what each instance's lifecycle agent says of its
     own launch in the workspace (`Pool.launches`): that it started, and that its hook is
     completed, before which no instance is in service. An instance whose agent was read, at the
     lowering or after it, with its launch started and its hook not completed was the starting
     one as the job lowered (`Starting.still_starting`); the other, the note having read one of
     each, was the one in service. The instance that stopped is that one, or the other; and when
     none stopped, though a stop had the time to be read, and the starting one alone ever wrote,
     the one in service left before its collector wrote. When a collector first wrote tells
     nothing: it starts apart from the agent, and either instance's may be the first.
   Where neither tells, `left` is null, `role_not_told`, and the line still names the instance that
   left and its times: so when the starting one's collector had not written, or its hook was
   completed before the lowering, the instance not yet in service. Where both tell and differ, it
   is null, `readings_disagree`. The line holds what each told, the instances read still
   starting (`starting_when_lowered`), and when the hook of the instance that stopped was first
   read completed (`hook_completed_at`), null when it never was: an instance that was starting
   as the job lowered may have completed it before it left. The session of this cycle is stopped
   as the job ends.

**A stream's line** (`Read`): when the request was sent, its first part, and when its body was sent
whole; when its first event and its last arrived, and the events it received. The gateway may write
a keep-alive, `ping` on Messages, while it holds a stream: the keep-alives are counted among the
events and as `pings`, and `first_engine_event_at` is the first event that is neither a keep-alive
nor an error event, the engine's first, which says the request reached an instance that served it.
It ended (`Ending`) with the format's closing event, `message_stop`; with an error event; with the
connection closed, the body ending without either or the read failing; with a status other than
200; at the job's own bound once the body was whole; or with its last byte never sent.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s7_drain`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, reached over HTTPS (`common.gateway_address`) |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_SPIKE_RUN_MINUTES` | The minutes the job books the pool for, from its start: required, a whole number of one or more |
| `RHI_S7_DEPLOYMENT_SECONDS` | How long the stream across the gateway deployment is held; `DEFAULT_DEPLOYMENT_SECONDS` unless given |

The minutes are the whole run's: the first session ends by itself once they have passed, and the
second is booked for what is left of them. Give enough for the stream across the deployment and
for the waits of the other steps, each of which is bounded (`IN_SERVICE_WAIT_SECONDS`,
`DEPARTURE_WAIT_SECONDS`): about an hour and a half for a run in which no wait runs out. They are
at most the configuration's `session_max_hours`: a longer booking fails the run with nothing
written. The job stops its session when it ends, so minutes it does not use cost nothing. A
session that ends while the job runs leaves the pool with none: the scaler takes its instances
away, and the job records what it then reads, its waits running out.

The pool is the one named `mock` (`POOL`), and the job refuses it when its engine is not the mock.
Its engine and `max_instances` and the bound `session_max_hours` come from the deployed
configuration (`rhi.config.read_ssm`), and the region from CodeBuild's `AWS_REGION`. The job's role
may write the mock pool's records of the pool-state table and no other pool's
(`rhi.pipeline.spikes`). Run it while nothing else sends to the pool. The scaler releases the
instances of a lowered session from issue E6-15 on: before it is deployed, a lowered session
releases nothing, the waits of steps 2 to 4 run out, and only the stop of the session makes
instances leave.

**Across a deployment.** The pipeline holds a stage's deployment while a session is active on any
pool, this job's on the mock pool included (section 8.2, step 5): a release made once the job has
booked waits until the job has stopped its session, and deploys nothing under its first stream.
So the release comes first. While no session is active, the owner releases a change that replaces
the gateway's task, and starts the job's build once that execution's action `Hold` has succeeded:
the job then books, which holds only later releases, and opens its stream on the task that the
deployment is about to replace. The job cannot read whether it did. The owner reads it, by
comparing the `stream` line's `sent_at` and `body_sent_at` with the times of the gateway service's
deployment in the account (`docs/measurements/s7.md`, "Running it").
"""

from __future__ import annotations

import asyncio
import contextlib
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

import booking
import common
import loadgen
from rhi import config
from rhi_common.causes import Cause
from rhi_common.records import NOTE_RECORD, ScalerNote, Session
from rhi_gateway.sse import SseReader
from rhi_lifecycle.stages import METRIC as STAGE_SECONDS
from rhi_lifecycle.stages import Stage
from rhi_scaler.metrics import INSTANCES_IN_SERVICE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

JOB = "s7_drain"
# The name the job's sessions are booked under.
SET_BY = f"spike/{JOB}"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_STATE_TABLE_VARIABLE = "RHI_SPIKE_POOL_STATE_TABLE"
DEPLOYMENT_VARIABLE = "RHI_S7_DEPLOYMENT_SECONDS"
# The pool the job runs on, the mock pool, and the engine it must have (`rhi_common.pool_config`).
POOL = "mock"
MOCK_ENGINE = "mock"
# Under the gateway target group's 1800 seconds of deregistration delay (section 8.5), and long
# enough for a release that has passed the pipeline's hold to reach the gateway and replace its
# task while the stream is held.
DEFAULT_DEPLOYMENT_SECONDS = 1500

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
MINUTE_SECONDS = 60
# Seconds a reading may take to show in the workspace once it was written.
INGEST_SECONDS = 30
# The instances the job books, and the one it lowers its session to.
INSTANCES = 2
LOWERED = 1
# Seconds the job waits for the instances it booked to be in service: the mock starts in about a
# minute, once the scaler's next run has asked for it.
IN_SERVICE_WAIT_SECONDS = 20 * 60
# Seconds the job waits for an instance to leave once it lowered or stopped its session: a run of
# the scaler, the eleven minutes within which an instance is gone of its release (section 5.1),
# the quiet time that tells it gone, and a margin.
DEPARTURE_WAIT_SECONDS = 20 * 60
# Seconds the answer of the stream across a release is read once its body is whole: longer than a
# request has to finish on an instance that drains (section 5.1).
ANSWER_SECONDS = 15 * 60
# Seconds between two bytes of a slow stream's body: the edge's idle timeout is far longer.
TRICKLE_SECONDS = 5
PADDING = b" "
START_TOKENS = 200
CLOSING_EVENT = "message_stop"
ERROR_EVENT = "error"
# The gateway's keep-alive on a Messages stream it holds.
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

    def _times(self, query: str, start: float, end: float) -> dict[str, set[float]]:
        """For each instance, the times of the samples `query`, a `timestamp()` by instance, gives
        from `start` to `end`: at each step, the time of the last sample."""
        found: dict[str, set[float]] = {}
        for one in self.workspace.read_series(query, start=start, end=end, step=STEP_SECONDS):
            instance = one.labels.get("instance")
            if instance and one.samples:
                found.setdefault(instance, set()).update(one.values())
        return found

    def writes(self, start: float, end: float) -> dict[str, set[float]]:
        """Each instance of the pool that wrote from `start` to `end`, with the times of the writes
        read in that time: at each step, the time of the collector's last write."""
        return self._times(f'max by (instance) (timestamp(up{{pool="{self.name}"}}))', start, end)

    def launches(self, start: float, end: float) -> tuple[dict[str, set[float]], dict[str, set[float]]]:
        """What each instance's lifecycle agent said of its launch from `start` to `end`
        (`rhi_lifecycle.stages`): the times of the scrapes that read the launch started, its
        stage `boot` ended, and of those that read its hook completed, its stage `hook` ended."""

        def stage(name: Stage) -> dict[str, set[float]]:
            selector = f'{STAGE_SECONDS}{{pool="{self.name}",stage="{name.value}"}}'
            return self._times(f"max by (instance) (timestamp({selector}))", start, end)

        return stage(Stage.BOOT), stage(Stage.HOOK)



# The job's session.


@dataclass
class Booking:
    """The job's session on the pool `pool` of the pool-state table `table`, as the job last wrote
    it, with the two bounds of the configuration it is written under: None before the job booked
    and once it stopped. Each write is `scripts/pipeline/booking.py`'s, at the time `now`, and says
    whether it was written. A method calls the AWS SDK and may raise."""

    table: Any
    pool: str
    max_instances: int
    session_max_hours: int
    session: Session | None = None

    def book(self, minutes: int, now: int) -> bool:
        """Books the two instances for `minutes`, under the name `SET_BY`: False, with nothing
        written, when the pool has a session booked or active."""
        self.session = booking.book(
            self.table,
            self.pool,
            minutes=minutes,
            set_by=SET_BY,
            now=now,
            session_max_hours=self.session_max_hours,
            instances=INSTANCES,
            max_instances=self.max_instances,
        )
        return self.session is not None

    def lower(self, instances: int, now: int) -> bool:
        """Lowers the session to `instances`: False, with nothing written, when the job holds none,
        or when the item no longer reads as the session the job wrote or its end has come."""
        if self.session is None:
            return False
        lowered = booking.changed(
            self.table,
            self.session,
            instances=instances,
            max_instances=self.max_instances,
            now=now,
            session_max_hours=self.session_max_hours,
        )
        self.session = self.session if lowered is None else lowered
        return lowered is not None

    def stop(self, now: int) -> bool:
        """Stops the session, which the job holds no more from then on: False, with nothing written,
        when the item no longer reads as the session the job wrote or its end has come."""
        if self.session is None:
            return False
        ended = booking.stop(self.table, self.session, now=now, session_max_hours=self.session_max_hours)
        self.session = None
        return ended is not None


# The departures.


@dataclass
class Departure:
    """One instance leaving service, as the job reads it: its collector silent for `QUIET_SECONDS`.
    `instance` is its AWS identifier, never written in a line."""

    number: int
    instance: str
    last_write: float
    gone_read_at: float
    decision: Decision | None = None


@dataclass
class Decision:
    """One note of the scaler asking for `fewer` instances fewer, and what the job read of it: the
    in-service drops it read for it (when each was read, and the reading's time), and the departures
    it read for it, in order."""

    number: int
    asked: int
    fewer: int
    asked_at: float
    asked_read_at: float
    drops: list[tuple[float, float]] = field(default_factory=list)
    departures: list[Departure] = field(default_factory=list)

    @property
    def whole(self) -> bool:
        """Whether each instance it asked fewer was read out of service and gone."""
        return len(self.drops) >= self.fewer and len(self.departures) >= self.fewer

    def may_take(self, departure: Departure) -> bool:
        """Whether `departure` may be one of this decision's: it is still owed one, and the
        instance did not stop before it. The scaler stamps its note with the start of its run and
        releases an instance after that, and an instance writes until it shuts down, so the last
        write of one it released is no older than the note, less a scrape; twice that is allowed
        for two clocks. An older stop is some other event, or a batch the workspace shows late."""
        return len(self.departures) < self.fewer and departure.last_write >= self.asked_at - 2 * SCRAPE_SECONDS


def stopped_at(writes: set[float], *, since: float, now: float) -> float | None:
    """When an instance stopped, from the times of its writes read so far: its last write, when
    that is `QUIET_SECONDS` old and not older than `since`. An instance that left never writes
    again, so a write after it, whenever a later read shows one, withdraws the stop: a time with
    no write of an instance that goes on writing, however long, is no departure."""
    last = max(writes)
    return last if last >= since and now - last >= QUIET_SECONDS else None


def read_staying(writes: Mapping[str, set[float]], note: ScalerNote) -> bool:
    """Whether the instances `note` counts in the group, draining ones included, were read staying:
    at least as many instances were read writing more than a minute after the note was written. An
    instance writes until it shuts down and is in the group until then, and the scaler's run, which
    stamps its note with its start, lasts less than a minute: an instance that writes a minute
    after the note was in the group as the run read it. Once those the note counts are all read so,
    any other instance is out of the group, and a collector read stopped is an instance that left.
    Until then it may be an instance that stays, at a time the workspace shows no write for: the
    note counts instances without naming them."""
    after = note.written_at + MINUTE_SECONDS
    staying = sum(1 for times in writes.values() if times and max(times) > after)
    return staying >= note.starting + note.in_service + note.draining


@dataclass
class Watch:
    """What the departures step reads from the time the job lowered its session, `start`: the number
    the scaler asks for and the in-service count last read, the note last read, the times of each
    instance's writes read so far (by AWS identifier, never written), the decisions, and the
    departures read and not withdrawn, by instance and last write; and every note read, in order."""

    start: float
    asked: int
    in_service: int
    note: ScalerNote | None = None
    notes: list[ScalerNote] = field(default_factory=list)
    writes: dict[str, set[float]] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    departures: dict[tuple[str, float], Departure] = field(default_factory=dict)

    @property
    def since(self) -> float:
        return self.start - FRESH_SECONDS

    def span(self, instance: str) -> tuple[float, float]:
        """The first and the last write read of `instance`."""
        return min(self.writes[instance]), max(self.writes[instance])

    def attribute(self) -> list[Departure]:
        """Gives the departures, in the order their instances stopped, to the decisions, in order,
        each as many as it asked fewer: the departures left over, which no decision took. A
        decision takes no instance that stopped before it (`Decision.may_take`)."""
        for decision in self.decisions:
            decision.departures = []
        left: list[Departure] = []
        for departure in sorted(self.departures.values(), key=lambda one: (one.last_write, one.number)):
            owing = next((one for one in self.decisions if one.may_take(departure)), None)
            departure.decision = owing
            if owing is None:
                left.append(departure)
            else:
                owing.departures.append(departure)
        return left

    def left(self, decision: Decision) -> bool:
        """Whether the instances `decision` asked fewer have left for good: each was read out of
        service and gone, a note of the scaler written since the last of them stopped reads no
        more instances in the group, draining ones included, than the decision asked for, and the
        instances that note counts were read staying (`read_staying`). While the notes still count
        an instance more, or an instance they count is not read writing after them, a collector
        that seems to have stopped is not taken for the departure: it may be the one of the
        instance that stays, and a later read may withdraw it."""
        if not decision.whole:
            return False
        stopped = max(one.last_write for one in decision.departures)
        return any(
            note.written_at >= stopped
            and note.starting + note.in_service + note.draining <= decision.asked
            and read_staying(self.writes, note)
            for note in self.notes
        )

    def waiting_for(self) -> str:
        if any(len(one.departures) < one.fewer for one in self.decisions):
            return "gone"
        if any(len(one.drops) < one.fewer for one in self.decisions):
            return "out_of_service"
        if not all(self.left(one) for one in self.decisions):
            return "departure_confirmed"
        return "scale_in_asked"


# The starting instance.


class Left(StrEnum):
    """Which of the two instances left once the session was lowered: the one the note read starting,
    or the one it read in service."""

    STARTING = "starting"
    IN_SERVICE = "in_service"


class Untold(StrEnum):
    """Why the line of the starting instance does not say which of the two left."""

    POOL_NOT_AT_ZERO = "pool_not_at_zero"
    NOT_BOOKED = "not_booked"
    STATE_NOT_READ = "state_not_read"
    NOT_LOWERED = "not_lowered"
    NOTHING_LEFT = "nothing_left"
    BOTH_IN_SERVICE = "both_in_service"
    BOTH_LEFT = "both_left"
    ROLE_NOT_TOLD = "role_not_told"
    READINGS_DISAGREE = "readings_disagree"


def left_by_notes(after: Sequence[ScalerNote]) -> tuple[Left | None, Untold | None]:
    """Which of the two left, as far as the notes tell it, from those written since the one that
    read one instance in service and one starting, in order: by the first that reads fewer than two
    starting or in service. None in service and one starting: the one in service left, since the
    other is starting still. One in service and none starting does not tell (`ROLE_NOT_TOLD`): the
    starting one left, or it entered service and the other left, between two notes a minute apart,
    and the notes count instances without naming them. Not told either, with the reason, when no
    note reads fewer than two, when one read both in service before, or when the first that reads
    fewer reads neither."""
    both = False
    for note in after:
        if note.in_service + note.starting >= INSTANCES:
            both = both or note.in_service >= INSTANCES
        elif both:
            return None, Untold.BOTH_IN_SERVICE
        elif (note.in_service, note.starting) == (0, 1):
            return Left.IN_SERVICE, None
        elif (note.in_service, note.starting) == (1, 0):
            return None, Untold.ROLE_NOT_TOLD
        else:
            return None, Untold.BOTH_LEFT
    return None, Untold.NOTHING_LEFT


def left_by_launches(
    starting: Iterable[str], written: Iterable[str], stopped: Sequence[str], *, waited: bool
) -> Left | None:
    """Which of the two left, from what the instances' lifecycle agents said of their launches:
    `starting` are the instances read still starting once the job had lowered
    (`Starting.still_starting`), `written` those whose collector wrote in the cycle, `stopped`
    those whose collector stopped since the state was read. Told only when one instance alone was
    read still starting: the note read one in service and one starting, so the other is the one
    in service. When that instance alone stopped, the starting one left. When another alone
    stopped, the one in service left. So it did when none stopped, no other instance ever wrote
    and `waited`, a note read one of the two out of the group long enough ago for a collector
    that stopped to be read so: the starting one writes still, and the one in service left
    before its collector wrote. An instance's role is never told by when its collector first
    wrote: the collector and the agent start apart, and either instance's may be the first."""
    starting = set(starting)
    if len(starting) != 1 or len(stopped) > 1:
        return None
    if stopped:
        return Left.STARTING if stopped[0] in starting else Left.IN_SERVICE
    return Left.IN_SERVICE if waited and set(written) == starting else None


def which_left(by_notes: tuple[Left | None, Untold | None], by_launches: Left | None) -> tuple[Left | None, Untold | None]:
    """Which of the two left, and why it is not told when it is not: what the notes tell, or the
    launches where the notes do not, and neither when both tell and differ."""
    told, why = by_notes
    if told is not None and by_launches is not None and told is not by_launches:
        return None, Untold.READINGS_DISAGREE
    if told is None and by_launches is not None:
        return by_launches, None
    return told, why


@dataclass
class Starting:
    """What the cycle of the starting instance read: when the job booked; the note that read one
    instance in service and one starting, when the job read it and when it lowered its session;
    the notes written after that one, each with when it was read; and, for each instance (by AWS
    identifier, never written), the times read since the booking of its collector's writes, of
    the scrapes that read its launch started and of those that read its hook completed."""

    booked_at: float | None = None
    state: ScalerNote | None = None
    state_read_at: float | None = None
    lowered_at: float | None = None
    after: list[tuple[ScalerNote, float]] = field(default_factory=list)
    writes: dict[str, set[float]] = field(default_factory=dict)
    launching: dict[str, set[float]] = field(default_factory=dict)
    launched: dict[str, set[float]] = field(default_factory=dict)

    def see(self, note: ScalerNote, read_at: float) -> None:
        """Keeps a note written after the last one kept, once the state was read."""
        last = self.state if not self.after else self.after[-1][0]
        if last is not None and note.written_at > last.written_at:
            self.after.append((note, read_at))

    def _first(self, *, drained: bool) -> tuple[ScalerNote, float] | None:
        """The first note kept that reads fewer than two starting or in service, and none draining
        either when `drained`: the group holds the instance that left no more."""
        for note, read_at in self.after:
            if note.in_service + note.starting < INSTANCES and not (drained and note.draining):
                return note, read_at
        return None

    def stopped(self, now: float) -> list[tuple[float, str]]:
        """The instances whose collector stopped since the state was written, each after its last
        write, in the order they stopped."""
        if self.state is None:
            return []
        since = self.state.written_at
        last = {instance: stopped_at(times, since=since, now=now) for instance, times in self.writes.items()}
        return sorted((stop, instance) for instance, stop in last.items() if stop is not None)

    def still_starting(self) -> set[str]:
        """The instances read still starting once the job had lowered: a scrape of its lifecycle
        agent, at the time of the lowering or after it, read its launch started, and none up to
        that scrape had read its hook completed. An instance is in service only once its launch's
        hook is completed (`rhi_lifecycle.stages`), so such an instance was the starting one as
        the job lowered, whatever its collector and the other instance's wrote, and when."""
        if self.lowered_at is None:
            return set()
        return {
            instance
            for instance, scrapes in self.launching.items()
            if any(self.lowered_at <= at < min(self.launched.get(instance, (math.inf,))) for at in scrapes)
        }

    def _other_stays(self) -> bool:
        """Whether a note read one of the two out of the group and the instances that note counts
        were read staying (`read_staying`): only then is a collector read stopped the one of the
        instance that left, and not the other's at a time the workspace shows no write for."""
        gone = self._first(drained=True)
        return gone is not None and read_staying(self.writes, gone[0])

    def _waited(self, now: float) -> bool:
        """Whether a note read one of the two out of the group long enough ago for its collector,
        if it ever wrote, to be read stopped."""
        gone = self._first(drained=True)
        return gone is not None and now >= gone[1] + QUIET_SECONDS + INGEST_SECONDS

    def settled(self, now: float) -> bool:
        """Whether one of the two has left for good: a note reads it out of the group, the other
        was read staying, and its collector was read stopped, or has had the time to be if it ever
        wrote."""
        return self._other_stays() and (bool(self.stopped(now)) or self._waited(now))

    def fields(self, names: Names, why: Untold | None, now: float) -> dict[str, object]:
        """The line: which of the two left, or `why` it was not looked for, and when. The collectors
        tell which instance left, and so do the launches, only once the other was read staying
        (`_other_stays`): the line names no instance before."""
        starting = self.still_starting()
        told = self._other_stays()
        stopped = self.stopped(now) if told else []
        out, gone = self._first(drained=False), self._first(drained=True)
        by_notes = left_by_notes([note for note, _ in self.after])
        by_launches = left_by_launches(
            starting, self.writes, [instance for _, instance in stopped], waited=told and self._waited(now)
        )
        left, reason = (None, why) if why is not None else which_left(by_notes, by_launches)
        # When the hook of the instance that stopped was first read completed, if it ever was: an
        # instance that was starting as the job lowered may have completed it before it left.
        completed = self.launched.get(stopped[0][1]) if stopped else None
        return {
            "left": None if left is None else left.value,
            "reason": None if reason is None else reason.value,
            "left_by_notes": None if by_notes[0] is None else by_notes[0].value,
            "left_by_launches": None if by_launches is None else by_launches.value,
            "state_written_at": None if self.state is None else utc(self.state.written_at),
            "state_read_at": utc(self.state_read_at),
            "lowered_at": utc(self.lowered_at),
            "left_written_at": None if out is None else utc(out[0].written_at),
            "left_read_at": None if out is None else utc(out[1]),
            "in_service_after": None if out is None else out[0].in_service,
            "starting_after": None if out is None else out[0].starting,
            "gone_written_at": None if gone is None else utc(gone[0].written_at),
            "starting_when_lowered": names.ordered(starting),
            "instance": names(stopped[0][1]) if stopped else None,
            "last_write_at": utc(stopped[0][0]) if stopped else None,
            "hook_completed_at": utc(min(completed)) if completed else None,
        }


# The requests.


class Ending(StrEnum):
    """How a stream ended."""

    CLOSING_EVENT = "closing_event"
    ERROR_EVENT = "error_event"
    CONNECTION_CLOSED = "connection_closed"
    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"
    BODY_UNSENT = "body_unsent"


class Unsent(Exception):
    """A held stream is given up before its last byte: the body is never sent whole."""


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
    limit: asyncio.Timeout | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Read:
    """Sends `request`, with `content` as its body or its own, as the student whose key is given,
    reads its answer as it comes and returns what it read (`Read`). `body_sent` says when the body
    was sent whole, for a body sent in parts; `limit`, not entered yet, bounds the whole."""
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
            limit if limit is not None else contextlib.nullcontext(),
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
    except Unsent:
        ended = Ending.BODY_UNSENT
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


async def _slept(clock: Clock, seconds: float, *events: asyncio.Event | None) -> None:
    """Waits `seconds` on `clock`, or until one of `events` is set, whichever comes first."""
    waits: list[asyncio.Future[Any]] = [asyncio.ensure_future(event.wait()) for event in events if event is not None]
    if not waits:
        await clock.sleep(seconds)
        return
    waits.append(asyncio.ensure_future(clock.sleep(seconds)))
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


def _is_set(event: asyncio.Event | None) -> bool:
    return event is not None and event.is_set()


async def slow_stream(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    *,
    clock: Clock,
    seconds: float | None = None,
    release: asyncio.Event | None = None,
    abandon: asyncio.Event | None = None,
    opened: asyncio.Event | None = None,
    after: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Read:
    """Sends `request` as a held stream: its body but the last byte, then a byte of padding every
    `TRICKLE_SECONDS`, then the last byte once `seconds` have passed, when they are given, or
    `release` is set, whichever comes first; and reads its answer as it comes (`send`), for at most
    `after` seconds once the body is whole. A stream given no `seconds` sends its last byte for
    `release` alone, however long that takes, and never when `abandon` is set first: it then ends
    with its body unsent (`Ending.BODY_UNSENT`). Either event ends the wait for the next byte of
    padding: the last byte leaves as `release` is set, not at the next padding. `opened` is set
    once the body's first part is sent: the connection is made and the request is on it."""
    body = request.body
    if not body.endswith(b"}"):
        raise ValueError("a held stream's body is a JSON object")
    whole: list[float] = []
    limit = asyncio.timeout(None)

    async def held() -> AsyncIterator[bytes]:
        yield body[:-1]
        # The connection asks for the next part once it has written this one.
        if opened is not None:
            opened.set()
        until = math.inf if seconds is None else clock.now() + seconds
        while clock.now() < until and not _is_set(release):
            if _is_set(abandon):
                raise Unsent
            await _slept(clock, min(TRICKLE_SECONDS, until - clock.now()), release, abandon)
            if not (_is_set(release) or _is_set(abandon)):
                yield PADDING
        whole.append(clock.now())
        if after is not None:
            limit.reschedule(asyncio.get_running_loop().time() + after)
        yield body[-1:]

    return await send(
        endpoint,
        key,
        request,
        clock=clock,
        content=held(),
        body_sent=lambda: whole[0] if whole else None,
        limit=limit,
        transport=transport,
    )


@dataclass
class Held:
    """The stream across a release while the job watches the pool: its task, what sends its last
    byte, what gives it up and what says its first part is sent, and whether its line was
    written."""

    task: asyncio.Task[Read]
    release: asyncio.Event = field(default_factory=asyncio.Event)
    abandon: asyncio.Event = field(default_factory=asyncio.Event)
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    told: bool = False

    async def open(self) -> None:
        """Waits until the stream's first part is sent, or the stream has ended without it."""
        opening = asyncio.ensure_future(self.opened.wait())
        try:
            await asyncio.wait([self.task, opening], return_when=asyncio.FIRST_COMPLETED)
        finally:
            opening.cancel()


# The run.


async def _off_loop(action: Callable[[], Any]) -> Any:
    """Runs `action`, a blocking call, in a thread."""
    return await asyncio.to_thread(action)


@dataclass
class Run:
    """One run of the spike against the pool, through the gateway at `endpoint`, as the canary
    student whose key is given, in the session `booking` holds, booked for `run_minutes`.
    `transport` and `offload`, which runs a blocking call off the event loop, are for the tests:
    the job gives neither."""

    pool: Pool
    endpoint: str
    key: str
    booking: Booking
    run_minutes: int
    deployment_seconds: float
    recorder: Recorder
    clock: Clock = field(default_factory=SystemClock)
    transport: httpx.AsyncBaseTransport | None = None
    offload: Callable[[Callable[[], Any]], Awaitable[Any]] = _off_loop
    names: Names = field(default_factory=Names)
    decisions: list[Decision] = field(default_factory=list, init=False)
    departures_read: int = field(default=0, init=False)
    # When the minutes the job was given end, from its first booking.
    ends_at: int = field(default=0, init=False)

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

    async def launches(self, start: float, end: float) -> tuple[dict[str, set[float]], dict[str, set[float]]] | None:
        return await self.read("workspace", lambda: self.pool.launches(start, end))

    def name_new(self, writes: Mapping[str, set[float]]) -> None:
        """Names each instance not named yet, in the order of its first write read: an instance is
        named as the job first reads it, and keeps its name for the run."""
        new = [(min(times), max(times), instance) for instance, times in writes.items() if times]
        for _, _, instance in sorted(new):
            self.names(instance)

    def stream_line(self, stream: str, read: Read, **fields: object) -> None:
        self.recorder.line("stream", stream=stream, **fields, **read.fields())

    # The session. A write that fails is not a reading the run goes on without: it raises.

    async def _write[Value](self, action: Callable[[], Value]) -> Value:
        """`action`, a write of the job's session, run off the loop and waited for to its end, also
        by a job that is cancelled meanwhile: the call goes on in its thread whatever the job does,
        so the job stops its session only once it knows what that call wrote (`Booking.session`).
        The cancellation is raised again then."""
        write = asyncio.ensure_future(self.offload(action))
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError:
            while not write.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait([write])
            if not write.cancelled():
                # The cancellation is what the job ends on: the write's own error is read and dropped.
                write.exception()
            raise

    async def book(self) -> bool:
        """Books the two instances for what is left of the job's minutes, with its `session` line:
        whether it was written."""
        now = int(self.clock.now())
        minutes = (self.ends_at - now) // MINUTE_SECONDS
        written = minutes >= 1 and await self._write(lambda: self.booking.book(minutes, now))
        self.recorder.line("session", action="booked", instances=INSTANCES, minutes=minutes, written=written)
        return written

    async def lower(self) -> bool:
        """Lowers the job's session to one instance, with its `session` line: whether it was written."""
        now = int(self.clock.now())
        written = await self._write(lambda: self.booking.lower(LOWERED, now))
        self.recorder.line("session", action="lowered", instances=LOWERED, written=written)
        return written

    async def stop(self) -> None:
        """Stops the session the job holds, if it holds one, with its `session` line."""
        if self.booking.session is None:
            return
        now = int(self.clock.now())
        written = await self._write(lambda: self.booking.stop(now))
        self.recorder.line("session", action="stopped", written=written)

    async def in_session(self) -> bool:
        """The run, in a session of two instances the job books first, for its minutes, and stops
        when the run ends, whatever came of it. False, with a `refused` line and nothing written or
        sent, when the pool allows fewer than two instances or has a session booked or active."""
        if self.booking.max_instances < INSTANCES:
            self.recorder.line("refused", reason="fewer_than_two_instances_allowed")
            return False
        now = int(self.clock.now())
        self.ends_at = now + self.run_minutes * MINUTE_SECONDS
        # From the booking on, whatever fails, a line of the log included, stops the session: the
        # booking is inside, so that one written while the job is cancelled is stopped too.
        try:
            if not await self._write(lambda: self.booking.book(self.run_minutes, now)):
                self.recorder.line("refused", reason="session_booked_or_active")
                return False
            self.recorder.line("session", action="booked", instances=INSTANCES, minutes=self.run_minutes, written=True)
            await self.run()
        finally:
            await self.stop()
        return True

    # The stream across a gateway deployment.

    async def across_deployment(self) -> None:
        """Holds one slow stream for `deployment_seconds`, while the owner releases the pipeline."""
        now = self.clock.now()
        self.recorder.line(
            "hold_started",
            stream="gateway_deployment",
            seconds=self.deployment_seconds,
            until=utc(now + self.deployment_seconds),
        )
        read = await slow_stream(
            self.endpoint,
            self.key,
            _request(self.pool.name),
            clock=self.clock,
            seconds=self.deployment_seconds,
            transport=self.transport,
        )
        self.stream_line("gateway_deployment", read)

    # The departures, and the stream across a release.

    async def two_in_service(self) -> bool:
        """Waits until the scaler's note asks for two instances and reads two in service, and its
        in-service gauge reads two, then writes the instances that write: whether it read them."""
        deadline = self.clock.now() + IN_SERVICE_WAIT_SECONDS
        while self.clock.now() < deadline:
            note = await self.note()
            if note is not None and (note.capacity_asked, note.in_service) == (INSTANCES, INSTANCES):
                reading = await self.gauge(INSTANCES_IN_SERVICE, note)
                if reading is not None and reading[1] == INSTANCES:
                    now = self.clock.now()
                    written = await self.writes(now - FRESH_SECONDS, now) or {}
                    self.name_new(written)
                    writing = [one for one, times in written.items() if max(times) >= now - 2 * SCRAPE_SECONDS]
                    self.recorder.line("instances", writing=self.names.ordered(writing))
                    return True
            await self.clock.sleep(POLL_SECONDS)
        return False

    async def _read_note(self, watch: Watch) -> None:
        """Reads the scaler's note: a note that asks for fewer than the last is a decision, with its
        `scale_in_asked` line and the instances writing as it was read."""
        note = await self.note()
        if note is None:
            return
        now = self.clock.now()
        if watch.note is None or note.written_at > watch.note.written_at:
            watch.notes.append(note)
        watch.note = note
        if note.capacity_asked < watch.asked:
            decision = Decision(
                number=len(self.decisions) + 1,
                asked=note.capacity_asked,
                fewer=watch.asked - note.capacity_asked,
                asked_at=float(note.written_at),
                asked_read_at=now,
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
        and each decision given one has its `out_of_service` line, with how many. One reading that
        shows the drops of two decisions gives two lines, with the same times. An instance fewer
        that no decision read so far is owed is kept for the next one, at a later reading."""
        if watch.note is None or not any(len(one.drops) < one.fewer for one in watch.decisions):
            return
        reading = await self.gauge(INSTANCES_IN_SERVICE, watch.note)
        read_at = self.clock.now()
        if reading is None:
            return
        reading_at, in_service = reading
        given: dict[int, int] = {}
        fewer = max(0, watch.in_service - in_service)
        for _ in range(fewer):
            owing = next((one for one in watch.decisions if len(one.drops) < one.fewer), None)
            if owing is None:
                break
            owing.drops.append((read_at, reading_at))
            given[owing.number] = given.get(owing.number, 0) + 1
        for number, instances in given.items():
            self.recorder.line(
                "out_of_service",
                decision=number,
                fewer=instances,
                in_service=in_service,
                reading_at=utc(reading_at),
                read_at=utc(read_at),
            )
        watch.in_service = in_service + fewer - sum(given.values())

    async def _read_writes(self, watch: Watch) -> None:
        """Reads every collector's writes since the watch began, kept with those read before, and
        names an instance new to the run. An instance that stopped since the watch began
        (`stopped_at`) has left, with a `gone` line. A stop that a later read contradicts, a
        write after it that the workspace showed late, is withdrawn, with a `gone_withdrawn`
        line, and the departures are given to the decisions again (`Watch.attribute`)."""
        now = self.clock.now()
        written = await self.writes(watch.since, now)
        read_at = self.clock.now()
        if written is None:
            return
        self.name_new(written)
        for instance, times in written.items():
            watch.writes.setdefault(instance, set()).update(times)
        since = watch.start - 2 * SCRAPE_SECONDS
        last = {instance: stopped_at(times, since=since, now=now) for instance, times in watch.writes.items()}
        stopped = {(instance, write) for instance, write in last.items() if write is not None}
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

    def _scale_in_lines(self, watch: Watch) -> None:
        """Writes the `scale_in` line of every departure read and not withdrawn, with what was read,
        those no decision took included. Once, as the step ends: until then a later read may
        withdraw a stop, or show an earlier one that takes its decision, and a line is not taken
        back."""
        left = watch.attribute()
        for decision in watch.decisions:
            for index, departure in enumerate(decision.departures):
                drop = decision.drops[index] if index < len(decision.drops) else (None, None)
                self._scale_in_line(departure, decision, drop)
        for departure in left:
            self._scale_in_line(departure, None, (None, None))

    def _scale_in_line(
        self, departure: Departure, decision: Decision | None, drop: tuple[float | None, float | None]
    ) -> None:
        self.recorder.line(
            "scale_in",
            departure=departure.number,
            decision=None if decision is None else decision.number,
            instance=self.names(departure.instance),
            asked_at=None if decision is None else utc(decision.asked_at),
            asked_read_at=None if decision is None else utc(decision.asked_read_at),
            out_of_service_read_at=utc(drop[0]),
            out_of_service_reading_at=utc(drop[1]),
            last_write_at=utc(departure.last_write),
            gone_read_at=utc(departure.gone_read_at),
        )

    def _stream_told(self, held: Held, watch: Watch) -> None:
        """Writes the line of the stream across a release, once, with the number of the decision
        that sent its last byte: null when none did."""
        if held.told:
            return
        held.told = True
        sent = held.release.is_set() and bool(watch.decisions)
        self.stream_line("release", held.task.result(), decision=watch.decisions[0].number if sent else None)

    async def _watched(self, watch: Watch, held: Held, whole: Callable[[], bool]) -> None:
        """Reads the pool into `watch` until `whole` says the departures waited for are; a `timeout`
        line when `DEPARTURE_WAIT_SECONDS` pass first. The first decision read, when its note asks
        for the one instance the job lowered its session to, sends the last byte of the stream
        across a release, at once (`slow_stream`), unless that stream was given up. A first
        decision that asks for any other number is not the scaler acting on the lowering, the
        session ended or was changed first: the stream is given up, its last byte unsent."""
        deadline = self.clock.now() + DEPARTURE_WAIT_SECONDS
        while self.clock.now() < deadline:
            await self._read_note(watch)
            if watch.decisions and not held.abandon.is_set():
                (held.release if watch.decisions[0].asked == LOWERED else held.abandon).set()
            await self._read_in_service(watch)
            await self._read_writes(watch)
            if held.task.done():
                self._stream_told(held, watch)
            if whole():
                return
            await self.clock.sleep(NOTE_POLL_SECONDS)
        self.recorder.line("timeout", waiting_for=watch.waiting_for())

    async def departures(self) -> None:
        """Two instances in service; then the session lowered to one, and the departure of the
        instance that leaves; then the session stopped, and the departure of the last one. The
        stream across a release is opened before the lowering, its body held open, and its last
        byte is sent when the job reads, as the first decision after the lowering, the note that
        asks for one instance (`_watched`). When the wait for that decision runs out, the first
        decision asks for another number or the session was not lowered, the stream is given up
        before the session is stopped: a note that asks for fewer after the stop does not send
        it. A departure is waited for until it is whole, a note reads the instance out of the
        group and the instance that stays was read writing after that note (`Watch.left`). The
        session is stopped whatever was read."""
        if not await self.two_in_service():
            self.recorder.line("timeout", waiting_for="two_in_service")
            await self.stop()
            return
        stream = slow_stream(
            self.endpoint,
            self.key,
            _request(self.pool.name),
            clock=self.clock,
            release=(release := asyncio.Event()),
            abandon=(abandon := asyncio.Event()),
            opened=(opened := asyncio.Event()),
            after=ANSWER_SECONDS,
            transport=self.transport,
        )
        held = Held(asyncio.create_task(stream), release, abandon, opened)
        try:
            # The session is lowered once the stream is held open at the gateway, not before: its
            # connection is made and its first part sent, or it ended without, which its line says.
            await held.open()
            lowered = await self.lower()
            watch = Watch(start=self.clock.now(), asked=INSTANCES, in_service=INSTANCES)
            if lowered:
                await self._watched(watch, held, lambda: bool(watch.decisions) and watch.left(watch.decisions[0]))
            held.abandon.set()
            await self.stop()
            await self._watched(
                watch, held, lambda: watch.asked == 0 and all(watch.left(one) for one in watch.decisions)
            )
            self._scale_in_lines(watch)
        finally:
            held.abandon.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([held.task])
        self._stream_told(held, watch)

    # The starting instance.

    async def _starting_writes(self, seen: Starting) -> None:
        """Reads every collector's writes since the cycle's booking, and what the lifecycle agents
        said of their launches, kept with what was read before, and names an instance new to the
        run. The workspace repeats an instance's last sample for some minutes after it: one from
        before the booking is not one of the cycle's."""
        if seen.booked_at is None:
            return
        booked_at, now = seen.booked_at, self.clock.now()

        def keep(read: Mapping[str, set[float]], kept: dict[str, set[float]]) -> None:
            for instance, times in read.items():
                if since := {time for time in times if time >= booked_at}:
                    kept.setdefault(instance, set()).update(since)

        written = await self.writes(booked_at, now)
        if written is not None:
            keep(written, seen.writes)
            self.name_new(seen.writes)
        launches = await self.launches(booked_at, now)
        if launches is not None:
            keep(launches[0], seen.launching)
            keep(launches[1], seen.launched)

    async def _lowered_while_starting(self, seen: Starting) -> Untold | None:
        """From a pool at zero, books two instances, and lowers the session to one at the first
        note that reads one instance in service and one starting: None once it has, or why not."""
        deadline = self.clock.now() + DEPARTURE_WAIT_SECONDS
        while True:
            note, now = await self.note(), self.clock.now()
            counts = None if note is None else (note.capacity_asked, note.in_service, note.starting, note.draining)
            if note is not None and counts == (0, 0, 0, 0) and note.written_at >= now - FRESH_SECONDS:
                break
            if now >= deadline:
                self.recorder.line("timeout", waiting_for="pool_at_zero")
                return Untold.POOL_NOT_AT_ZERO
            await self.clock.sleep(NOTE_POLL_SECONDS)
        if not await self.book():
            return Untold.NOT_BOOKED
        seen.booked_at = self.clock.now()
        deadline = seen.booked_at + IN_SERVICE_WAIT_SECONDS
        while self.clock.now() < deadline:
            note = await self.note()
            if note is not None and (note.in_service, note.starting) == (1, 1):
                seen.state, seen.state_read_at = note, self.clock.now()
                if not await self.lower():
                    return Untold.NOT_LOWERED
                seen.lowered_at = self.clock.now()
                return None
            if note is not None and note.in_service >= INSTANCES:
                # Both entered service between two notes: the state will not be read.
                return Untold.STATE_NOT_READ
            await self._starting_writes(seen)
            await self.clock.sleep(NOTE_POLL_SECONDS)
        self.recorder.line("timeout", waiting_for="one_in_service_one_starting")
        return Untold.STATE_NOT_READ

    async def starting_instance(self) -> None:
        """The cycle of the starting instance: a session of two booked on a pool at zero, lowered to
        one as soon as the note reads one instance in service and one starting, then the pool
        watched until one of the two has left for good (`Starting.settled`) or
        `DEPARTURE_WAIT_SECONDS` have passed; and its one line, whatever was read."""
        seen = Starting()
        why = await self._lowered_while_starting(seen)
        if why is None:
            deadline = self.clock.now() + DEPARTURE_WAIT_SECONDS
            while not seen.settled(self.clock.now()):
                if self.clock.now() >= deadline:
                    self.recorder.line("timeout", waiting_for="starting_instance_gone")
                    break
                await self.clock.sleep(NOTE_POLL_SECONDS)
                note = await self.note()
                if note is not None:
                    seen.see(note, self.clock.now())
                await self._starting_writes(seen)
        self.recorder.line("starting_instance", **seen.fields(self.names, why, self.clock.now()))

    async def run(self) -> None:
        """The stream across a gateway deployment, the departures with the stream across a release,
        then the starting instance."""
        self.recorder.line(
            "run_started", pool=self.pool.name, run_minutes=self.run_minutes, deployment_seconds=self.deployment_seconds
        )
        await self.across_deployment()
        await self.departures()
        await self.starting_instance()
        self.recorder.line("run_ended")


def main(environ: Mapping[str, str] = os.environ, clock: Clock | None = None) -> int:
    """Runs the spike on the mock pool with the job's environment and credentials, writing its
    lines on standard output: 0 once the run has ended, 1 when it could not run or failed, with a
    `refused` line, or a `failed` line that gives the step and the error's kind, and no traceback.
    Without the minutes of `RHI_SPIKE_RUN_MINUTES` it fails before anything is read or written."""
    clock = SystemClock() if clock is None else clock
    recorder = Recorder(clock)
    step = "configuration"
    try:
        minutes = common.needs_of(environ).run_minutes
        deployed = config.read_ssm()
        (settings,) = [pool for pool in deployed.pools if pool.name == POOL]
        if settings.engine != MOCK_ENGINE:
            recorder.line("refused", reason="not_the_mock_engine")
            return 1
        seconds = float(environ.get(DEPLOYMENT_VARIABLE, DEFAULT_DEPLOYMENT_SECONDS))
        if seconds <= 0:
            recorder.line("refused", reason="deployment_seconds_out_of_range")
            return 1
        # The gateway stack's output is a host name: every request goes to it over HTTPS.
        endpoint = common.gateway_address(environ[ENDPOINT_VARIABLE])
        step = "clients"
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        table = session.resource("dynamodb").Table(environ[POOL_STATE_TABLE_VARIABLE])
        step = "canary_keys"
        key = common.canary_keys().students[0]
        step = "run"
        held = Booking(table, POOL, settings.max_instances, deployed.session_max_hours)
        run = Run(Pool(POOL, workspace, table), endpoint, key, held, minutes, seconds, recorder, clock)
        if not asyncio.run(run.in_session()):
            return 1
    except Exception as error:  # noqa: BLE001 - recorded by its kind: no text of AWS's reaches the log
        recorder.line("failed", step=step, error=error_kind(error).value)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
