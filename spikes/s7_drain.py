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
is. A job that dies leaves a session that ends by itself after its minutes.

**What it does**, through the gateway only, as a canary student (`common.canary_keys`), and reading
the pool only from the workspace and the pool-state table (`Pool`). Of the pool it writes its
session, and nothing else: the scaler alone moves the instances. The steps, in order (`Run.run`):

1. **The stream across a gateway deployment** (`Run.across_deployment`). It writes `hold_started`,
   with how long it holds, and holds one slow stream (`slow_stream`) for that long: the owner
   releases the pipeline again meanwhile, and the job starts no deployment. The gateway reads a
   request's whole body before it forwards it, so the job sends the body but its last byte, then
   one byte of white space every `TRICKLE_SECONDS`, which JSON allows before the closing brace, and
   the last byte when the time is over: the stream's connection stays on the gateway task that
   took it while the deployment replaces that task. Then one `stream` line, `gateway_deployment`.
2. **The departures** (`Run.departures`). It waits until the scaler's note asks for two instances
   and reads two in service, as does its in-service gauge (`instances`, the two that write). It
   lowers its session to one, and watches the instance that leaves; once that departure is whole,
   or its wait ran out, it stops its session and watches the last one leave. Each reading is kept
   apart and read again at every poll, so that a gap in the workspace loses nothing it shows later:
   - `scale_in_asked`: each note that asks for fewer than the last (a decision), with its
     `written_at`, when it was read and how many fewer;
   - `out_of_service`: when the job first reads the scaler's in-service gauge, as fresh as the
     note, below what it read before; each instance fewer is one owed to the decisions, in order;
   - `gone`: when the job first reads an instance silent for `QUIET_SECONDS` that was writing as
     the watch began or since. Its node's collector writes the series `up` of each of its scrape
     targets every `SCRAPE_SECONDS` until the instance shuts down. Each stop is one departure; the
     departures, in the order their instances stopped, are given to the decisions in order;
   - `gone_withdrawn`: a stop that a later read contradicts, a write within `QUIET_SECONDS` after it
     that the workspace showed late: the instance never stopped, and the departures are given to
     the decisions again;
   - `scale_in`: each departure whole, the instance with its three times, when the scaler asked
     for fewer, when it was read out of service and when it was gone, once its decision has its
     instances out of service and gone, or at the step's end with what was read.
   A wait that runs out is a `timeout` line that says what was waited for, and the step goes on.
3. **The stream across a release**, in the departures. Before it lowers its session, the job opens
   one slow stream, as across the deployment, and holds its body open at the gateway while it
   watches the pool. The gateway reads a body whole before it counts the request or forwards it,
   so the held stream keeps no instance busy. Its last byte is sent when the job reads, after its
   own lowering, the scaler's note that asks for fewer, and at no other moment: the gateway
   forwards the request to the pool while the group takes the departing instance out of service
   and before that instance stops, the window in which a request may meet a deregistering target
   (section 5.1), and the job reads the answer to its end, for at most `ANSWER_SECONDS`. Whether
   it finished, ended with an error event or was cut is what its `stream` line, `release`,
   records, with its times, among them `body_sent_at`, when it was forwarded, and the decision's
   number: the departures' lines hold theirs, so a reader places one against the other. When the
   wait for that note runs out, the last byte is never sent: the line then has no `body_sent_at`
   and ends `body_unsent`.
