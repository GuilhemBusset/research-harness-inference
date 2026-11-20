"""Spike S8: does a replayed stickiness cookie keep a student on one instance, with two instances in
two zones (system design, sections 5.1, 5.5, 7.2 and 12)?

A student's turns stay on one instance because the gateway replays the internal load balancer's
stickiness cookie for each student (section 5.5). AWS lists a healthy target in every zone as a
requirement for stickiness, which a pool of two instances over three zones does not have, and the
replay itself has not been tried (evidence E13, known gap 10). This job asks it of the mock pool.
**Pass:** every request of a student reaches one instance until that instance leaves.

**What it does**, through the gateway only, as canary students (`common.canary_keys`), and reading
the pool only from the workspace and the pool-state table. It never changes the pool: the scaler
alone moves it (section 5.1).

1. **Two instances.** It holds twice the pool's `target_concurrency` of streams open, each sending
   one short stream after another, as the canary students after the first two, until the workspace
   shows two instances in service in one observation (`Pool.present`): both writing, both streaming
   answers, and both asked for and read in service by the scaler, whose gauge of the instances in
   service is in the workspace too. The scaler adds the second once the load is above target for 60
   seconds, and an instance writes, and answers its launch's warm-up, before its launch completes and
   it enters service. It records how many zones the two are in, by the label `zone` the node's
   collector adds to every series; an instance whose series has no zone label is not a zone, and
   the load is held until both have one. Then it stops the load and waits until the workspace shows
   nothing running or waiting on the pool.
2. **Before the departure.** The first two canary students send their first requests at once, so
   that least-outstanding routing places them on different instances: whichever instance leaves,
   one of them has to move. Then each student in turn sends the number of requests it is given, one
   after another, continuing the conversation its first request began, and the job reads how many
   of them each instance served. A student's first request counts among its requests of the phase
   (`with_openings`): its sent number is one more than it was given. Both instances must still be
   in service when the phase ends, and the scaler's note must still ask for both; if one has left,
   or is leaving, the run is not measured (`Departed`), since a student moved by a departure did
   not leave an instance that was there. The phase ends `SETTLE_SECONDS` after the last request,
   and the scaler asks Auto Scaling for fewer instances before it writes its note, so a scale-in
   the scaler began while a student's requests were sent shows at the phase's end.
   When a student's requests were split, the job looks again every `POLL_SECONDS` for
   `SPLIT_RECHECK_SECONDS`, for a departure the scaler did not ask for (an instance that stopped
   writing, or one the group replaced) and that had not yet shown in the workspace: that makes the
   run not measured rather than failed. A scale-in the scaler asks for while it looks began after
   the phase, when both instances were in service: the split stands, and the run goes on.
3. **After the departure.** It sends nothing until the scaler has taken the pool to one instance:
   its note asks for one, its gauge reads one in service, and the workspace shows exactly one
   writing (load that fits in one instance for 10 minutes, and 15 since the scale-out, section 5.1).
   A sample that has gone stale is not a departure while the scaler still asks for two, nor while it
   still reads two in service: an instance being drained stays in service until it is gone. Then
   each student sends its requests again, and every one of them must have reached the instance
   that stayed. The job prints, for each student, the instance it was on before the departure,
   whether that one stayed or left, and whether its requests after reached the remaining one.

The question is asked only when, before the departure, the two students were on different
instances: one of them then has to move, to the remaining instance, and the other has to stay. A
run whose students were both on one instance, the one that stayed (neither moved) or the one that
left (both moved), would pass without asking it, so it is not measured (`NotExercised`).

**Which instance served** is read from the workspace: the count of the mock's histogram of the gaps
between two output tokens of a stream, on each `instance` series (section 7.2). A student's request
is a stream, and the mock streams its canned turn, so each request adds `GAPS_PER_REQUEST`. The
lifecycle agent's completion of one token, which it sends every instance at launch and every minute
after (section 4.3), is a whole answer: it has a first token, and so is counted by the
time-to-first-token histogram, but no gap. A student's window opens before its first request and
closes `SETTLE_SECONDS` after its last, so the collector's scrape has carried the last count to the
workspace.

**Whose first request an instance served.** The two first requests are sent at once, so their window
cannot say which student each instance served. The mock's cache imitation can (section 5.5, issue
E2-75): an instance keeps, for each student, the input of the last request of that student it
served, and a request it serves has as cached the input it shares with that one from its start. A
student's second request repeats its first whole, so it has all of the first's input cached exactly
when it reached the instance that served the first, as each answer's usage reports. The two first
requests are then given to the two students in the ways that agree with this: a student that
kept its first's instance has its first on an instance its window reached; one that did not, when
its window reached a single instance, has it on another. A student whose window reached two
instances is split wherever its first was. The firsts are given only when their window counts one
request per student and each student's window counts exactly the requests it sent: a miscount in
one window is never made up by another. Counts with a miscount, that no way agrees with, or that two
ways agree with and give the students different counts, are measured again.

**The rebalance number.** For each new rebalance number the gateway drops about one student in ten
of its cookies (section 5.5), which would move a student for a reason that is not the one measured.
One student sending one request after another never leaves an instance with waiting requests, so
the scaler has no cause to raise it, but the job checks: it reads the pool's number from the
scaler's note at the start and the end of each measured phase, and measures a phase again when it
changed. A gateway task judges a request on the number it last read, every readiness cycle, so a
phase sends nothing until `GATEWAY_SECONDS` after the job first read the number it started with:
a number the scaler wrote just before the phase then acts before it, not during it. It does the same when a student's counts do not add up to the requests it sent, which is
another request on the pool during the window. After `ATTEMPTS` disturbed attempts it gives up, and
the run is not measured. The note is read with a strongly consistent read, so the reading at a
phase's end sees a change the scaler wrote during it. Each attempt is a new conversation, and each
run draws its first conversation at random, so that neither an earlier attempt's cache nor that
of an earlier run of the job, with the same canary students, answers for this one's.

**The numbers** (`Number`): the zones of the two instances, and for each student and phase the
requests it sent and the most any one instance served. A student stayed on one instance in a phase
when the two are equal. Two instances in one zone are not the question S8 asks: such a run posts
its numbers with no verdict (`Verdict.NOT_MEASURED`, the state error).

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s8_stickiness`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_S8_REQUESTS` | The requests each student sends in each phase, one after another |
| `RHI_SPIKE_POOL` | The pool, `mock` unless given |

The pool's `target_concurrency` comes from the deployed configuration (`rhi.config.read_ssm`), and
the region from CodeBuild's `AWS_REGION`. The canary students need keys for the two measured
students and enough others to hold the load, three streams each (`STREAMS_PER_STUDENT`). Run it
while nothing else sends to the pool: another request in a student's window is measured again, and
one that lands after the departure on an instance other than the remaining one stops the run.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import boto3

import common
import loadgen
from rhi import config
from rhi_admin.roster import DEFAULT_CONCURRENCY_LIMIT
from rhi_common.engine_metrics import MOCK, Quantity, metric_names
from rhi_common.records import NOTE_RECORD, ScalerNote
from rhi_gateway.readiness import INTERVAL_SECONDS as GATEWAY_CYCLE_SECONDS
from rhi_mock_engine.app import ANSWER, REASONING
from rhi_reporter.measure import Verdict
from rhi_scaler.metrics import INSTANCES_IN_SERVICE

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Mapping, Sequence

JOB = "s8_stickiness"
MEASUREMENT = "S8"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_STATE_TABLE_VARIABLE = "RHI_SPIKE_POOL_STATE_TABLE"
REQUESTS_VARIABLE = "RHI_S8_REQUESTS"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "mock"

# The streams one student of the load holds: one under a student's default limit, since a refused
# request ends its stream's session (`loadgen.replay`) and a lease may outlive its stream a moment.
STREAMS_PER_STUDENT = DEFAULT_CONCURRENCY_LIMIT - 1
# The instances of the question.
INSTANCES = 2
# The two measured students: the first two canary keys. The load runs as the others.
STUDENTS = (1, 2)
# Seconds. The node's collector scrapes every 15 seconds and writes to the workspace after; a window
# is read this long after its last request, and a series is fresh when its last sample is this recent.
SETTLE_SECONDS = 90
FRESH_SECONDS = 60
POLL_SECONDS = 30
# Seconds. The scaler adds an instance after 60 seconds above target, and the mock starts in about
# a minute; it removes one after 10 idle minutes and 15 since the scale-out (section 5.1).
SCALE_OUT_WAIT_SECONDS = 20 * 60
LOAD_END_WAIT_SECONDS = 5 * 60
DEPARTURE_WAIT_SECONDS = 40 * 60
# Seconds. After a phase in which a student's requests were split, the job looks this long, every
# `POLL_SECONDS`, whether an instance left: one that stopped writes no sample after `FRESH_SECONDS`,
# and one the group replaces shows a third instance once its replacement serves.
SPLIT_RECHECK_SECONDS = 5 * 60
# How many times a phase is measured before a disturbed one stops the run.
ATTEMPTS = 3
# The sizes of the requests: small, since only where each lands matters.
START_TOKENS = 200
TURN_INPUT_TOKENS = 50
OUTPUT_TOKENS = 64

# Seconds. Every gateway task reads the scaler's note once a readiness cycle, and judges a student's
# request on the rebalance number it last read: a number has reached them all two cycles after the
# job first read it, the second for the time a cycle's reads take.
GATEWAY_SECONDS = 2 * GATEWAY_CYCLE_SECONDS
# The gaps between the output tokens of one streamed answer of the mock: its canned turn has one
# token per piece. A whole answer, as the lifecycle agent's completion of one token, has none.
GAPS_PER_REQUEST = len(REASONING) + len(ANSWER) - 1

# Seconds: a sample this close to an instant is the query's sample at that instant.
_SAME_INSTANT = 1.0
_GAPS = f"{metric_names(MOCK)[Quantity.TIME_PER_OUTPUT_TOKEN]}_count"
_RUNNING = metric_names(MOCK)[Quantity.RUNNING_REQUESTS]
_WAITING = metric_names(MOCK)[Quantity.WAITING_REQUESTS]


class Phase(StrEnum):
    """The two measured phases: before the departure of an instance, and after it."""

    BEFORE = "before"
    AFTER = "after"


class Number(StrEnum):
    """The names of the numbers the job posts."""

    ZONES = "zones"
    STUDENT_1_BEFORE_SENT = "student_1_before_sent"
    STUDENT_1_BEFORE_MOST_SERVED = "student_1_before_most_served"
    STUDENT_1_AFTER_SENT = "student_1_after_sent"
    STUDENT_1_AFTER_MOST_SERVED = "student_1_after_most_served"
    STUDENT_2_BEFORE_SENT = "student_2_before_sent"
    STUDENT_2_BEFORE_MOST_SERVED = "student_2_before_most_served"
    STUDENT_2_AFTER_SENT = "student_2_after_sent"
    STUDENT_2_AFTER_MOST_SERVED = "student_2_after_most_served"


def sent(student: int, phase: Phase) -> Number:
    """The name of the requests `student` sent in `phase`."""
    return Number(f"student_{student}_{phase}_sent")


def most_served(student: int, phase: Phase) -> Number:
    """The name of the most requests of `student` in `phase` that any one instance served."""
    return Number(f"student_{student}_{phase}_most_served")


# The verdict.


def passes(numbers: common.Numbers) -> bool:
    """Whether every student's requests, in each phase, all reached one instance."""
    return all(
        numbers[sent(student, phase)] > 0 and numbers[most_served(student, phase)] == numbers[sent(student, phase)]
        for student in STUDENTS
        for phase in Phase
    )


