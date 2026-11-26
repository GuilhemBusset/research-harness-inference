"""Drives one cell of a frozen plan on the student gateway: the replay of a sweep, of a class
opening, of the warm-up, and the background of a live trial
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol").

`run_cell` sends the conversations of one `plan.PlannedCell` through the gateway as canary
students do, and returns what it sent and when: a `contracts.RequestRecord` for each request, the
cell's `contracts.CellRecord`, and the driver's own `Summary`. It holds no AWS material and reads
no telemetry: the hosts of a cell and its counters are read elsewhere.

**Seats and identities.** A seat is a student identity: one canary key, and with it the student's
concurrency limit, cache salt and gateway affinity. Slot `s` of a cell is a conversation of seat
`s // n`, where `n` is the suite's conversations of the cell's shape: one in hands-on, two in
several-agents, which therefore advance independently under one key. Seat `i` has key `i` of the
keys given, each counted once, and is student `i` in the records; a key is never recorded. A live cell reserves its
first seat (`reserved_seats`) for the live task and whatever that task fans out to: the replay
takes the seats after it and sends nothing with that seat's key.

**Independent conversations.** Each slot runs on its own: its conversation's first request is
sent at the slot's planned arrival, and every later one when the stream of the request before has
ended and the conversation's pause for tools, fixed in its trace, has passed. Nothing a slot
waits for is another slot's, so a slow seat holds up no other seat. A conversation that sent
every turn and got each answer whole is completed. One whose request failed, or that was still
under way when the cell stopped sending, is abandoned: a transcript does not go on from an answer
that did not come. A slot then starts a replacement, after a pause drawn from the cell's schedule
seed inside the range of the tool pauses, so the start of every conversation is fixed by the plan
and by when the one before ended.

**The conversations** are `traces.build` under the cell's input seed and prefix namespace, so
every conversation of a cell starts with the same bytes (`Trace.prefix`), a cell of another
repetition, the warm-up and each opening start with others, and two treatments of one plan are
sent the same bytes. Conversation `k` of slot `s` is number `s + k * slots` in the records. The
text of a conversation is drawn under that number and the cell's, so no cell is sent the
transcript an earlier cell of its repetition already left in an engine's cache. Their sizes are
the plan's (`sizes_of`): its suite's `replay` section, which the plan's protocol digest covers, so
the requests on the wire are the workload the digest names. A cell sent with other sizes, as a
rehearsal against a local mock may be, is flagged `missing_evidence` and does not count.

**When a cell sends, and when it stops.** What ends the sending depends on the phase:

- a sweep cell sends for its `admissions_ms`, and replaces conversations;
- a class opening sends for its window, one conversation for each slot at its seeded arrival, and
  replaces none: the count that starts is the planned one. A conversation that started goes on
  with its later turns while the window lasts, as a student who started early does;
- the warm-up and the background of a live cell have no window of their own. They send, with
  replacements, until `Stops.finished` is set: by whoever saw each host show work, or saw the
  live trials end, so a background outlasts the trial it loads. The warm-up's planned admissions
  are zero and are not a window. Such a cell given neither that signal nor a deadline is refused
  (`Refusal.UNBOUNDED_CELL`).

Once the sending has ended no request is dispatched, and the streams still open have the cell's
`drain_ms` to end; those open at that instant are cut, and their conversations are abandoned.
`Stops.cancel` and `Stops.deadline_ms` stop a cell at once, whatever its phase: nothing more is
sent, open streams are cut, and the cell is recorded incomplete. Each is read again at the
instant of every dispatch, so a cell that starts cancelled, past its deadline or already finished
sends nothing. Cancelling the task that runs `run_cell` also stops every request before the
cancellation goes on.

**The driver's lag.** A request is eligible at a time the plan and the answers fix; `sent_ms` is
when the driver dispatched it, and the difference is the driver's own delay, whatever the server
did. A cell whose lag at the 95th percentile (`common.percentile`) is above the suite's tolerance
is flagged `driver_saturation`. A conversation that was eligible before the sending ended and
that the driver did not start is a skipped start: it is planned and not started, and the cell
does not count.

**Keys.** `preflight` counts the keys a plan needs, for each cohort it is run with: a paired
treatment on a reused engine takes a cohort of its own. Fewer distinct keys are refused before
anything is sent (`Refusal.INSUFFICIENT_KEYS`), by `run_cell` as well. A key is counted as the
gateway reads it: an entry that is not a well-formed key (`rhi_common.keys.is_valid_key`) is no
key, whether it is empty, junk, or a key with a space beside it, which the gateway would take for
the key itself, so that two seats would be one student.

**The fresh prefix.** The gateway keeps a cache salt for each student, so what one student left
in a cache says nothing of another's: a cell counts only when every one of its students met its
prefix uncached, and only what was observed shows that. A student's place in the plan shows
nothing: one whose every request an earlier cell saw refused has met no cache there. It is read
from served usage: of the student's first answers under the prefix, those of requests sent while
no answer to that student had ended, one must report a cached input of exactly zero. A count
above zero, however small, may be the namespace read back and is not told apart from cached
framing; an answer with no count is no evidence, and what a later request reports says nothing
of the first. The student's other first answer may report a cached input: it can read what its
twin just wrote. A request that brought nothing of an answer back, refused by the gateway or
lost or cut before a count or an output came, is no first answer: the question stays open until
one comes, in that cell or in a later one. `Evidence` holds what the cells driven with it have
shown, for each key on a gateway, pool and prefix, and takes it from the records of requests
alone; a caller passes one to every cell of a run. A cell given none starts with nothing shown,
so each of its students must show it there. A class opening is where its students meet its prefix:
it is the fresh prefix on a warm engine only if each of them shows it in that very cell, so what an
earlier try of the opening showed, cancelled or not, counts for nothing in the next, and a try
whose student reads the prefix back does not count. A cell with a student who has not shown it is
flagged `missing_evidence` and does not count.

**What is recorded** is ordinals, times on the run's clock, counts and labels: no key, no host,
no text of a request or an answer. `Summary` tells the three kinds of traffic apart (`Segment`),
and holds, beside the planned arrivals and coverage, what was observed of them. A request the
driver cut is recorded as far as its stream had come, and `Summary.cut` names it. No finish reason
is read here: `finish_reason` is written as missing. Every request that was dispatched has its
record: a record that cannot be written (`contracts.ContractError`, as for an ordinal past the
last one) ends the cell, and `run_cell` raises it instead of returning a cell with a part of its
requests.

**The address** is the gateway stack's output, a host name, or an `https://` address
(`common.gateway_address`): a canary key is sent over HTTPS only, and `Gateway` is not made for
any other address.

It needs `spikes` and `services/gateway` on the path, as `evaluate.streams` does, and what
`spikes/common.py` imports.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import httpx

import loadgen
from common import gateway_address, percentile
from evaluate import traces
from evaluate.contracts import (
    LIVE_PHASES,
    CachePolicy,
    CellRecord,
    CellStatus,
    InvalidFlag,
    Phase,
    RequestOutcome,
    RequestRecord,
    Route,
    Traffic,
)
from evaluate.streams import Attempt, Attempts, Tally
from rhi_common.keys import is_valid_key, key_hash

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable, Sequence

    from evaluate.plan import Plan, PlannedCell

# A trace is a conversation on Messages (`evaluate.traces`).
ROUTE = Route.MESSAGES
# The numbers a cell's conversations are drawn under are apart by this from the next cell's.
CONVERSATIONS = 1_000_000


class Refusal(StrEnum):
    """Why nothing was sent."""

    INSUFFICIENT_KEYS = "insufficient-keys"
    UNBOUNDED_CELL = "unbounded-cell"
    UNSUPPORTED_ROUTE = "unsupported-route"


class ReplayError(Exception):
    """A replay that is refused before it sends, with its reason."""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Segment(StrEnum):
    """The kind of traffic a cell is. Only `measured` enters a steady-state summary."""

    WARM_UP = "warm_up"
    OPENING = "opening"
    MEASURED = "measured"


class Stop(StrEnum):
    """What ended a cell."""

    ADMISSIONS_ENDED = "admissions_ended"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"


class Fresh(StrEnum):
    """Whether served usage showed the cell's prefix uncached for each student of the cell who had
    it to show: in a class opening all of them, in another cell those who had not shown it yet. A
    cell whose students all showed it in earlier cells has nothing to show."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NOT_REQUIRED = "not_required"


