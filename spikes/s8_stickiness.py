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
   shows two instances serving: the scaler adds the second once the load is above target for 60
   seconds. It records how many zones the two are in, by the label `zone` the node's collector
   adds to every series. Then it stops the load and waits until the workspace shows nothing running
   or waiting on the pool.
2. **Before the departure.** The first two canary students send their first requests at once, so
   that least-outstanding routing places them on different instances: whichever instance leaves,
   one of them has to move. Then each student in turn sends the number of requests it is given, one
   after another, and the job reads how many of them each instance served.
3. **After the departure.** It sends nothing until the scaler has taken the pool to one instance
   (no request for 10 minutes, and 15 since the scale-out, section 5.1), then each student sends
   its requests again, and every one of them must have reached the instance that stayed.

**Which instance served** is read from the workspace: the count of the mock's time-to-first-token
histogram, one per request, on each `instance` series (section 7.2). A student's window opens before
its first request and closes `SETTLE_SECONDS` after its last, so the collector's scrape has carried
the last count to the workspace.

**The rebalance number.** For each new rebalance number the gateway drops about one student in ten
of its cookies (section 5.5), which would move a student for a reason that is not the one measured.
One student sending one request after another never leaves an instance with waiting requests, so
the scaler has no cause to raise it, but the job checks: it reads the pool's number from the
scaler's note at the start and the end of each measured phase, and measures a phase again when it
changed. It does the same when a student's counts do not add up to the requests it sent, which is
another request on the pool during the window. After `ATTEMPTS` disturbed attempts it gives up, and
the run is not measured.

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
import math
import os
import time
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
from rhi_reporter.measure import Verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

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
# How many times a phase is measured before a disturbed one stops the run.
ATTEMPTS = 3
# The sizes of the requests: small, since only where each lands matters.
START_TOKENS = 200
TURN_INPUT_TOKENS = 50
OUTPUT_TOKENS = 64

# Seconds: a sample this close to an instant is the query's sample at that instant.
_SAME_INSTANT = 1.0
_SERVED = f"{metric_names(MOCK)[Quantity.TIME_TO_FIRST_TOKEN]}_count"
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

    @property
    def most_served(self) -> int:
        return max(self.served.values(), default=0)

    def adds_up(self) -> bool:
        """Whether the instances served exactly the student's requests: no other request came in its window."""
        return sum(self.served.values()) == self.sent


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


class Disturbed(RuntimeError):
    """A phase was disturbed on every attempt: the run is not a measurement."""


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

    def serving(self) -> dict[str, str]:
        """The instances that served a request in the last minutes, each with its zone."""
        end = time.time()
        series = self._read(f'{_SERVED}{{pool="{self.name}"}}', end - 2 * FRESH_SECONDS, end, FRESH_SECONDS)
        rising = served_between(series, end - 2 * FRESH_SECONDS, end)
        return {
            one.labels["instance"]: one.labels.get("zone", "") for one in series if rising.get(one.labels["instance"])
        }

    def present(self) -> set[str]:
        """The instances whose collector wrote a sample in the last `FRESH_SECONDS`."""
        end = time.time()
        series = self._read(f'timestamp({_RUNNING}{{pool="{self.name}"}})', end - FRESH_SECONDS, end, 15)
        return {one.labels["instance"] for one in series if one.samples and one.samples[-1][1] >= end - FRESH_SECONDS}

    def busy(self) -> float:
        """The requests running or waiting on the pool's instances, as the workspace last saw them."""
        end = time.time()
        query = f'sum({_RUNNING}{{pool="{self.name}"}}) + sum({_WAITING}{{pool="{self.name}"}})'
        series = self._read(query, end - FRESH_SECONDS, end, 15)
        return sum(one.samples[-1][1] for one in series if one.samples)

    def served(self, start: float, end: float) -> dict[str, int]:
        """Requests each instance served from `start` to `end`."""
        return served_between(self._read(f'{_SERVED}{{pool="{self.name}"}}', start, end, end - start), start, end)

    def rebalance_number(self) -> int:
        """The pool's rebalance number, from the scaler's note: 0 while it has none."""
        item = self.table.get_item(Key={"pool": self.name, "record": NOTE_RECORD}).get("Item")
        return 0 if item is None else ScalerNote.from_item(item).rebalance_number


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
            found.update(self.pool.serving())
            return len(found) >= INSTANCES

        try:
            await _wait(two, SCALE_OUT_WAIT_SECONDS, "two instances serving")
        finally:
            stop.set()
            await held
        await _wait(lambda: self.pool.busy() == 0, LOAD_END_WAIT_SECONDS, "the load to end")
        return found

    async def student(self, number: int, seed: int) -> Counts:
        """Sends one student's requests one after another, and reads what each instance served."""
        start = time.time()
        load = loadgen.Load((_session(self.pool.name, self.requests, seed),))
        _all_answered(await loadgen.replay(load, [self.keys[number - 1]], self.endpoint, at_once=1), self.requests)
        await asyncio.sleep(SETTLE_SECONDS)
        served = await asyncio.to_thread(self.pool.served, start, time.time())
        print(f"student {number}: {self.requests} sent, served as {sorted(served.values(), reverse=True)}")
        return Counts(self.requests, served)

    async def phase(self, *, first_at_once: bool) -> PhaseCounts:
        """Each student's requests in turn, after the two students' first requests at once if asked."""
        if first_at_once:
            keys = [self.keys[student - 1] for student in STUDENTS]
            firsts = loadgen.Load(tuple(_session(self.pool.name, 1, student) for student in STUDENTS))
            _all_answered(await loadgen.replay(firsts, keys, self.endpoint, at_once=len(STUDENTS)), len(STUDENTS))
            await asyncio.sleep(SETTLE_SECONDS)
        return {student: await self.student(student, seed=100 * student) for student in STUDENTS}

    def measured(self, phase: Phase, *, first_at_once: bool) -> PhaseCounts:
        def measure() -> PhaseCounts:
            return asyncio.run(self.phase(first_at_once=first_at_once))

        return measure_undisturbed(phase, measure, self.pool.rebalance_number)

    def measure(self) -> dict[str, int | float]:
        found = asyncio.run(self.two_instances())
        zones = len(set(found.values()))
        print(f"two instances in service, in {zones} zone(s)")
        before = self.measured(Phase.BEFORE, first_at_once=True)
        remaining: set[str] = set()

        def departed() -> bool:
            remaining.clear()
            remaining.update(self.pool.present())
            if not remaining:
                raise RuntimeError("the pool went to no instance before the departure was measured")
            return len(remaining) == 1

        asyncio.run(_wait(departed, DEPARTURE_WAIT_SECONDS, "the scaler to take the pool to one instance"))
        print("one instance remains")
        after = self.measured(Phase.AFTER, first_at_once=False)
        if any(set(counts.served) - remaining for counts in after.values()):
            raise RuntimeError("an instance other than the remaining one served after the departure")
        return numbers_of(zones, {Phase.BEFORE: before, Phase.AFTER: after})


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