4. **The starting instance** (`Run.starting_instance`), in a cycle of its own. It waits until the
   note reads the pool at zero, books two instances again, for what is left of its minutes, and
   polls the note: at the first one that reads one instance in service and one starting it lowers
   its session to one, at once. Then it watches which of the two leaves, and writes one line,
   `starting_instance` (`Starting.fields`): `left`, `starting` or `in_service`, or null with the
   `reason`; and when, the note's times and the collector's. Which one left is told (`which_left`)
   by the notes written after the lowering, from the first that reads fewer than two starting or in
   service: one still in service and none starting, the starting one left; none in service and one
   starting, the one in service left. That note's `written_at` is `left_written_at`, and the first
   such note that reads none draining either is `gone_written_at`: the scaler counts an instance as
   starting before its collector writes, so one that leaves then has no other time. It is told by
   the collectors too when a single instance had written as the note read the state, the one in
   service, since an instance writes before it enters service: the instance that stopped is that
   one, or the other. Where both tell and differ, `left` is null (`readings_disagree`): two notes
   a minute apart read the same when the starting instance entered service and the other left
   between them. The line holds what each was told from. The session of this cycle is stopped as
   the job ends.

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
# enough for a release of the pipeline to reach the gateway once the owner has started it.
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
    `instance` is its AWS identifier, never written in a line. `told` once its `scale_in` line is
    written."""

    number: int
    instance: str
    last_write: float
    gone_read_at: float
    decision: Decision | None = None
    told: bool = False


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
    """What the departures step reads from the time the job lowered its session, `start`: the number
    the scaler asks for and the in-service count last read, the note last read, the times of each
    instance's writes read so far (by AWS identifier, never written), the decisions, and the
    departures read and not withdrawn, by instance and last write."""

    start: float
    asked: int
    in_service: int
    note: ScalerNote | None = None
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
        if any(len(one.departures) < one.fewer for one in self.decisions):
            return "gone"
        if any(len(one.drops) < one.fewer for one in self.decisions):
            return "out_of_service"
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
    READINGS_DISAGREE = "readings_disagree"


def left_by_notes(after: Sequence[ScalerNote]) -> tuple[Left | None, Untold | None]:
    """Which of the two left, from the notes written since the one that read one instance in service
    and one starting, in order: told by the first that reads fewer than two starting or in service.
    One still in service and none starting: the starting one left. None in service and one starting:
    the one in service left. Not told, with the reason, when no note reads fewer than two, when one
    read both in service before (the starting one entered service, and the notes do not say which
    of two instances in service left), or when the first that reads fewer reads neither."""
    both = False
    for note in after:
        if note.in_service + note.starting >= INSTANCES:
            both = both or note.in_service >= INSTANCES
        elif both:
            return None, Untold.BOTH_IN_SERVICE
        elif (note.in_service, note.starting) == (1, 0):
            return Left.STARTING, None
        elif (note.in_service, note.starting) == (0, 1):
            return Left.IN_SERVICE, None
        else:
            return None, Untold.BOTH_LEFT
    return None, Untold.NOTHING_LEFT


def left_by_collectors(written: Iterable[str], stopped: Sequence[str]) -> Left | None:
    """Which of the two left, from the collectors: `written` are the instances that had written
    when the note read one in service and one starting, `stopped` those whose collector stopped
    since. Told only when one instance alone had written, which is the one in service, since an
    instance writes before it enters service, and one alone stopped: that one, or the other."""
    written = set(written)
    if len(written) != 1 or len(stopped) != 1:
        return None
    return Left.IN_SERVICE if stopped[0] in written else Left.STARTING


def which_left(by_notes: tuple[Left | None, Untold | None], by_collectors: Left | None) -> tuple[Left | None, Untold | None]:
    """Which of the two left, and why it is not told when it is not: what the notes tell, or the
    collectors where the notes do not, and neither when both tell and differ."""
    told, why = by_notes
    if told is not None and by_collectors is not None and told is not by_collectors:
        return None, Untold.READINGS_DISAGREE
    if told is None and by_collectors is not None:
        return by_collectors, None
    return told, why


@dataclass
class Starting:
    """What the cycle of the starting instance read: when the job booked; the note that read one
    instance in service and one starting, when the job read it and when it lowered its session;
    the notes written after that one, each with when it was read; and the times of each instance's
    writes read since the booking (by AWS identifier, never written)."""

    booked_at: float | None = None
    state: ScalerNote | None = None
    state_read_at: float | None = None
    lowered_at: float | None = None
    after: list[tuple[ScalerNote, float]] = field(default_factory=list)
    writes: dict[str, set[float]] = field(default_factory=dict)

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
        return sorted(
            (stop, instance)
            for instance, times in self.writes.items()
            for stop in stops(times, since=self.state.written_at, now=now)
        )

    def settled(self, now: float) -> bool:
        """Whether one of the two has left for good: a note reads it out of the group, and its
        collector was read stopped, or has had the time to be if it ever wrote."""
        gone = self._first(drained=True)
        return gone is not None and (bool(self.stopped(now)) or now >= gone[1] + QUIET_SECONDS + INGEST_SECONDS)

    def fields(self, names: Names, why: Untold | None, now: float) -> dict[str, object]:
        """The line: which of the two left, or `why` it was not looked for, and when."""
        written = set() if self.state is None else {one for one, times in self.writes.items() if min(times) <= self.state.written_at}
        stopped = self.stopped(now)
        by_notes = left_by_notes([note for note, _ in self.after])
        by_collectors = left_by_collectors(written, [instance for _, instance in stopped])
        left, reason = (None, why) if why is not None else which_left(by_notes, by_collectors)
        out, gone = self._first(drained=False), self._first(drained=True)
        return {
            "left": None if left is None else left.value,
            "reason": None if reason is None else reason.value,
            "left_by_notes": None if by_notes[0] is None else by_notes[0].value,
            "left_by_collectors": None if by_collectors is None else by_collectors.value,
            "state_written_at": None if self.state is None else utc(self.state.written_at),
            "state_read_at": utc(self.state_read_at),
            "lowered_at": utc(self.lowered_at),
            "left_written_at": None if out is None else utc(out[0].written_at),
            "left_read_at": None if out is None else utc(out[1]),
            "in_service_after": None if out is None else out[0].in_service,
            "starting_after": None if out is None else out[0].starting,
            "gone_written_at": None if gone is None else utc(gone[0].written_at),
            "written_at_state": names.ordered(written),
            "instance": names(stopped[0][1]) if stopped else None,
            "last_write_at": utc(stopped[0][0]) if stopped else None,
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


async def slow_stream(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    *,
    clock: Clock,
    seconds: float | None = None,
    release: asyncio.Event | None = None,
    abandon: asyncio.Event | None = None,
    after: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Read:
    """Sends `request` as a held stream: its body but the last byte, then a byte of padding every
    `TRICKLE_SECONDS`, then the last byte once `seconds` have passed, when they are given, or
    `release` is set, whichever comes first; and reads its answer as it comes (`send`), for at most
    `after` seconds once the body is whole. A stream given no `seconds` sends its last byte for
    `release` alone, however long that takes, and never when `abandon` is set first: it then ends
    with its body unsent (`Ending.BODY_UNSENT`)."""
    body = request.body
    if not body.endswith(b"}"):
        raise ValueError("a held stream's body is a JSON object")
    whole: list[float] = []
    limit = asyncio.timeout(None)

    async def held() -> AsyncIterator[bytes]:
        yield body[:-1]
        until = math.inf if seconds is None else clock.now() + seconds
        while clock.now() < until and not (release is not None and release.is_set()):
            if abandon is not None and abandon.is_set():
                raise Unsent
            await clock.sleep(min(TRICKLE_SECONDS, until - clock.now()))
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
    byte and what gives it up, and whether its line was written."""

    task: asyncio.Task[Read]
    release: asyncio.Event = field(default_factory=asyncio.Event)
    abandon: asyncio.Event = field(default_factory=asyncio.Event)
    told: bool = False


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

    def name_new(self, writes: Mapping[str, set[float]]) -> None:
        """Names each instance not named yet, in the order of its first write read: an instance is
        named as the job first reads it, and keeps its name for the run."""
        new = [(min(times), max(times), instance) for instance, times in writes.items() if times]
        for _, _, instance in sorted(new):
            self.names(instance)

    def stream_line(self, stream: str, read: Read, **fields: object) -> None:
        self.recorder.line("stream", stream=stream, **fields, **read.fields())

    # The session. A write that fails is not a reading the run goes on without: it raises.

    async def book(self) -> bool:
        """Books the two instances for what is left of the job's minutes, with its `session` line:
        whether it was written."""
        now = int(self.clock.now())
        minutes = (self.ends_at - now) // MINUTE_SECONDS
        written = minutes >= 1 and await self.offload(lambda: self.booking.book(minutes, now))
        self.recorder.line("session", action="booked", instances=INSTANCES, minutes=minutes, written=written)
        return written

    async def lower(self) -> bool:
        """Lowers the job's session to one instance, with its `session` line: whether it was written."""
        now = int(self.clock.now())
        written = await self.offload(lambda: self.booking.lower(LOWERED, now))
        self.recorder.line("session", action="lowered", instances=LOWERED, written=written)
        return written

    async def stop(self) -> None:
        """Stops the session the job holds, if it holds one, with its `session` line."""
        if self.booking.session is None:
            return
        now = int(self.clock.now())
        written = await self.offload(lambda: self.booking.stop(now))
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
        if not await self.offload(lambda: self.booking.book(self.run_minutes, now)):
            self.recorder.line("refused", reason="session_booked_or_active")
            return False
        # From the booking on, whatever fails, a line of the log included, stops the session.
        try:
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
        (`stops`) has left, with a `gone` line. A stop that a later read contradicts, a write
        within `QUIET_SECONDS` after it that the workspace showed late, is withdrawn, with a
        `gone_withdrawn` line, and the departures are given to the decisions again
        (`Watch.attribute`)."""
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

    def _scale_in_lines(self, watch: Watch, *, final: bool = False) -> None:
        """Writes the `scale_in` line of each departure not told yet whose decision is whole; when
        `final`, of every departure read, with what was read, those no decision took included."""
        left = watch.attribute()
        for decision in watch.decisions:
            if not (final or decision.whole):
                continue
            for index, departure in enumerate(sorted(decision.departures, key=lambda one: one.last_write)):
                drop = decision.drops[index] if index < len(decision.drops) else (None, None)
                self._scale_in_line(departure, decision, drop)
        for departure in left if final else ():
            self._scale_in_line(departure, None, (None, None))

    def _scale_in_line(
        self, departure: Departure, decision: Decision | None, drop: tuple[float | None, float | None]
    ) -> None:
        if departure.told:
            return
        departure.told = True
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
        """Reads the pool into `watch` until `whole` says the departures waited for are, and writes
        their `scale_in` lines; a `timeout` line when `DEPARTURE_WAIT_SECONDS` pass first. The
        first decision read sends the last byte of the stream across a release, unless that stream
        was given up."""
        deadline = self.clock.now() + DEPARTURE_WAIT_SECONDS
        while self.clock.now() < deadline:
            await self._read_note(watch)
            if watch.decisions and not held.abandon.is_set():
                held.release.set()
            await self._read_in_service(watch)
            await self._read_writes(watch)
            if held.task.done():
                self._stream_told(held, watch)
            if whole():
                self._scale_in_lines(watch)
                return
            await self.clock.sleep(NOTE_POLL_SECONDS)
        self.recorder.line("timeout", waiting_for=watch.waiting_for())

    async def departures(self) -> None:
        """Two instances in service; then the session lowered to one, and the departure of the
        instance that leaves; then the session stopped, and the departure of the last one. The
        stream across a release is opened before the lowering, its body held open, and its last
        byte is sent when the job reads the first decision after the lowering (`_watched`). When
        the wait for that decision runs out, or the session was not lowered, the stream is given
        up before the session is stopped: a note that asks for fewer after the stop does not send
        it. The session is stopped whatever was read."""
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
            after=ANSWER_SECONDS,
            transport=self.transport,
        )
        held = Held(asyncio.create_task(stream), release, abandon)
        try:
            # The stream's first part leaves before the session is lowered.
            await asyncio.sleep(0)
            lowered = await self.lower()
            watch = Watch(start=self.clock.now(), asked=INSTANCES, in_service=INSTANCES)
            if lowered:
                await self._watched(watch, held, lambda: bool(watch.decisions) and watch.decisions[0].whole)
            held.abandon.set()
            await self.stop()
            await self._watched(
                watch, held, lambda: watch.asked == 0 and all(one.whole for one in watch.decisions)
            )
            self._scale_in_lines(watch, final=True)
        finally:
            held.abandon.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([held.task])
        self._stream_told(held, watch)

    # The starting instance.

    async def _starting_writes(self, seen: Starting) -> None:
        """Reads every collector's writes since the cycle's booking, kept with those read before,
        and names an instance new to the run. The workspace repeats an instance's last write for
        some minutes after it: a write from before the booking is not one of the cycle's."""
        booked_at = seen.booked_at
        if booked_at is None:
            return
        written = await self.writes(booked_at, self.clock.now())
        if written is None:
            return
        since = {instance: {time for time in times if time >= booked_at} for instance, times in written.items()}
        self.name_new({instance: times for instance, times in since.items() if times})
        for instance, times in since.items():
            if times:
                seen.writes.setdefault(instance, set()).update(times)

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