class Clock(Protocol):
    """The run's clock, which every recorded time is read on."""

    def now_ms(self) -> int:
        """Milliseconds since the start of the run. It does not go back."""
        ...

    async def sleep_ms(self, ms: int) -> None: ...


class RunClock:
    """The machine's monotonic clock, counted from when the run made it."""

    def __init__(self) -> None:
        self._origin = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self._origin) * 1000)

    async def sleep_ms(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)


@dataclass(frozen=True)
class Gateway:
    """The student gateway at `endpoint` (`common.gateway_address`), the pool `model` names, and
    the clock of the run. `transport` is what requests are sent through: httpx's own unless given.
    ValueError for an address that is not reached over HTTPS: no key is sent in the clear."""

    endpoint: str
    model: str
    clock: Clock
    transport: httpx.AsyncBaseTransport | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", gateway_address(self.endpoint))


@dataclass(frozen=True)
class Stops:
    """What stops a cell from outside. `finished` ends the sending of a warm-up or of a live
    cell's background, which then drains. `cancel`, and `deadline_ms` on the run's clock, stop any
    cell at once."""

    finished: asyncio.Event | None = None
    cancel: asyncio.Event | None = None
    deadline_ms: int | None = None


@dataclass(frozen=True)
class Arrival:
    """The first conversation of a slot: when the plan made it eligible, and when its first
    request was dispatched, `None` for a slot that sent nothing."""

    slot: int
    student: int
    planned_ms: int
    observed_ms: int | None