@dataclass(frozen=True)
class ZonedSpike(common.Spike):
    """A spike whose question needs its two instances in two zones: with one zone, no verdict."""

    def verdict(self, numbers: common.Numbers) -> Verdict:
        if numbers[Number.ZONES] < 2:
            return Verdict.NOT_MEASURED
        return super().verdict(numbers)


SPIKE = ZonedSpike(MEASUREMENT, Number, passes)


# The counts.


@dataclass(frozen=True)
class Counts:
    """One student's window: the requests it sent, and how many of them each instance served."""

    sent: int
    served: Mapping[str, int] = field(default_factory=dict)
    # False when some of the student's requests could not be given an instance (`with_openings`).
    accounted: bool = True

    @property
    def most_served(self) -> int:
        return max(self.served.values(), default=0)

    def adds_up(self) -> bool:
        """Whether the instances served exactly the student's requests: each of them was given an
        instance, and no other request came in its window."""
        return self.accounted and sum(self.served.values()) == self.sent


type PhaseCounts = Mapping[int, Counts]


def numbers_of(zones: int, phases: Mapping[Phase, PhaseCounts]) -> dict[str, int | float]:
    """The numbers the job posts, from the zones and each phase's counts by student."""
    numbers: dict[str, int | float] = {Number.ZONES: zones}
    for phase, counts in phases.items():
        for student, held in counts.items():
            numbers[sent(student, phase)] = held.sent
            numbers[most_served(student, phase)] = held.most_served
    return numbers


