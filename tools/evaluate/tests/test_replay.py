"""The replay driver (`evaluate.replay`) on cells of the course suite's plan
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol"): what it sends as
which student and when, when it stops, and what it records of it.

The gateway is made up and answers on a clock the test moves; the keys are drawn while the tests
run. Nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
from collections import Counter
from dataclasses import dataclass, replace
from functools import cached_property
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from evaluate import plan, replay, traces
from evaluate.contracts import (
    CellStatus,
    ContractError,
    InvalidFlag,
    Phase,
    Profile,
    RequestOutcome,
    Shape,
    Traffic,
)
from evaluate.replay import Fresh, Refusal, Segment, Stop
from rhi_common.keys import generate_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Sequence

COURSE = plan.load_suite()


def suite(**replayed: Any) -> plan.SuiteSpec:
    """The course suite with a replay of short conversations, a few words to a request and short
    pauses for tools, and with what is given of its replay beside."""
    short = {
        "opening_tokens": 8,
        "transcript_tokens": [16, 32],
        "turn_input_tokens": [2, 6],
        "turn_output_tokens": 4,
        "tool_delay_ms": [500, 1000],
    }
    return replace(COURSE, protocol=COURSE.protocol | {"replay": COURSE.protocol["replay"] | short | replayed})


SPEC = suite()
# A class of three: its sweep has cells of four slots, its live cells two seats of background.
SCREEN = plan.expand(SPEC, Profile.SCREEN, 3)
# Three repetitions.
QUALIFY = plan.expand(SPEC, Profile.QUALIFY, 2)
KEYS = [generate_key() for _ in range(2 * SCREEN.canary_identities)]
# The sizes the plans froze.
SIZES = replay.sizes_of(SCREEN)
# The run is under way when a cell starts: its times are not counted from the cell.
START = 7_000
# Turns of the event loop that let every task go on until it waits on the clock.
YIELDS = 100
ANSWER_MS = 1_000
FOREVER_MS = 10**9


class Clock:
    """A clock the test moves: `drive` runs a coroutine, and each time every task of it waits,
    moves the time to the end of the earliest wait."""

    def __init__(self) -> None:
        self.at = START
        self._waits: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def now_ms(self) -> int:
        return self.at

    async def sleep_ms(self, ms: int) -> None:
        wait = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waits, (self.at + max(ms, 0), next(self._order), wait))
        await wait

    def drive[Result](self, work: Coroutine[Any, Any, Result]) -> Result:
        async def driven() -> Result:
            task = asyncio.ensure_future(work)
            while not task.done():
                for _ in range(YIELDS):
                    await asyncio.sleep(0)
                while self._waits and self._waits[0][2].cancelled():
                    heapq.heappop(self._waits)
                if task.done():
                    break
                assert self._waits, "a task waits on something the clock does not end"
                ends, _, wait = heapq.heappop(self._waits)
                self.at = max(self.at, ends)
                wait.set_result(None)
            return task.result()

        return asyncio.run(driven())


@dataclass
class Busy:
    """The clock as a driver with too much to do reads it: every wait ends `late_ms` late, and one
    that would end inside `stall` ends when the stall does."""

    clock: Clock
    late_ms: int = 0
    stall: tuple[int, int] | None = None

    def now_ms(self) -> int:
        return self.clock.now_ms()

    async def sleep_ms(self, ms: int) -> None:
        ends = self.clock.now_ms() + ms + self.late_ms
        if self.stall and self.stall[0] <= ends < self.stall[1]:
            ends = self.stall[1]
        await self.clock.sleep_ms(ends - self.clock.now_ms())


@dataclass(frozen=True)
class Arrived:
    """A request as the gateway got it: when, as which student, and its bytes."""

    at_ms: int
    student: int
    content: bytes

    @cached_property
    def body(self) -> dict[str, Any]:
        return json.loads(self.content)

    @property
    def turn(self) -> int:
        return sum(message["role"] == "assistant" for message in self.body["messages"])

    @property
    def prefix(self) -> bytes:
        return json.dumps({"system": self.body["system"], "tools": self.body["tools"]}).encode()


def event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name} | data)}\n\n".encode()


class Pool(httpx.AsyncBaseTransport):
    """A made-up gateway. It knows a student by the key, answers after `answer_ms(student)` (never,
    for `None`), refuses the students of `refused`, and reports a cached input as an engine with
    a prefix cache for each student does: nothing for the first request of a student under a
    prefix, unless `stale` gives what that request reports instead (`None` for no usage at all),
    and `later` for every later one."""

    def __init__(
        self,
        clock: Clock,
        keys: Sequence[str],
        answer_ms: Callable[[int], int | None] = lambda _student: ANSWER_MS,
        stale: dict[int, int | None] | None = None,
        refused: frozenset[int] = frozenset(),
        later: int = 12,
    ) -> None:
        self.clock, self.keys, self.answer_ms, self.stale = clock, list(keys), answer_ms, stale or {}
        self.refused, self.later = refused, later
        self.arrived: list[Arrived] = []
        self.open: Counter[int] = Counter()
        self.peak: Counter[int] = Counter()
        self._cached: set[tuple[int, bytes]] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        student = self.keys.index(request.headers["authorization"].removeprefix("Bearer "))
        arrived = Arrived(self.clock.now_ms(), student, request.content)
        self.arrived.append(arrived)
        if student in self.refused:
            return httpx.Response(503, json={"type": "error", "error": {"type": "no_session"}})
        cached = self.later if (student, arrived.prefix) in self._cached else self.stale.get(student, 0)
        self._cached.add((student, arrived.prefix))
        usage = {} if cached is None else {"usage": {"input_tokens": 30, "cache_read_input_tokens": cached}}
        wait = self.answer_ms(student)

        async def stream() -> AsyncIterator[bytes]:
            self.open[student] += 1
            self.peak[student] = max(self.peak[student], self.open[student])
            try:
                yield event("message_start", {"message": usage})
                await self.clock.sleep_ms(FOREVER_MS if wait is None else wait)
                yield event("content_block_delta", {"delta": {"type": "text_delta", "text": "made-up answer"}})
                yield event("message_delta", {"usage": {"output_tokens": 4}} if usage else {})
                yield event("message_stop", {})
            finally:
                self.open[student] -= 1

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())


def cell_of(planned: plan.Plan, phase: Phase, shape: Shape, slots: int | None = None, repetition: int = 0) -> int:
    """The first cell of the plan of that phase, shape and repetition, and of that many agent slots if given."""
    return next(
        cell.identity.cell
        for cell in planned.cells
        if (cell.identity.phase, cell.identity.shape, cell.identity.repetition) == (phase, shape, repetition)
        and slots in (None, cell.identity.agent_slots)
    )


def run(
    planned: plan.Plan,
    cell: int,
    *,
    keys: Sequence[str] = KEYS,
    pool: Pool | None = None,
    late_ms: int = 0,
    stall: tuple[int, int] | None = None,
    finished_at: int | None = None,
    cancel_at: int | None = None,
    deadline_ms: int | None = None,
    sizes: traces.Sizes | None = None,
    first_request: int = 0,
    **answers: Any,
) -> tuple[replay.CellRun, Pool]:
    """Drives a cell against a made-up gateway on a clock of the test: what it left, and what the
    gateway saw. `finished_at` and `cancel_at` are the times the two signals are given at: before
    the cell starts, for a time that is not after its start."""
    clock = pool.clock if pool else Clock()
    pool = pool or Pool(clock, keys, **answers)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", Busy(clock, late_ms, stall), pool)

    async def work() -> replay.CellRun:
        giving = []

        def signal(at_ms: int | None) -> asyncio.Event | None:
            """A signal that is given at that time, if one is."""
            if at_ms is None:
                return None
            given = asyncio.Event()

            async def give() -> None:
                await clock.sleep_ms(at_ms - clock.now_ms())
                given.set()

            if at_ms <= clock.now_ms():
                given.set()
            else:
                giving.append(asyncio.ensure_future(give()))
            return given

        stops = replay.Stops(finished=signal(finished_at), cancel=signal(cancel_at), deadline_ms=deadline_ms)
        return await replay.run_cell(
            planned, cell, gateway, keys, stops=stops, sizes=sizes, first_request=first_request
        )

    return clock.drive(work()), pool


def test_a_slow_seat_does_not_hold_up_another_seats_next_conversation() -> None:
    slow_ms = 200_000
    ran, _pool = run(
        SCREEN,
        cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4),
        answer_ms=lambda student: slow_ms if student == 0 else ANSWER_MS,
    )
    slow, quick = ([one for one in ran.requests if one.student == student] for student in (0, 1))
    held_until = slow[0].ended_ms
    assert held_until == START + slow_ms

    # While the slow seat's first answer was still coming, the other seat ended conversations and
    # started the next ones, each at the moment it was eligible.
    starts = [one for one in quick if one.turn == 0 and one.sent_ms < held_until]
    assert len({one.conversation for one in starts}) == len(starts) > 3
    assert all(one.sent_ms == one.eligible_ms for one in starts)
    # Its turns stay causal: each request after the end of the answer before it, and a pause for tools.
    low, high = SIZES.tool_delay_ms
    for before, later in pairwise(quick):
        assert low <= later.eligible_ms - before.ended_ms <= high
    # The slow seat's own next turn waited for its answer.
    assert slow[1].turn == 1
    assert slow[1].sent_ms >= held_until + low


def test_a_conversation_whose_request_failed_is_abandoned_and_replaced_after_a_pause() -> None:
    cell = cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4)
    ran, _pool = run(SCREEN, cell, refused=frozenset({0}))

    # No transcript goes on from a refusal: each of the seat's requests opens another conversation.
    refused = [one for one in ran.requests if one.student == 0]
    assert len({one.conversation for one in refused}) == len(refused) > 3
    assert all((one.turn, one.outcome) == (0, RequestOutcome.HTTP_REFUSAL) for one in refused)
    low, high = SIZES.tool_delay_ms
    for before, later in pairwise(refused):
        assert low <= later.sent_ms - before.ended_ms <= high
    assert ran.summary.tally.failures[RequestOutcome.HTTP_REFUSAL] == len(refused)
    assert ran.record.conversations_abandoned >= len(refused)
    assert ran.record.conversations_outstanding == 0


@pytest.mark.parametrize("shape", list(Shape))
def test_the_agents_of_a_seat_share_its_identity_and_each_seat_has_its_own(shape: Shape) -> None:
    cell = cell_of(SCREEN, Phase.CAPACITY_SWEEP, shape, 4)
    agents = SPEC.conversations[shape]
    seats = 4 // agents
    ran, pool = run(SCREEN, cell)
    planned = SCREEN.cells[cell]
    assert planned.identities == seats

    # When the cell starts the gateway gets one conversation for each slot: those of a seat under
    # one key, at once, and each seat under a key of its own.
    opening = [arrived for arrived in pool.arrived if arrived.at_ms == START]
    assert len({arrived.content for arrived in opening}) == 4
    assert Counter(arrived.student for arrived in opening) == dict.fromkeys(range(seats), agents)
    assert pool.peak == dict.fromkeys(range(seats), agents)
    # The records name the same students, and the fan-out the gateway saw.
    assert Counter(one.student for one in ran.requests) == Counter(arrived.student for arrived in pool.arrived)
    summary = ran.summary
    assert (summary.peak_in_flight, summary.peak_student_in_flight) == (4, agents)
    assert (summary.identities_planned, summary.identities_used) == (seats, seats)
    assert [(arrival.slot, arrival.student) for arrival in summary.arrivals] == [(at, at // agents) for at in range(4)]

    # The cell ran its admissions and no longer, and every conversation it started is accounted for.
    record = ran.record
    assert max(arrived.at_ms for arrived in pool.arrived) < record.admissions_ended_ms == START + planned.admissions_ms
    assert (summary.stopped_by, summary.segment, summary.traffic) == (
        Stop.ADMISSIONS_ENDED,
        Segment.MEASURED,
        Traffic.BACKGROUND,
    )
    assert record.conversations_planned == record.conversations_started > 4
    assert record.conversations_completed + record.conversations_abandoned == record.conversations_started
    assert summary.tally.attempts == summary.tally.completed == len(pool.arrived) == len(ran.requests)
    assert ran.valid


def test_a_live_cell_keeps_a_seat_for_the_live_task_and_its_background_lasts_until_the_task_ends() -> None:
    cell = cell_of(SCREEN, Phase.LIVE_CLASS_LOAD, Shape.SEVERAL_AGENTS)
    live_ends = START + 400_000
    ran, pool = run(SCREEN, cell, finished_at=live_ends)

    # The first seat is the live task's: no replay is sent under its key. The other two seats replay.
    assert replay.reserved_seats(SCREEN.cells[cell]) == 1
    assert {arrived.student for arrived in pool.arrived} == {1, 2}
    assert all(one.traffic is Traffic.BACKGROUND and one.trial is None for one in ran.requests)
    assert ran.summary.peak_student_in_flight == 2
    # The background was still being sent when the live work ended, and not after.
    assert live_ends - 15_000 < max(arrived.at_ms for arrived in pool.arrived) <= live_ends
    assert (ran.summary.stopped_by, ran.record.admissions_ended_ms) == (Stop.FINISHED, live_ends)
    assert ran.valid

    # With one seat there is nothing to load the live task with: the cell sends nothing.
    alone = plan.expand(SPEC, Profile.SCREEN, 1)
    ran, pool = run(alone, cell_of(alone, Phase.LIVE_LOW_LOAD, Shape.SEVERAL_AGENTS), finished_at=live_ends)
    assert (pool.arrived, ran.requests, ran.record.conversations_planned) == ([], (), 0)


def test_the_warm_up_is_sent_apart_until_the_hosts_show_work_and_is_refused_without_an_end() -> None:
    # The plan gives the warm-up no admissions: it lasts until it is told the hosts work.
    shown = START + 30_000
    ran, pool = run(SCREEN, 0, finished_at=shown)
    assert len(pool.arrived) > 1
    assert all(one.traffic is Traffic.WARM_UP for one in ran.requests)
    assert (ran.summary.segment, ran.summary.stopped_by) == (Segment.WARM_UP, Stop.FINISHED)
    assert max(arrived.at_ms for arrived in pool.arrived) < ran.record.ended_ms == shown

    # A deadline bounds it too. With neither, nothing would end it, and nothing is sent.
    ran, pool = run(SCREEN, 0, deadline_ms=shown)
    assert pool.arrived
    assert (ran.summary.stopped_by, ran.record.ended_ms) == (Stop.DEADLINE, shown)
    idle = Pool(Clock(), KEYS)
    with pytest.raises(replay.ReplayError) as refused:
        run(SCREEN, 0, pool=idle)
    assert refused.value.reason is Refusal.UNBOUNDED_CELL
    assert idle.arrived == []


@pytest.mark.parametrize("shape", list(Shape))
def test_a_class_opening_starts_each_planned_conversation_once_at_its_seeded_time(shape: Shape) -> None:
    cell = cell_of(SCREEN, Phase.CLASS_OPENING, shape)
    planned = SCREEN.cells[cell]
    # Answers quick enough that a conversation is over while the window lasts.
    ran, pool = run(SCREEN, cell, answer_ms=lambda _student: 100)

    first_turns = [arrived.at_ms - START for arrived in pool.arrived if arrived.turn == 0]
    assert first_turns == list(planned.arrivals_ms)
    assert len(first_turns) == planned.replay_slots == 3 * SPEC.conversations[shape]
    assert max(first_turns) < SPEC.opening_window_ms == 60_000
    # A slot whose conversation ended early started no other: the count that started is the planned one.
    record, summary = ran.record, ran.summary
    assert record.conversations_completed >= 1
    assert record.conversations_planned == record.conversations_started == planned.replay_slots
    assert [(arrival.planned_ms, arrival.observed_ms) for arrival in summary.arrivals] == [
        (START + at, START + at) for at in planned.arrivals_ms
    ]
    assert (summary.slots_planned, summary.slots_started) == (planned.replay_slots, planned.replay_slots)
    # An opening is traffic of its own, on a prefix that usage showed to be fresh for each student.
    assert (summary.segment, summary.fresh_students, summary.fresh_prefix) == (
        Segment.OPENING,
        (0, 1, 2),
        Fresh.VERIFIED,
    )
    assert ran.valid


def test_a_start_the_driver_missed_is_a_skipped_opening() -> None:
    cell = cell_of(SCREEN, Phase.CLASS_OPENING, Shape.HANDS_ON)
    window_ends = START + SPEC.opening_window_ms
    last = START + SCREEN.cells[cell].arrivals_ms[-1]
    # The driver does nothing from the last arrival until the window is over.
    ran, pool = run(SCREEN, cell, stall=(last, window_ends + 1))

    assert max(arrived.at_ms for arrived in pool.arrived) < last
    record = ran.record
    assert (record.conversations_planned, record.conversations_started, record.conversations_skipped) == (3, 2, 1)
    assert [arrival.observed_ms is None for arrival in ran.summary.arrivals] == [False, False, True]
    assert (ran.summary.slots_planned, ran.summary.slots_started) == (3, 2)
    assert record.status is CellStatus.COMPLETE
    assert not ran.valid


@pytest.mark.parametrize(("late_ms", "counts"), [(100, True), (300, False)])
def test_a_driver_that_dispatches_late_at_p95_is_flagged_as_saturated(late_ms: int, counts: bool) -> None:
    cell = cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4)
    # The answers are slow, and that is no lag of the driver's: a lag is from eligible to sent.
    ran, _pool = run(SCREEN, cell, late_ms=late_ms, answer_ms=lambda _student: 20_000)

    lags = [one.sent_ms - one.eligible_ms for one in ran.requests]
    assert set(lags) == {0, late_ms}
    assert ran.record.driver_lag_p95_ms == ran.summary.lag_p95_ms == late_ms == ran.summary.lag_max_ms
    assert ran.summary.lag_tolerance_ms == 250
    assert (InvalidFlag.DRIVER_SATURATION in ran.record.invalid_flags) is not counts
    assert ran.valid is counts


@pytest.mark.parametrize("stop", [Stop.CANCELLED, Stop.DEADLINE])
def test_a_cell_stopped_part_way_sends_nothing_more_and_is_incomplete(stop: Stop) -> None:
    cell = cell_of(SCREEN, Phase.CLASS_OPENING, Shape.SEVERAL_AGENTS)
    planned = SCREEN.cells[cell]
    # Half of its six conversations have started, and their first answers are still coming.
    at = START + (planned.arrivals_ms[2] + planned.arrivals_ms[3]) // 2
    cancel_at, deadline_ms = (at, None) if stop is Stop.CANCELLED else (None, at)
    ran, pool = run(SCREEN, cell, answer_ms=lambda _student: 90_000, cancel_at=cancel_at, deadline_ms=deadline_ms)

    assert len(pool.arrived) == 3
    assert not any(pool.open.values())
    record, summary = ran.record, ran.summary
    assert (summary.stopped_by, record.status, record.ended_ms) == (stop, CellStatus.INCOMPLETE, at)
    assert (record.conversations_planned, record.conversations_started, record.conversations_abandoned) == (6, 3, 3)
    assert (summary.slots_planned, summary.slots_started) == (6, 3)
    assert (summary.admissions_run_ms, summary.admissions_planned_ms) == (at - START, planned.admissions_ms)
    assert at - START < planned.admissions_ms
    # The three streams were cut then, and are recorded as far as they had come.
    assert summary.cut == (0, 1, 2)
    assert [(one.ended_ms, one.failed) for one in ran.requests] == [(at, True)] * 3
    assert (summary.tally.attempts, summary.tally.unfinished, summary.tally.completed) == (3, 0, 0)
    assert not ran.valid


def test_the_drain_ends_at_its_cutoff_and_cuts_the_streams_still_open() -> None:
    cell = cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4)
    planned = SCREEN.cells[cell]
    admissions_end = START + planned.admissions_ms
    cutoff = admissions_end + planned.drain_ms
    # One seat's answer never ends. Another's are slow, and one is open when the admissions end.
    ran, pool = run(SCREEN, cell, answer_ms=lambda student: {0: None, 1: 100_000}.get(student, ANSWER_MS))

    assert max(arrived.at_ms for arrived in pool.arrived) < admissions_end
    record = ran.record
    assert (record.admissions_ended_ms, record.ended_ms) == (admissions_end, cutoff)
    assert (planned.admissions_ms, planned.drain_ms) == (300_000, 120_000)
    # The stream that outlasted the drain was cut at the cutoff, and its conversation is abandoned.
    (hung,) = (one for one in ran.requests if one.student == 0)
    assert (hung.ended_ms, hung.outcome) == (cutoff, RequestOutcome.TRUNCATED_STREAM)
    assert ran.summary.cut == (hung.request,)
    assert not any(pool.open.values())
    # The one that ended inside the drain came whole.
    slow = [one for one in ran.requests if one.student == 1][-1]
    assert admissions_end < slow.ended_ms < cutoff
    assert slow.outcome is RequestOutcome.COMPLETED
    # Every start has an outcome, and a drain that cut is a cell that ran whole.
    assert record.conversations_outstanding == 0
    assert record.conversations_abandoned >= 1
    assert (ran.summary.stopped_by, record.status) == (Stop.ADMISSIONS_ENDED, CellStatus.COMPLETE)


def test_a_prefix_is_shared_in_its_cell_paired_across_treatments_and_apart_for_each_repetition() -> None:
    cell = cell_of(QUALIFY, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4)
    # Two treatments on one engine: each has a cohort of keys of its own, and is sent the same bytes.
    cohorts = replay.preflight(QUALIFY, KEYS, cohorts=2)
    assert not set(cohorts[0]) & set(cohorts[1])
    (reference, sent), (candidate, paired) = (run(QUALIFY, cell, keys=cohort) for cohort in cohorts)
    assert [arrived.content for arrived in sent.arrived] == [arrived.content for arrived in paired.arrived]
    assert reference.summary.prefix_hash == candidate.summary.prefix_hash

    # Every conversation of the cell starts with the same bytes.
    assert len({arrived.content for arrived in sent.arrived if arrived.turn == 0}) > 4
    (shared,) = {arrived.prefix for arrived in sent.arrived}
    # Another cell of the repetition starts with them too, and sends none of the same transcripts.
    _ran, same = run(QUALIFY, cell_of(QUALIFY, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 1))
    assert {arrived.prefix for arrived in same.arrived} == {shared}
    assert not {arrived.content for arrived in same.arrived} & {arrived.content for arrived in sent.arrived}

    # Another repetition, the warm-up and an opening each start with other bytes, which differ
    # before the content a class reuses: the opening and the tools are the same.
    apart = [
        cell_of(QUALIFY, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4, repetition=1),
        cell_of(QUALIFY, Phase.WARM_UP, Shape.HANDS_ON),
        cell_of(QUALIFY, Phase.CLASS_OPENING, Shape.HANDS_ON),
    ]
    prefixes = [shared]
    for other in apart:
        _ran, pool = run(QUALIFY, other, finished_at=START + 30_000)
        (prefix,) = {arrived.prefix for arrived in pool.arrived}
        prefixes.append(prefix)
    assert len(set(prefixes)) == 4
    systems = [json.loads(prefix)["system"] for prefix in prefixes]
    assert len({system[0]["text"] for system in systems}) == 4
    assert len({json.dumps(system[1:]) for system in systems}) == 1


def test_too_few_keys_are_refused_before_anything_is_sent() -> None:
    needed = SCREEN.canary_identities
    assert (replay.keys_needed(SCREEN), replay.keys_needed(SCREEN, cohorts=2)) == (needed, 2 * needed)
    # A key given twice is one key, an empty entry is none, and a second cohort needs as many again.
    few = KEYS[: needed - 1]
    short = [(few, 1), ([*few, KEYS[0]], 1), ([*few, ""], 1), ([*few, " "], 1), (KEYS[: 2 * needed - 1], 2)]
    for keys, cohorts in short:
        with pytest.raises(replay.ReplayError) as refused:
            replay.preflight(SCREEN, keys, cohorts=cohorts)
        assert refused.value.reason is Refusal.INSUFFICIENT_KEYS
    assert [len(set(cohort)) for cohort in replay.preflight(SCREEN, KEYS, cohorts=2)] == [needed, needed]

    # A cell is not driven with fewer keys than it has identities.
    cell = cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 16)
    idle = Pool(Clock(), KEYS)
    for keys in (KEYS[:15], [*KEYS[:15], ""]):
        with pytest.raises(replay.ReplayError) as refused:
            run(SCREEN, cell, keys=keys, pool=idle)
        assert refused.value.reason is Refusal.INSUFFICIENT_KEYS
    assert idle.arrived == []


# The second sweep cell of a class: one student replayed under its namespace in the cell before.
FOUR_SEATS = (SCREEN, cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4))
# The cell that opens the last repetition's namespace: two students, the two agents of each at once.
TWINS = (QUALIFY, cell_of(QUALIFY, Phase.CAPACITY_SWEEP, Shape.SEVERAL_AGENTS, 4, repetition=2))


@pytest.mark.parametrize(
    ("of", "new", "stale", "later", "fresh"),
    [
        # Each new student's first request read nothing from the cache. Its twin, sent with it, may.
        (TWINS, (0, 1), {}, 12, Fresh.VERIFIED),
        # Both first requests of one student read a few tokens: fewer than the opening holds,
        # and still not shown fresh.
        (TWINS, (0, 1), {1: 5}, 12, Fresh.UNVERIFIED),
        # Nothing read later, once an answer has ended, says what the student's first request met.
        (FOUR_SEATS, (1, 2, 3), {1: 5}, 0, Fresh.UNVERIFIED),
        # One student's first answer reported no usage.
        (FOUR_SEATS, (1, 2, 3), {3: None}, 12, Fresh.UNVERIFIED),
        # The student of the cell before may read what that cell left: the others show it fresh.
        (FOUR_SEATS, (1, 2, 3), {0: 5}, 12, Fresh.VERIFIED),
    ],
)
def test_a_cell_counts_only_when_usage_shows_its_prefix_fresh_for_each_student_new_to_it(
    of: tuple[plan.Plan, int], new: tuple[int, ...], stale: dict[int, int | None], later: int, fresh: Fresh
) -> None:
    planned, cell = of
    ran, pool = run(planned, cell, stale=stale, later=later)

    assert len(pool.arrived) > 4
    assert (ran.summary.fresh_students, ran.summary.fresh_prefix) == (new, fresh)
    assert (InvalidFlag.MISSING_EVIDENCE in ran.record.invalid_flags) is (fresh is Fresh.UNVERIFIED)
    assert ran.valid is (fresh is Fresh.VERIFIED)


def test_a_student_who_joins_a_repetition_after_its_first_cell_must_find_the_prefix_fresh_too() -> None:
    one_seat = cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 1)
    four_seats = FOUR_SEATS[1]
    assert SCREEN.cells[one_seat].prefix_namespace == SCREEN.cells[four_seats].prefix_namespace

    # The cells run one after the other on one engine. The first student's cache holds the prefix
    # by the second cell, and that is no fault: the three who join there find it fresh.
    pool = Pool(Clock(), KEYS)
    first, _pool = run(SCREEN, one_seat, pool=pool)
    second, _pool = run(SCREEN, four_seats, pool=pool)
    assert (first.summary.fresh_students, second.summary.fresh_students) == ((0,), (1, 2, 3))
    opening = [one for one in second.requests if one.sent_ms == second.record.started_ms]
    assert [one.usage.cached_input_tokens for one in opening if one.usage] == [pool.later, 0, 0, 0]
    assert (first.valid, second.summary.fresh_prefix, second.valid) == (True, Fresh.VERIFIED, True)

    # What the first student showed in the first cell shows nothing for another student, whose
    # cache has a salt of its own: one who joins and reads from the cache leaves the cell unverified.
    pool = Pool(Clock(), KEYS, stale={1: 20})
    first, _pool = run(SCREEN, one_seat, pool=pool)
    second, _pool = run(SCREEN, four_seats, pool=pool)
    assert (first.valid, second.summary.fresh_prefix, second.valid) == (True, Fresh.UNVERIFIED, False)
    assert InvalidFlag.MISSING_EVIDENCE in second.record.invalid_flags

    # Who must show it comes from the plan, and no caller says otherwise: each student of a
    # namespace in the first cell that has them replay under it, and in no other.
    for namespace in {one.prefix_namespace for one in SCREEN.cells}:
        cells = [one for one in SCREEN.cells if one.prefix_namespace == namespace]
        new = [student for one in cells for student in replay.new_students(SCREEN, one)]
        students = {replay.seat_of(SCREEN, one, slot) for one in cells for slot in range(one.replay_slots)}
        assert sorted(new) == sorted(students)


@pytest.mark.parametrize("stale", [{0: None}, {2: 7}])
def test_a_class_opening_never_counts_without_usage_that_shows_each_student_fresh(
    stale: dict[int, int | None],
) -> None:
    ran, _pool = run(SCREEN, cell_of(SCREEN, Phase.CLASS_OPENING, Shape.HANDS_ON), stale=stale)
    assert ran.summary.fresh_prefix is Fresh.UNVERIFIED
    assert ran.record.invalid_flags == (InvalidFlag.MISSING_EVIDENCE,)
    assert not ran.valid


@pytest.mark.parametrize(
    ("phase", "stop"),
    [(Phase.CAPACITY_SWEEP, Stop.CANCELLED), (Phase.CAPACITY_SWEEP, Stop.DEADLINE), (Phase.WARM_UP, Stop.FINISHED)],
)
def test_a_cell_that_is_stopped_before_it_starts_sends_nothing(phase: Phase, stop: Stop) -> None:
    # The signal is given, or the deadline has come, when the cell starts.
    at = {given: START if given is stop else None for given in Stop}
    ran, pool = run(
        SCREEN,
        cell_of(SCREEN, phase, Shape.HANDS_ON),
        cancel_at=at[Stop.CANCELLED],
        deadline_ms=at[Stop.DEADLINE],
        finished_at=at[Stop.FINISHED],
    )

    assert pool.arrived == []
    assert (ran.requests, ran.summary.stopped_by, ran.summary.slots_started) == ((), stop, 0)
    assert ran.record.conversations_started == 0 < ran.record.conversations_planned
    assert not ran.valid


def test_a_request_that_cannot_be_recorded_ends_the_cell_instead_of_leaving_it_out() -> None:
    cell = cell_of(SCREEN, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4)
    pool = Pool(Clock(), KEYS)
    # The ordinals of requests end at 999,999: of the four requests of the start, three have none.
    with pytest.raises(ContractError):
        run(SCREEN, cell, pool=pool, first_request=999_999)

    # The cell stopped when their answers ended: it sent nothing after them and left no stream open.
    assert [arrived.at_ms for arrived in pool.arrived] == [START] * 4
    assert pool.clock.at == START + ANSWER_MS
    assert not any(pool.open.values())


def test_the_requests_have_the_sizes_the_plan_froze_and_a_cell_sent_with_others_does_not_count() -> None:
    # The course suite's replay is spike S5's.
    assert replay.sizes_of(plan.expand(COURSE, Profile.SCREEN, 3)) == traces.S5

    # A plan that freezes another output is sent with that one: it is in the plan's digest.
    other = plan.expand(suite(turn_output_tokens=73), Profile.SCREEN, 3)
    assert other.workload.protocol_digest != SCREEN.workload.protocol_digest
    cell = cell_of(other, Phase.CAPACITY_SWEEP, Shape.HANDS_ON, 4)
    ran, pool = run(other, cell)
    assert {arrived.body["max_tokens"] for arrived in pool.arrived} == {73}
    assert (ran.summary.sizes_planned, ran.valid) == (True, True)

    # Sent with sizes the plan does not hold, as a rehearsal is, the cell is kept and does not count.
    ran, pool = run(SCREEN, cell, sizes=replay.sizes_of(other))
    assert {arrived.body["max_tokens"] for arrived in pool.arrived} == {73}
    assert (ran.summary.sizes_planned, ran.record.invalid_flags) == (False, (InvalidFlag.MISSING_EVIDENCE,))
    assert not ran.valid

    # The conversations are Messages requests: a suite that replays on another route is refused.
    idle = Pool(Clock(), KEYS)
    with pytest.raises(replay.ReplayError) as refused:
        run(plan.expand(suite(route="responses"), Profile.SCREEN, 3), cell, pool=idle)
    assert refused.value.reason is Refusal.UNSUPPORTED_ROUTE
    assert idle.arrived == []


def test_a_gateway_is_reached_over_https_only() -> None:
    # A key is not sent in the clear: no gateway is made for such an address.
    for address in ("http://gateway.example.com", "ftp://gateway.example.com", ""):
        with pytest.raises(ValueError, match="https"):
            replay.Gateway(address, "coursework", Clock())
    # The gateway stack's output is a host name.
    assert replay.Gateway("gateway.example.com", "coursework", Clock()).endpoint == "https://gateway.example.com"