@dataclass(frozen=True)
class Summary:
    """What the driver planned for a cell and what it observed, beside the cell's record."""

    segment: Segment
    traffic: Traffic
    stopped_by: Stop
    cache_policy: CachePolicy
    # The hash of the bytes every conversation of the cell starts with; `None` with no slot to replay.
    prefix_hash: str | None
    # Whether the conversations were drawn to the plan's sizes. A cell sent with others does not count.
    sizes_planned: bool
    # The students who had the fresh prefix to show in the cell, and whether usage showed it for each:
    # every student of a class opening, elsewhere those nothing had shown fresh when the cell started.
    fresh_students: tuple[int, ...]
    fresh_prefix: Fresh
    arrivals: tuple[Arrival, ...]
    # Coverage: what the plan asked of the cell, and what the cell came to.
    slots_planned: int
    slots_started: int
    identities_planned: int
    identities_used: int
    # `None` for a cell with no window of its own: a warm-up, a live cell's background.
    admissions_planned_ms: int | None
    admissions_run_ms: int
    # Every request sent, by how it ended; the requests the driver itself cut are among the failed.
    tally: Tally
    cut: tuple[int, ...]
    # Eligibility to dispatch, over every request; `None` when none was sent.
    lag_p95_ms: float | None
    lag_max_ms: int | None
    lag_tolerance_ms: int
    # Fan-out: the most requests open at once, in the cell and for any one student.
    peak_in_flight: int
    peak_student_in_flight: int


@dataclass(frozen=True)
class CellRun:
    """What a cell left: its record, a record for each request in the order they were sent, and
    the driver's summary."""

    record: CellRecord
    requests: tuple[RequestRecord, ...]
    summary: Summary

    @property
    def valid(self) -> bool:
        return self.record.valid_for_qualification


@dataclass(frozen=True)
class _Rule:
    segment: Segment
    traffic: Traffic
    # Whether the cell sends for its planned admissions; if not, until `Stops.finished`.
    window: bool
    replaces: bool


_BACKGROUND = _Rule(Segment.MEASURED, Traffic.BACKGROUND, window=False, replaces=True)
_RULES = {
    Phase.WARM_UP: _Rule(Segment.WARM_UP, Traffic.WARM_UP, window=False, replaces=True),
    Phase.CAPACITY_SWEEP: _Rule(Segment.MEASURED, Traffic.BACKGROUND, window=True, replaces=True),
    Phase.LIVE_LOW_LOAD: _BACKGROUND,
    Phase.LIVE_CLASS_LOAD: _BACKGROUND,
    Phase.CLASS_OPENING: _Rule(Segment.OPENING, Traffic.BACKGROUND, window=True, replaces=False),
}


# --- Seats, keys and the prefix --------------------------------------------------------------