def served_between(series: Sequence[common.Series], start: float, end: float) -> dict[str, int]:
    """Requests each instance served from `start` to `end`: the rise of its count between the two,
    from a range query's answer whose samples are at `start` and `end`. A series with no sample at
    `start` started inside the window, from 0; one with no sample at `end` keeps its last."""
    served: dict[str, int] = {}
    for one in series:
        if not one.samples:
            continue
        (first_at, first), (_, last) = one.samples[0], one.samples[-1]
        rise = last - (first if first_at <= start + _SAME_INSTANT else 0.0)
        instance = one.labels.get("instance", "")
        served[instance] = served.get(instance, 0) + round(rise)
    return {instance: count for instance, count in served.items() if count > 0}


def with_openings(openings: Mapping[str, int], counts: PhaseCounts, kept: Mapping[int, bool]) -> PhaseCounts:
    """Each student's counts with its first request added to them: `openings` counts the two first
    requests, sent at once, by instance, and `kept` says for each student whether its next request
    reached the instance that served its first (`kept_opening`).

    The firsts are placed only when the window of the firsts counts exactly one request per student
    and each student's own window counts exactly the requests it sent. They are then given to the
    students in the ways that agree with what each student's window shows (`_agrees`), and placed
    only when every such way gives each student the same counts. Otherwise (a window with a request
    too many or too few, no way that agrees, or two that give different counts, so the evidence does
    not say which student's first an instance served) none is placed: every student's counts are
    marked as not accounted for (`Counts.accounted`), they do not add up, and the phase is measured
    again. A miscount in one window is never made up by one in another."""
    left = Counter({instance: count for instance, count in openings.items() if count > 0})
    ways: list[PhaseCounts] = []
    if left.total() == len(counts) and all(held.adds_up() for held in counts.values()):
        for order in dict.fromkeys(itertools.permutations(left.elements())):
            if all(
                _agrees(place, counts[student], kept[student]) for student, place in zip(counts, order, strict=True)
            ):
                ways.append(_placed(counts, dict(zip(counts, order, strict=True))))
    if ways and all(way == ways[0] for way in ways):
        return ways[0]
    return {student: Counts(held.sent + 1, dict(held.served), accounted=False) for student, held in counts.items()}