def reserved_seats(cell: PlannedCell) -> int:
    """The seats a cell keeps for live work, which are its first: one in a live cell."""
    return 1 if cell.identity.phase in LIVE_PHASES else 0


def seat_of(plan: Plan, cell: PlannedCell, slot: int) -> int:
    """The seat replay slot `slot` of the cell belongs to: the student, and the key, it is sent as."""
    return reserved_seats(cell) + slot // plan.spec.conversations[cell.identity.shape]


def keys_needed(plan: Plan, *, cohorts: int = 1) -> int:
    """The distinct canary keys the plan needs: the most identities any cell uses at once, for
    each cohort. Two treatments on one reused engine are two cohorts, unless eviction is verified."""
    if cohorts < 1:
        raise ValueError("a plan runs with one cohort or more")
    return cohorts * plan.canary_identities


def _distinct(keys: Iterable[str]) -> tuple[str, ...]:
    """The keys given, each once, in order. An entry that is not a well-formed key is no key: the
    gateway refuses it, or reads it, with a space beside a key, as a key that is counted already."""
    return tuple(dict.fromkeys(key for key in keys if is_valid_key(key)))


def preflight(plan: Plan, keys: Sequence[str], *, cohorts: int = 1) -> tuple[tuple[str, ...], ...]:
    """The cohorts the keys give the plan, disjoint, each with a key for every identity.
    `ReplayError` when the distinct keys are too few."""
    distinct = _distinct(keys)
    if len(distinct) < keys_needed(plan, cohorts=cohorts):
        raise ReplayError(Refusal.INSUFFICIENT_KEYS)
    each = plan.canary_identities
    return tuple(distinct[at * each : (at + 1) * each] for at in range(cohorts))


def _pair(held: Sequence[int]) -> tuple[int, int]:
    low, high = held
    return low, high


def sizes_of(plan: Plan) -> traces.Sizes:
    """The sizes the plan's conversations are drawn to: its suite's `replay` section, which is in
    the plan's protocol digest. The turns of a conversation are spike S5's, as `evaluate.traces`
    has them. `ReplayError` for a suite that replays on another route than Messages."""
    table = plan.spec.protocol["replay"]
    if table["route"] != ROUTE:
        raise ReplayError(Refusal.UNSUPPORTED_ROUTE)
    return traces.Sizes(
        opening_tokens=table["opening_tokens"],
        transcript_tokens=_pair(table["transcript_tokens"]),
        turn_input_tokens=_pair(table["turn_input_tokens"]),
        turn_output_tokens=table["turn_output_tokens"],
        tool_delay_ms=_pair(table["tool_delay_ms"]),
    )


def _answered(one: RequestRecord) -> bool:
    """Whether anything of an answer came back: all of it, a count, or an output."""
    return one.outcome is RequestOutcome.COMPLETED or one.usage is not None or one.first_output_ms is not None


def met_fresh(requests: Iterable[RequestRecord]) -> bool | None:
    """Whether served usage shows that a student, whose requests under one prefix these are, met
    it uncached: one of the student's first answers, those of requests sent while no answer had
    ended, reported a cached input of exactly zero. `None` while no answer has come: a request
    that was refused, or lost or cut before a count or an output, shows nothing either way."""
    answers = [one for one in requests if _answered(one)]
    if not answers:
        return None
    first_end = min(one.ended_ms for one in answers)
    first = [one.usage for one in answers if one.sent_ms <= first_end]
    return any(usage is not None and usage.cached_input_tokens == 0 for usage in first)


class Evidence:
    """What served usage has shown, over the cells driven with it, of how each student first met
    each prefix. It is kept for the key on its gateway and pool, by the key's hash, and for the
    bytes of the prefix, by theirs: another cohort, engine or plan has shown nothing here. Once a
    student's first answers have come, what they showed stays."""

    def __init__(self) -> None:
        self._met: dict[tuple[str, str, str, str], bool] = {}

    @staticmethod
    def _mark(gateway: Gateway, key: str, prefix_hash: str) -> tuple[str, str, str, str]:
        return gateway.endpoint, gateway.model, key_hash(key), prefix_hash

    def fresh(self, gateway: Gateway, key: str, prefix_hash: str) -> bool:
        """Whether the student with that key has shown they met the prefix uncached."""
        return self._met.get(self._mark(gateway, key, prefix_hash), False)

    def read(self, gateway: Gateway, key: str, prefix_hash: str, requests: Iterable[RequestRecord]) -> None:
        """Takes what the student's requests under the prefix show (`met_fresh`), unless the
        student's first answers came before."""
        mark = self._mark(gateway, key, prefix_hash)
        if mark not in self._met and (fresh := met_fresh(requests)) is not None:
            self._met[mark] = fresh