def _placed(counts: PhaseCounts, placed: Mapping[int, str]) -> PhaseCounts:
    """Each student's counts with its first request on the instance `placed` gives it."""
    result: dict[int, Counts] = {}
    for student, held in counts.items():
        served = Counter(held.served)
        served[placed[student]] += 1
        result[student] = Counts(held.sent + 1, dict(served))
    return result


def _agrees(place: str, held: Counts, kept: bool) -> bool:
    """Whether a student's first request may have reached `place`: its next request, which is in its
    window `held`, reached that instance when it `kept` it, and another one when it did not."""
    if kept:
        return place in held.served
    return set(held.served) != {place}


def kept_opening(opening: loadgen.Record, following: loadgen.Record) -> bool:
    """Whether a student's request `following`, the next after its first `opening`, reached the
    instance that served `opening`: it had the whole input of `opening` cached."""
    first, cached = opening.input_tokens, following.cached_input_tokens
    return first is not None and cached is not None and 0 < first <= cached


def instance_of(held: Counts) -> str | None:
    """The one instance that served every request of the window, `None` when none did."""
    if len(held.served) != 1 or not held.adds_up():
        return None
    return next(iter(held.served))


def exercised(before: PhaseCounts, remaining: set[str]) -> bool:
    """Whether the run asked the question: before the departure, each student was on one instance,
    the two students on different instances, so that exactly one of them, the one on the instance
    that left, had to move, and the other had to stay."""
    on = [instance_of(held) for held in before.values()]
    return None not in on and len(set(on)) == len(on) and sum(instance in remaining for instance in on) == 1


class Disturbed(RuntimeError):
    """A phase was disturbed on every attempt: the run is not a measurement."""


class Departed(RuntimeError):
    """An instance left, or began to leave, while the phase before the departure was measured."""


class NotExercised(RuntimeError):
    """The two students were on one instance before the departure: either neither had to move, or
    both did, and the run does not show one student moving while the other stays."""


class Leaving(RuntimeError):
    """The scaler asks for fewer instances than the workspace shows, or than it reads in service:
    one is leaving, and the workspace cannot say which. `present` are the instances the workspace
    shows."""

    def __init__(self, message: str, present: set[str]) -> None:
        super().__init__(message)
        self.present = present


class Unseen(RuntimeError):
    """The instances writing to the workspace are not those the scaler asks for and reads in
    service, and not because it asks for fewer: one is starting (it writes, or is asked for, before
    it is in service), or one stopped writing without the scaler asking for fewer."""


def measure_undisturbed(
    phase: Phase,
    measure: Callable[[], PhaseCounts],
    rebalance_number: Callable[[], int],
    *,
    attempts: int = ATTEMPTS,
    log: Callable[[str], None] = print,
) -> PhaseCounts:
    """Measures `phase` with `measure`, reading the pool's rebalance number at its start and its
    end, and measures it again when the number changed or a student's counts do not add up.
    `Disturbed` after `attempts` disturbed attempts."""
    for attempt in range(1, attempts + 1):
        first = rebalance_number()
        counts = measure()
        last = rebalance_number()
        log(f"{phase} attempt {attempt}: rebalance number {first} at the start, {last} at the end")
        if first == last and all(held.adds_up() for held in counts.values()):
            return counts
        log(f"{phase} attempt {attempt} was disturbed: measured again")
    raise Disturbed(f"the phase {phase} was disturbed on each of {attempts} attempts")


# The pool, as the workspace and the pool-state table show it.


@dataclass(frozen=True)
class Pool:
    """The pool under test, read from the workspace and the pool-state table."""

    name: str
    workspace: common.Workspace
    table: Any

    def _read(self, query: str, start: float, end: float, step: float) -> list[common.Series]:
        return self.workspace.read_series(query, start=start, end=end, step=step)

    def serving(self) -> dict[str, str | None]:
        """The instances that streamed an answer in the last minutes, each with its zone label, `None`
        when its series has none: those whose count of token gaps rose. The lifecycle agent's
        completions, at launch and every minute after, are whole answers, with no gap."""
        end = time.time()
        series = self._read(f'{_GAPS}{{pool="{self.name}"}}', end - 2 * FRESH_SECONDS, end, FRESH_SECONDS)
        rising = served_between(series, end - 2 * FRESH_SECONDS, end)
        return {
            one.labels["instance"]: one.labels.get("zone") or None
            for one in series
            if rising.get(one.labels["instance"])
        }

    def in_service(self) -> int | None:
        """The instances in service as the scaler last read them from the group, from its gauge in
        the workspace (`rhi_scaler.metrics`): `None` while the workspace has no recent value."""
        end = time.time()
        series = self._read(f'{INSTANCES_IN_SERVICE}{{pool="{self.name}"}}', end - FRESH_SECONDS, end, 15)
        recent = [
            sample
            for one in series
            if one.labels.get("pool") == self.name
            for sample in one.samples
            if sample[0] >= end - FRESH_SECONDS
        ]
        return None if not recent else round(max(recent)[1])

    def present(self) -> set[str]:
        """The instances in service: those whose collector wrote a sample in the last `FRESH_SECONDS`,
        as many as the scaler asks for and as many as it reads in service. `Leaving` while it asks
        for fewer: an instance being drained still writes, and stays in service until it is gone.
        `Unseen` otherwise when the numbers differ, or while the workspace has no recent reading of
        the scaler's gauge: an instance that writes is not in service until its launch is complete,
        a stale sample is not an instance the scaler took away, and neither a lower capacity asked
        for nor a series gone quiet shows that a scale-in has completed."""
        end = time.time()
        series = self._read(f'timestamp({_RUNNING}{{pool="{self.name}"}})', end - FRESH_SECONDS, end, 15)
        fresh = {one.labels["instance"] for one in series if one.samples and one.samples[-1][1] >= end - FRESH_SECONDS}
        note = self._note()
        asked = None if note is None else note.capacity_asked
        in_service = self.in_service()
        if asked is not None and (asked < len(fresh) or (in_service is not None and asked < in_service)):
            raise Leaving(f"the scaler asks for {asked} instances, {len(fresh)} are present", fresh)
        if in_service is None:
            raise Unseen(f"the workspace has no recent reading of the instances in service, {len(fresh)} are present")
        if any(count != len(fresh) for count in (asked, in_service) if count is not None):
            raise Unseen(f"the scaler asks for {asked} and reads {in_service} in service, {len(fresh)} are present")
        return fresh

    def busy(self) -> float:
        """The requests running or waiting on the pool's instances, as the workspace last saw them."""
        end = time.time()
        query = f'sum({_RUNNING}{{pool="{self.name}"}}) + sum({_WAITING}{{pool="{self.name}"}})'
        series = self._read(query, end - FRESH_SECONDS, end, 15)
        return sum(one.samples[-1][1] for one in series if one.samples)

    def served(self, start: float, end: float) -> dict[str, int]:
        """Streamed requests each instance served from `start` to `end`: its token gaps, over those of
        one answer. A stream with fewer, cut short, still counts as one request."""
        gaps = served_between(self._read(f'{_GAPS}{{pool="{self.name}"}}', start, end, end - start), start, end)
        return {instance: math.ceil(count / GAPS_PER_REQUEST) for instance, count in gaps.items()}

    def _note(self) -> ScalerNote | None:
        """The scaler's note, read strongly consistent: a write the scaler completed is seen."""
        item = self.table.get_item(Key={"pool": self.name, "record": NOTE_RECORD}, ConsistentRead=True).get("Item")
        return None if item is None else ScalerNote.from_item(item)

    def rebalance_number(self) -> int:
        """The pool's rebalance number, from the scaler's note: 0 while it has none."""
        note = self._note()
        return 0 if note is None else note.rebalance_number