def _derive(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256(json.dumps(parts).encode()).digest()[:8])


def replacement_pause_ms(cell: PlannedCell, sizes: traces.Sizes, slot: int, conversation: int) -> int:
    """The pause of a slot before its conversation `conversation`, a replacement: from the cell's
    schedule seed, inside the range of the tool pauses."""
    low, high = sizes.tool_delay_ms
    return low + _derive("replacement", cell.schedule_seed, cell.identity.cell, slot, conversation) % (high - low + 1)


# --- Driving a cell --------------------------------------------------------------------------


async def _first[Key](ends: Sequence[tuple[Key, Coroutine[Any, Any, object]]]) -> Key:
    """Waits for the first of them to end, stops the others, and gives its key: the earliest
    listed, when several ended together."""
    tasks = [(key, asyncio.ensure_future(one)) for key, one in ends]
    try:
        await asyncio.wait([task for _, task in tasks], return_when=asyncio.FIRST_COMPLETED)
        return next(key for key, task in tasks if task.done())
    finally:
        for _, task in tasks:
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)


@dataclass
class _Slot:
    slot: int
    student: int
    conversations: int = 0
    # When the conversation the slot has not started yet is eligible.
    waiting_ms: int | None = None
    in_flight: bool = False
    first_sent_ms: int | None = None