# The load and the students' requests.


@dataclass(frozen=True)
class HeldStream:
    """A session that sends one short stream after another, until `stop` is set."""

    request: loadgen.Request
    stop: asyncio.Event

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        while not self.stop.is_set():
            yield 0.0, self.request


def _session(model: str, requests: int, seed: int) -> loadgen.SyntheticSession:
    turn = loadgen.Turn(pause_seconds=0.0, input_tokens=TURN_INPUT_TOKENS)
    return loadgen.SyntheticSession(model, START_TOKENS, (turn,) * (requests - 1), OUTPUT_TOKENS, seed)


@dataclass
class Opened:
    """A session sent in two replays: its first request (`Opening`), then the others (`Rest`),
    which continue the conversation with the first's answer."""

    session: loadgen.Session
    _steps: Generator[tuple[float, loadgen.Request], str] | None = field(default=None, init=False)
    _answer: str = field(default="", init=False)

    def opening(self) -> Generator[tuple[float, loadgen.Request], str]:
        self._steps = self.session.requests()
        self._answer = yield next(self._steps)

    def rest(self) -> Generator[tuple[float, loadgen.Request], str]:
        if self._steps is None:
            raise RuntimeError("the rest of a session is sent after its first request")
        try:
            step = self._steps.send(self._answer)
            while True:
                step = self._steps.send((yield step))
        except StopIteration:
            return


@dataclass(frozen=True)
class Opening:
    """The first request of an `Opened` session."""

    opened: Opened

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        return self.opened.opening()


@dataclass(frozen=True)
class Rest:
    """The requests of an `Opened` session after its first."""

    opened: Opened

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        return self.opened.rest()


def _first_request(model: str) -> loadgen.Request:
    return next(_session(model, 1, 0).requests())[1]


def _all_answered(records: Sequence[loadgen.Record], expected: int) -> None:
    if len(records) != expected or any(record.status != HTTPStatus.OK for record in records):
        raise RuntimeError("a request of a measured student was not answered 200")


async def _wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not await asyncio.to_thread(condition):
        if time.monotonic() > deadline:
            raise TimeoutError(f"waited {seconds:.0f} seconds for {what}")
        await asyncio.sleep(POLL_SECONDS)