class _Drive:
    """One cell while it runs."""

    def __init__(
        self,
        plan: Plan,
        cell: PlannedCell,
        gateway: Gateway,
        keys: Sequence[str],
        stops: Stops,
        sizes: traces.Sizes,
        first_request: int,
        evidence: Evidence,
        started: asyncio.Event | None = None,
    ) -> None:
        self.plan, self.cell, self.gateway, self.keys, self.stops, self.sizes = plan, cell, gateway, keys, stops, sizes
        self.evidence = evidence
        self.started = started
        self.clock = gateway.clock
        self.rule = _RULES[cell.identity.phase]
        self.course = traces.read_course()
        self.slots = [_Slot(slot, seat_of(plan, cell, slot)) for slot in range(cell.replay_slots)]
        self.attempts = Attempts()
        self.requests: list[RequestRecord] = []
        self.cut: list[int] = []
        self.next_request = first_request
        self.conversations: Counter[str] = Counter()
        self.open: Counter[int] = Counter()
        self.peak = self.peak_student = 0
        self.started_ms = 0
        self.window_ends_ms: int | None = None
        self.closed = False

    def conversation(self, slot: int, ordinal: int) -> tuple[int, traces.Trace]:
        """Conversation `ordinal` of a slot: its number in the records, and its trace."""
        cell = self.cell
        number = slot + ordinal * cell.replay_slots
        drawn = cell.identity.cell * CONVERSATIONS + number
        return number, traces.build(
            self.gateway.model, cell.input_seed, cell.prefix_namespace, drawn, sizes=self.sizes, course=self.course
        )

    async def run(self) -> CellRun:
        # The first conversations are built before the cell starts: building one takes no time from it.
        first = [self.conversation(one.slot, 0) for one in self.slots]
        prefix = first[0][1].record()["prefix_hash"] if first else None
        gateway, clock = self.gateway, self.clock
        # Nothing of the machine's own configuration takes part, and no slot waits for a connection.
        limits = httpx.Limits(max_connections=None)
        async with httpx.AsyncClient(
            base_url=gateway.endpoint,
            timeout=loadgen.TIMEOUT,
            limits=limits,
            transport=gateway.transport,
            trust_env=False,
        ) as http:
            self.started_ms = clock.now_ms()
            if self.started is not None:
                self.started.set()
            if self.rule.window:
                self.window_ends_ms = self.started_ms + self.cell.admissions_ms
            slots = [
                asyncio.ensure_future(self._slot(http, one, *conversation))
                for one, conversation in zip(self.slots, first, strict=True)
            ]
            supervisor = asyncio.ensure_future(self._supervise(slots))
            try:
                # Until the cell has ended, or a slot has failed: a cell does not go on past a
                # request it could not record.
                running = {supervisor, *slots}
                while supervisor in running:
                    done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    if any(not task.cancelled() and task.exception() is not None for task in done):
                        break
            finally:
                # Whatever ends the cell, a cancellation of this task included, stops every request.
                for task in (supervisor, *slots):
                    task.cancel()
                await asyncio.gather(supervisor, *slots, return_exceptions=True)
            for task in slots:
                if not task.cancelled() and (failure := task.exception()) is not None:
                    raise failure
            stop, closed_ms = supervisor.result()
            return self._result(stop, closed_ms, clock.now_ms(), prefix)

    async def _supervise(self, tasks: list[asyncio.Task[None]]) -> tuple[Stop, int]:
        """Ends the sending, then the drain: what stopped the cell, and when its sending ended."""
        stops, clock = self.stops, self.clock

        def at_once() -> list[tuple[Stop, Coroutine[Any, Any, object]]]:
            ends: list[tuple[Stop, Coroutine[Any, Any, object]]] = []
            if stops.cancel is not None:
                ends.append((Stop.CANCELLED, stops.cancel.wait()))
            if stops.deadline_ms is not None:
                ends.append((Stop.DEADLINE, self._until(stops.deadline_ms)))
            return ends

        ends = at_once()
        if self.window_ends_ms is not None:
            ends.append((Stop.ADMISSIONS_ENDED, self._until(self.window_ends_ms)))
        elif stops.finished is not None:
            ends.append((Stop.FINISHED, stops.finished.wait()))
        stop = await _first(ends)
        self.closed = True
        # A window ends when planned, however late the driver saw it: nothing was sent after it.
        closed_ms = clock.now_ms() if self.window_ends_ms is None else min(self.window_ends_ms, clock.now_ms())
        if stop in (Stop.CANCELLED, Stop.DEADLINE):
            return stop, closed_ms
        # A slot that waits has nothing left to send. One with a stream open has the drain to end it.
        for task, one in zip(tasks, self.slots, strict=True):
            if not one.in_flight:
                task.cancel()
        draining = [task for task, one in zip(tasks, self.slots, strict=True) if one.in_flight]
        if draining:
            drained = [(stop, asyncio.wait(draining)), (stop, self._until(closed_ms + self.cell.drain_ms))]
            stop = await _first([*drained, *at_once()])
        return stop, closed_ms

    async def _until(self, at_ms: int) -> None:
        while (wait := at_ms - self.clock.now_ms()) > 0:
            await self.clock.sleep_ms(wait)

    def _stopped(self) -> bool:
        """Whether the sending has ended, as read at this instant: the supervisor may not have
        run since a signal was given, and a driver that wakes after its window sends nothing late."""
        stops, now = self.stops, self.clock.now_ms()
        if self.closed or (stops.cancel is not None and stops.cancel.is_set()):
            return True
        if stops.deadline_ms is not None and now >= stops.deadline_ms:
            return True
        if self.window_ends_ms is not None:
            return now >= self.window_ends_ms
        return stops.finished is not None and stops.finished.is_set()

    async def _reach(self, eligible_ms: int) -> bool:
        """Waits for an eligible time. Whether a request may then be dispatched: none is once
        the sending has ended."""
        if self._stopped():
            return False
        await self._until(eligible_ms)
        return not self._stopped()

    async def _slot(self, http: httpx.AsyncClient, one: _Slot, number: int, trace: traces.Trace) -> None:
        """One slot: its conversation at its planned arrival, then, where the phase replaces, the next ones."""
        eligible = self.started_ms + self.cell.arrivals_ms[one.slot]
        while True:
            one.waiting_ms = eligible
            if not await self._reach(eligible):
                return
            one.waiting_ms = None
            one.conversations += 1
            await self._converse(http, one, number, trace, eligible)
            if not self.rule.replaces or self.closed:
                return
            eligible = self.clock.now_ms() + replacement_pause_ms(self.cell, self.sizes, one.slot, one.conversations)
            one.waiting_ms = eligible
            number, trace = self.conversation(one.slot, one.conversations)

    async def _converse(
        self, http: httpx.AsyncClient, one: _Slot, number: int, trace: traces.Trace, eligible: int
    ) -> None:
        """One conversation, turn by turn: each request after the answer before it and its pause."""
        self.conversations["started"] += 1
        completed = False
        try:
            for turn, (exchange, requested) in enumerate(zip(trace.exchanges, trace.requested, strict=True)):
                if turn:
                    eligible += requested.pause_ms
                    if not await self._reach(eligible):
                        return
                attempt = await self._send(http, one, number, turn, exchange.request, eligible)
                if attempt.failed:
                    return
                eligible = attempt.ended_ms
            completed = True
        finally:
            self.conversations["completed" if completed else "abandoned"] += 1

    async def _send(
        self, http: httpx.AsyncClient, one: _Slot, number: int, turn: int, request: loadgen.Request, eligible: int
    ) -> Attempt:
        """Sends one request as the slot's student and reads its stream to the end, or to the cut."""
        clock = self.clock
        sent = clock.now_ms()
        ordinal, self.next_request = self.next_request, self.next_request + 1
        reader = self.attempts.send(ROUTE, sent)
        one.in_flight = True
        one.first_sent_ms = sent if one.first_sent_ms is None else one.first_sent_ms
        self.open[one.student] += 1
        self.peak = max(self.peak, self.open.total())
        self.peak_student = max(self.peak_student, self.open[one.student])
        headers = {**request.headers, "authorization": f"Bearer {self.keys[one.student]}"}
        try:
            async with http.stream(request.method, request.path, headers=headers, content=request.body) as answer:
                reader.answered(answer.status_code)
                async for chunk in answer.aiter_bytes():
                    reader.feed(chunk, clock.now_ms())
        except httpx.HTTPError:
            reader.end(clock.now_ms(), transport_error=True)
        except asyncio.CancelledError:
            self.cut.append(ordinal)
            raise
        finally:
            # The attempt as it ended, or, for a stream the driver cut, as far as it had come.
            attempt = reader.end(clock.now_ms())
            one.in_flight = False
            self.open[one.student] -= 1
            self.requests.append(
                RequestRecord(
                    request=ordinal,
                    cell=self.cell.identity.cell,
                    student=one.student,
                    conversation=number,
                    turn=turn,
                    traffic=self.rule.traffic,
                    trial=None,
                    eligible_ms=eligible,
                    finish_reason=None,
                    **attempt.measured(),
                )
            )
        return attempt

    def _shown(self, requests: Sequence[RequestRecord], prefix: str | None) -> tuple[tuple[int, ...], Fresh]:
        """The students of the cell who had the fresh prefix to show in it, and whether usage has
        shown it for each of them. Outside a class opening they are those nothing had shown fresh
        when the cell started, and what the cell's requests show is kept in the evidence. In a
        class opening they are all its students, and only the cell's own requests show it: an
        earlier try of the opening shows nothing of how this one started."""
        if prefix is None:
            return (), Fresh.NOT_REQUIRED
        gateway, keys = self.gateway, self.keys
        evidence = Evidence() if self.rule.segment is Segment.OPENING else self.evidence
        students = sorted({one.student for one in self.slots})
        owed = tuple(student for student in students if not evidence.fresh(gateway, keys[student], prefix))
        for student in owed:
            evidence.read(gateway, keys[student], prefix, [one for one in requests if one.student == student])
        if not owed:
            return owed, Fresh.NOT_REQUIRED
        shown = all(evidence.fresh(gateway, keys[student], prefix) for student in owed)
        return owed, Fresh.VERIFIED if shown else Fresh.UNVERIFIED

    def _result(self, stop: Stop, closed_ms: int, ended_ms: int, prefix: str | None) -> CellRun:
        plan, cell, rule = self.plan, self.cell, self.rule
        requests = tuple(sorted(self.requests, key=lambda one: one.request))
        lags = [one.sent_ms - one.eligible_ms for one in requests]
        lag_p95 = percentile(lags, 95) if lags else None
        tolerance = plan.spec.protocol["driver"]["scheduling_lag_p95_ms_max"]
        students = sorted({one.student for one in self.slots})
        fresh_students, fresh = self._shown(requests, prefix)
        sizes_planned = self.sizes == sizes_of(plan)
        flags = set()
        if lag_p95 is not None and lag_p95 > tolerance:
            flags.add(InvalidFlag.DRIVER_SATURATION)
        if fresh is Fresh.UNVERIFIED or not sizes_planned:
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        # Planned and not started: a slot's first conversation, or a replacement that was eligible
        # before the sending ended.
        skipped = sum(
            one.waiting_ms is not None and (not one.conversations or one.waiting_ms < closed_ms) for one in self.slots
        )
        counts = self.conversations
        record = CellRecord(
            cell=cell.identity.cell,
            started_ms=self.started_ms,
            admissions_ended_ms=closed_ms,
            ended_ms=ended_ms,
            status=CellStatus.INCOMPLETE if stop in (Stop.CANCELLED, Stop.DEADLINE) else CellStatus.COMPLETE,
            invalid_flags=tuple(sorted(flags)),
            conversations_planned=counts["started"] + skipped,
            conversations_started=counts["started"],
            conversations_completed=counts["completed"],
            conversations_abandoned=counts["abandoned"],
            hosts_observed=None,
            driver_lag_p95_ms=lag_p95,
        )
        summary = Summary(
            segment=rule.segment,
            traffic=rule.traffic,
            stopped_by=stop,
            cache_policy=plan.spec.cache_policy,
            prefix_hash=prefix,
            sizes_planned=sizes_planned,
            fresh_students=fresh_students,
            fresh_prefix=fresh,
            arrivals=tuple(
                Arrival(one.slot, one.student, self.started_ms + cell.arrivals_ms[one.slot], one.first_sent_ms)
                for one in self.slots
            ),
            slots_planned=cell.replay_slots,
            slots_started=sum(one.first_sent_ms is not None for one in self.slots),
            identities_planned=len(students),
            identities_used=len({one.student for one in requests}),
            admissions_planned_ms=cell.admissions_ms if rule.window else None,
            admissions_run_ms=closed_ms - self.started_ms,
            tally=self.attempts.tally(),
            cut=tuple(sorted(self.cut)),
            lag_p95_ms=lag_p95,
            lag_max_ms=max(lags, default=None),
            lag_tolerance_ms=tolerance,
            peak_in_flight=self.peak,
            peak_student_in_flight=self.peak_student,
        )
        return CellRun(record, requests, summary)


async def run_cell(
    plan: Plan,
    cell: int,
    gateway: Gateway,
    keys: Sequence[str],
    *,
    stops: Stops | None = None,
    sizes: traces.Sizes | None = None,
    first_request: int = 0,
    evidence: Evidence | None = None,
    started: asyncio.Event | None = None,
) -> CellRun:
    """Drives cell `cell` of the plan on the gateway and returns what it left.

    `keys` are the canary students' keys of this treatment's cohort, one seat each, in order.
    `sizes` are the sizes its conversations are drawn to: the plan's (`sizes_of`) unless given,
    and a cell sent with others does not count. `first_request` is the ordinal of the first
    request it sends. `evidence` is what the cells driven before have shown of each student's
    prefix cache, and takes what this one shows: without it nothing was shown before, and every
    student of the cell must meet the prefix uncached here, as every student of a class opening
    must whatever was shown before. `started`, when given, is set at the instant the cell starts,
    its record's `started_ms`: before any of its requests is sent.

    `ReplayError`, with nothing sent, for fewer distinct keys than the cell has identities, for a
    warm-up or a live cell that nothing would end, and for a suite that replays on another route.
    `contracts.ContractError` for a request that cannot be recorded, once it has stopped the others.
    """
    planned = plan.cells[cell]
    stops = Stops() if stops is None else stops
    seats = _distinct(keys)
    if len(seats) < planned.identities:
        raise ReplayError(Refusal.INSUFFICIENT_KEYS)
    if not _RULES[planned.identity.phase].window and stops.finished is None and stops.deadline_ms is None:
        raise ReplayError(Refusal.UNBOUNDED_CELL)
    drawn = sizes_of(plan)
    drive = _Drive(
        plan,
        planned,
        gateway,
        seats,
        stops,
        drawn if sizes is None else sizes,
        first_request,
        Evidence() if evidence is None else evidence,
        started,
    )
    return await drive.run()