@dataclass
class Run:
    """One run of the spike against the pool, through the gateway at `endpoint`."""

    pool: Pool
    endpoint: str
    keys: Sequence[str]
    requests: int
    target_concurrency: int
    # The seed of each new conversation: no two attempts send the same text, and no two runs.
    _conversations: Iterator[int] = field(
        default_factory=lambda: itertools.count(secrets.randbits(48)), init=False, repr=False
    )
    # When the job first read each rebalance number, and the number it read last.
    _first_read: dict[int, float] = field(default_factory=dict, init=False, repr=False)
    _number: int = field(default=0, init=False, repr=False)

    def rebalance_number(self) -> int:
        """The pool's rebalance number, noting when the job first read it."""
        self._number = self.pool.rebalance_number()
        self._first_read.setdefault(self._number, time.time())
        return self._number

    def _gateway_holds(self) -> None:
        """Waits until every gateway task holds the number last read: `GATEWAY_SECONDS` after the job
        first read it, which is after the scaler wrote it. A request is judged on the number its
        gateway task holds (`rhi_gateway.affinity`), so a phase that starts sooner could see a
        number the job read before it act during it, and its start and end readings would not show it."""
        remaining = self._first_read[self._number] + GATEWAY_SECONDS - time.time()
        if remaining > 0:
            time.sleep(remaining)

    async def two_instances(self) -> dict[str, str]:
        """Holds the load until two instances serve, and returns them with their zones once it has ended."""
        streams = 2 * self.target_concurrency
        holders = math.ceil(streams / STREAMS_PER_STUDENT)
        load_keys = self.keys[len(STUDENTS) :]
        if len(load_keys) < holders:
            raise ValueError(f"the load needs {holders} canary students besides the {len(STUDENTS)} measured")
        stop = asyncio.Event()
        request = _first_request(self.pool.name)
        load = loadgen.Load(
            tuple(HeldStream(request, stop) for _ in range(streams)), sessions_per_student=STREAMS_PER_STUDENT
        )
        print(f"holding {streams} streams as {holders} students")
        held = asyncio.create_task(loadgen.replay(load, load_keys, self.endpoint, at_once=streams))
        found: dict[str, str] = {}

        def two() -> bool:
            if held.done():
                raise RuntimeError("the load ended before two instances served: a held stream was refused")
            # One observation: the instances serving now and present now, never an earlier one's.
            serving = self.pool.serving()
            try:
                present = self.pool.present()
            except (Leaving, Unseen):
                return False
            now = {instance: zone for instance, zone in serving.items() if instance in present}
            # The zones are those the workspace labels: an instance whose series has no zone label
            # is not counted as a zone of its own, and the load is held until it has one.
            zoned = {instance: zone for instance, zone in now.items() if zone is not None}
            if len(now) < INSTANCES or len(zoned) < len(now):
                if len(now) >= INSTANCES:
                    print("two instances in service, one with no zone label: held until both have one")
                return False
            found.clear()
            found.update(zoned)
            return True

        try:
            await _wait(two, SCALE_OUT_WAIT_SECONDS, "two instances serving")
        finally:
            stop.set()
            await held
        await _wait(lambda: self.pool.busy() == 0, LOAD_END_WAIT_SECONDS, "the load to end")
        return found

    async def student(self, number: int, session: loadgen.Session) -> tuple[Counts, list[loadgen.Record]]:
        """Sends one student's requests of `session` one after another, and reads what each instance
        served; with the records of its requests."""
        start = time.time()
        records = await loadgen.replay(loadgen.Load((session,)), [self.keys[number - 1]], self.endpoint, at_once=1)
        _all_answered(records, self.requests)
        await asyncio.sleep(SETTLE_SECONDS)
        served = await asyncio.to_thread(self.pool.served, start, time.time())
        print(f"student {number}: {self.requests} sent, served as {sorted(served.values(), reverse=True)}")
        return Counts(self.requests, served), records

    async def phase(self, *, first_at_once: bool) -> PhaseCounts:
        """Each student's requests in turn, after the two students' first requests at once if asked,
        which then count among each student's requests of the phase (`with_openings`)."""
        size = self.requests + 1 if first_at_once else self.requests
        sessions = {student: _session(self.pool.name, size, next(self._conversations)) for student in STUDENTS}
        if not first_at_once:
            return {student: (await self.student(student, sessions[student]))[0] for student in STUDENTS}
        opened = {student: Opened(sessions[student]) for student in STUDENTS}
        start = time.time()
        keys = [self.keys[student - 1] for student in STUDENTS]
        load = loadgen.Load(tuple(Opening(opened[student]) for student in STUDENTS))
        openings = await loadgen.replay(load, keys, self.endpoint, at_once=len(STUDENTS))
        _all_answered(openings, len(STUDENTS))
        await asyncio.sleep(SETTLE_SECONDS)
        window = await asyncio.to_thread(self.pool.served, start, time.time())
        print(f"first requests at once: served as {sorted(window.values(), reverse=True)}")
        counts: dict[int, Counts] = {}
        kept: dict[int, bool] = {}
        for position, student in enumerate(STUDENTS):
            counts[student], records = await self.student(student, Rest(opened[student]))
            kept[student] = kept_opening(openings[position], records[0])
            print(f"student {student}: its second request {'found' if kept[student] else 'missed'} its first's cache")
        return with_openings(window, counts, kept)

    def _in_service(self, instances: set[str], phase: Phase) -> None:
        """`Departed` unless every one of `instances` is still in service, none of them leaving."""
        try:
            present = self.pool.present()
        except (Leaving, Unseen) as changing:
            raise Departed(f"an instance began to leave during the phase {phase}") from changing
        if not instances <= present:
            raise Departed(f"an instance left during the phase {phase}")

    def _recheck(self, instances: set[str], phase: Phase) -> None:
        """After a split in `phase`, at whose end `instances` were in service and the scaler asked
        for them all: `Departed` if, in the next `SPLIT_RECHECK_SECONDS`, one of them stops writing,
        or another appears, while the scaler asks for as many. A scale-in the scaler asks for in that
        time began after the phase: the split stands."""
        for _ in range(max(1, math.ceil(SPLIT_RECHECK_SECONDS / POLL_SECONDS))):
            time.sleep(POLL_SECONDS)
            try:
                present = self.pool.present()
            except Leaving as leaving:
                if leaving.present != instances:
                    raise Departed(f"an instance changed after the phase {phase}") from leaving
                print(f"the scaler began to take an instance away after the phase {phase}: the split stands")
                return
            except Unseen as unseen:
                raise Departed(f"an instance stopped writing after the phase {phase}") from unseen
            if present != instances:
                raise Departed(f"an instance changed after the phase {phase}")

    def measured(self, phase: Phase, *, first_at_once: bool, in_service: set[str]) -> PhaseCounts:
        """The phase, measured again while disturbed, with `in_service` all in service at its end."""

        def measure() -> PhaseCounts:
            self._gateway_holds()
            counts = asyncio.run(self.phase(first_at_once=first_at_once))
            self._in_service(in_service, phase)
            if any(held.most_served < held.sent for held in counts.values()):
                # A split is a failure only if no instance was leaving: look again once a departure
                # that began during the phase has had time to show in the workspace.
                print(f"a student's requests were split in the phase {phase}: in service checked again")
                self._recheck(in_service, phase)
            return counts

        return measure_undisturbed(phase, measure, self.rebalance_number)

    def measure(self) -> dict[str, int | float]:
        # Read before the load, so that a number the scaler does not change during it needs no wait.
        self.rebalance_number()
        found = asyncio.run(self.two_instances())
        zones = len(set(found.values()))
        print(f"two instances in service, in {zones} zone(s)")
        before = self.measured(Phase.BEFORE, first_at_once=True, in_service=set(found))
        remaining: set[str] = set()

        def departed() -> bool:
            remaining.clear()
            try:
                remaining.update(self.pool.present())
            except (Leaving, Unseen):
                # Still draining, or a sample gone stale while the scaler asks for two.
                return False
            if not remaining:
                raise RuntimeError("the pool went to no instance before the departure was measured")
            return len(remaining) == 1

        asyncio.run(_wait(departed, DEPARTURE_WAIT_SECONDS, "the scaler to take the pool to one instance"))
        if not remaining <= set(found):
            raise RuntimeError("the instance that remains is not one of the two measured")
        print("one instance remains")
        after = self.measured(Phase.AFTER, first_at_once=False, in_service=remaining)
        if any(set(counts.served) - remaining for counts in after.values()):
            raise RuntimeError("an instance other than the remaining one served after the departure")
        (stayed,) = remaining
        for student in STUDENTS:
            on = instance_of(before[student])
            print(
                f"student {student}: before the departure on {on or 'more than one instance'}"
                f"{'' if on is None else ', which stayed' if on == stayed else ', which left'}; after it on "
                f"{'the remaining instance' if instance_of(after[student]) == stayed else 'more than one instance'}"
            )
        numbers = numbers_of(zones, {Phase.BEFORE: before, Phase.AFTER: after})
        if passes(numbers) and not exercised(before, remaining):
            raise NotExercised(
                "both students were on one instance before the departure: not one moved while one stayed"
            )
        return numbers


def main(environ: Mapping[str, str] = os.environ) -> Verdict:
    """Runs the spike with the job's environment and credentials, and posts its numbers."""

    def measure() -> dict[str, int | float]:
        name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
        (pool,) = [pool for pool in config.read_ssm().pools if pool.name == name]
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        table = session.resource("dynamodb").Table(environ[POOL_STATE_TABLE_VARIABLE])
        requests = int(environ[REQUESTS_VARIABLE])
        if requests < 1:
            raise ValueError("each student sends one request or more")
        keys = common.canary_keys().students
        return Run(
            Pool(name, workspace, table), environ[ENDPOINT_VARIABLE], keys, requests, pool.target_concurrency
        ).measure()

    return common.run_spike(SPIKE, JOB, measure, environ=environ)


if __name__ == "__main__":
    main()
