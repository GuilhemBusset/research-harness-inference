"""The session capacity of a run and the rows that limit it (docs/design/agent-session-evaluation.md,
"Reading a result"), on the qualification plan of the course suite for a class of eight: three
repetitions of each shape at the class size, eight hands-on slots and sixteen several-agents ones.

A class-size cell of the fixture runs from 100 s to 1,120 s of the run (fifteen minutes of
admissions and a two-minute drain), on hosts 1 and 2, each sampled every 15 s from 5 s before the
start to 10 s after the drain. Its requests are later turns that complete, answer in 1 s and
stream 21 tokens in 1 s (20 tokens a second), unless a test says otherwise. Every expected number
is worked out by hand in its test.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from evaluate import capacity, plan
from evaluate.capacity import Check, Floor, Reason, ShapeVerdict, Verdict
from evaluate.contracts import (
    METRIC_UNITS,
    CellRecord,
    CellStatus,
    HostMetric,
    HostSample,
    InvalidFlag,
    Phase,
    Profile,
    RequestOutcome,
    RequestRecord,
    Route,
    Shape,
    Traffic,
    Usage,
)
from evaluate.plan import Role
from evaluate.streams import Tally

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from evaluate.plan import PlannedCell

PLAN = plan.expand(plan.load_suite(), Profile.QUALIFY, 8)
OBJECTIVES = PLAN.workload.objectives
START, ADMITTED, END = 100_000, 1_000_000, 1_120_000
PRICE = 2.0


def floor_cell(shape: Shape = Shape.HANDS_ON, repetition: int = 0) -> PlannedCell:
    return next(
        cell
        for cell in PLAN.cells
        if Role.CLASS_FLOOR in cell.roles and (cell.identity.shape, cell.identity.repetition) == (shape, repetition)
    )


def request(
    cell: int,
    at: int,
    *,
    turn: int = 1,
    ttft_ms: int = 1000,
    tokens: int | None = 21,
    stream_ms: int = 1000,
    outcome: RequestOutcome = RequestOutcome.COMPLETED,
    lag_ms: int = 0,
    output: bool = True,
    traffic: Traffic = Traffic.BACKGROUND,
) -> RequestRecord:
    eligible = START + 1000 + at * 100
    sent = eligible + lag_ms
    first = sent + ttft_ms if output else None
    last = first + stream_ms if first is not None else None
    return RequestRecord(
        request=at,
        cell=cell,
        student=at % 8,
        conversation=at,
        turn=turn,
        traffic=traffic,
        trial=None,
        route=Route.MESSAGES,
        eligible_ms=eligible,
        sent_ms=sent,
        first_output_ms=first,
        first_answer_ms=first,
        last_output_ms=last,
        ended_ms=(last or sent) + 10,
        outcome=outcome,
        finish_reason=None,
        usage=None if tokens is None else Usage(input_tokens=1000, cached_input_tokens=250, output_tokens=tokens),
    )


def requests(cell: int, count: int, *, offset: int = 0, **given: object) -> list[RequestRecord]:
    return [request(cell, offset + at, **given) for at in range(count)]  # ty: ignore[invalid-argument-type]


def tally_of(rows: Sequence[RequestRecord]) -> Tally:
    """What the scheduler counted, for rows whose every output count closed its answer: a stream is
    eligible when it completed with two tokens or more over a time that is not zero."""
    causes = [cause for cause in RequestOutcome if cause is not RequestOutcome.COMPLETED]
    return Tally(
        attempts=len(rows),
        unfinished=0,
        completed=sum(not row.failed for row in rows),
        failures={cause: sum(row.outcome is cause for row in rows) for cause in causes},
        retries=0,
        usage_missing=sum(row.usage_missing for row in rows),
        eligible=sum(
            not row.failed
            and row.usage is not None
            and (row.usage.output_tokens or 0) >= 2
            and (row.last_output_ms or 0) > (row.first_output_ms or 0)
            for row in rows
        ),
    )


def rising(per_s: float, *, until: int = ADMITTED) -> Callable[[int], float]:
    """A counter that rises at `per_s` from the start of the cell until `until`, then holds."""
    return lambda at: per_s * (min(max(at, START), until) - START) / 1000


def samples(
    cell: int,
    hosts: Sequence[int] = (1, 2),
    given: Mapping[tuple[int, HostMetric], Callable[[int], float | None]] | None = None,
    missing: Sequence[tuple[int, HostMetric]] = (),
) -> list[HostSample]:
    series: dict[HostMetric, Callable[[int], float | None]] = {
        HostMetric.OUTPUT_TOKENS_TOTAL: rising(10),
        HostMetric.INPUT_TOKENS_TOTAL: rising(100),
        HostMetric.CACHED_INPUT_TOKENS_TOTAL: rising(25),
        HostMetric.COMPUTED_INPUT_TOKENS_TOTAL: rising(75),
        HostMetric.PREEMPTIONS_TOTAL: lambda _at: 0.0,
        HostMetric.RUNNING_REQUESTS: lambda _at: 4.0,
        HostMetric.WAITING_REQUESTS: lambda _at: 1.0,
        HostMetric.GPU_UTILIZATION: lambda _at: 0.5,
    }
    found = []
    for host in hosts:
        for metric, default in series.items():
            if (host, metric) in missing:
                continue
            value = (given or {}).get((host, metric), default)
            for at in range(START - 5000, END + 15_000, 15_000):
                found.append(
                    HostSample(
                        cell=cell, host=host, at_ms=at, metric=metric, unit=METRIC_UNITS[metric], value=value(at)
                    )
                )
    return found


def evidence(
    planned: PlannedCell,
    rows: Sequence[RequestRecord] | None = None,
    *,
    hosts: Sequence[int] = (1, 2),
    given: Mapping[tuple[int, HostMetric], Callable[[int], float | None]] | None = None,
    missing: Sequence[tuple[int, HostMetric]] = (),
    tally: Tally | None = None,
    in_service: Mapping[int, float] | None = None,
    **record: object,
) -> capacity.CellEvidence:
    cell = planned.identity.cell
    rows = requests(cell, 100) if rows is None else list(rows)
    fields: dict[str, object] = {
        "cell": cell,
        "started_ms": START,
        "admissions_ended_ms": ADMITTED,
        "ended_ms": END,
        "status": CellStatus.COMPLETE,
        "invalid_flags": (),
        "conversations_planned": 20,
        "conversations_started": 20,
        "conversations_completed": 18,
        "conversations_abandoned": 2,
        "hosts_observed": len(hosts),
        "driver_lag_p95_ms": None,
    } | record
    return capacity.CellEvidence(
        record=CellRecord(**fields),  # ty: ignore[invalid-argument-type]
        requests=tuple(rows),
        samples=tuple(samples(cell, hosts, given, missing)),
        tally=tally_of(rows) if tally is None else tally,
        in_service_seconds={host: (END - START) / 1000 for host in hosts} if in_service is None else in_service,
    )


def read_one(one: capacity.CellEvidence) -> capacity.CellReading:
    reading = capacity.read(PLAN, [one], usd_per_host_hour=PRICE)
    return next(cell for cell in reading.cells if cell.identity.cell == one.record.cell)


# --- Which cells, and which floors -------------------------------------------------------------


def test_the_class_size_cell_is_the_class_floor_and_not_the_largest_sweep_cell():
    reading = capacity.read(PLAN, [], usd_per_host_hour=PRICE)
    # The sweep runs sixteen hands-on slots too; a class of eight is judged at eight.
    assert reading.shapes[Shape.HANDS_ON].agent_slots == 8
    assert reading.shapes[Shape.SEVERAL_AGENTS].agent_slots == 16
    assert [cell.identity.repetition for cell in reading.shapes[Shape.HANDS_ON].repetitions] == [0, 1, 2]
    # A planned repetition with no evidence is inconclusive, never a pass.
    assert {cell.verdict for cell in reading.cells} == {Verdict.INSUFFICIENT_EVIDENCE}
    assert reading.shapes[Shape.HANDS_ON].verdict is ShapeVerdict.INCONCLUSIVE


def test_a_clean_repetition_passes_and_reports_its_rows():
    at = floor_cell().identity.cell
    cell = read_one(evidence(floor_cell(), requests(at, 100) + requests(at, 8, offset=100, turn=0, ttft_ms=3000)))
    assert cell.verdict is Verdict.PASS and set(cell.floors.values()) == {Check.MET}
    assert (cell.later_turns.count, cell.later_turns.p95_ms) == (100, 1000)
    # First turns are read apart and are not held to the floor.
    assert (cell.first_turns.count, cell.first_turns.p95_ms) == (8, 3000)
    assert (cell.speeds.count, cell.speeds.p50_tokens_per_s, cell.speeds.p10_tokens_per_s) == (108, 20, 20)
    failures = cell.failures
    assert (failures.failed, failures.attempts, failures.observed_floor) == (0, 108, True)
    low, high = failures.wilson_low, failures.wilson_high
    assert low == 0 and high is not None and 0 < high < 0.05


@pytest.mark.parametrize(
    ("floor", "rows", "given", "check"),
    [
        # Later-turn p95 must be under the limit: at the limit is a miss.
        (Floor.TTFT_P95, {"ttft_ms": int(OBJECTIVES.ttft_p95_ms_max)}, {}, Check.MISSED),
        (Floor.TTFT_P95, {"ttft_ms": int(OBJECTIVES.ttft_p95_ms_max) - 1}, {}, Check.MET),
        # The median speed floor is met at the floor: 16 tokens in 1 s is 15 a second.
        (Floor.SPEED_P50, {"tokens": 16}, {}, Check.MET),
        (Floor.SPEED_P50, {"tokens": 16, "stream_ms": 1001}, {}, Check.MISSED),
        # Preemptions: none passes, one on one host misses.
        (Floor.PREEMPTIONS, {}, {}, Check.MET),
        (Floor.PREEMPTIONS, {}, {(2, HostMetric.PREEMPTIONS_TOTAL): lambda at: float(at > ADMITTED)}, Check.MISSED),
    ],
)
def test_threshold_equality(floor, rows, given, check):
    planned = floor_cell()
    assert OBJECTIVES.stream_speed_p50_tokens_per_s_min == 15
    cell = read_one(evidence(planned, requests(planned.identity.cell, 100, **rows), given=given))
    assert cell.floors[floor] is check
    assert cell.verdict is (Verdict.PASS if check is Check.MET else Verdict.MEASURED_MISS)


@pytest.mark.parametrize(("stream_ms", "check"), [(1000, Check.MET), (1001, Check.MISSED)])
def test_threshold_equality_of_the_slowest_tenth(stream_ms, check):
    planned = floor_cell()
    assert OBJECTIVES.stream_speed_p10_tokens_per_s_min == 10
    # Eleven streams of 11 tokens in `stream_ms` and 89 at 20 a second: the 10th percentile falls
    # between the 10th and 11th slowest, both of the eleven.
    slow = requests(planned.identity.cell, 11, tokens=11, stream_ms=stream_ms)
    cell = read_one(evidence(planned, slow + requests(planned.identity.cell, 89, offset=11)))
    assert cell.floors[Floor.SPEED_P10] is check and cell.floors[Floor.SPEED_P50] is Check.MET


@pytest.mark.parametrize(("failed", "check"), [(1, Check.MET), (2, Check.MISSED)])
def test_threshold_equality_of_the_failed_share(failed, check):
    planned = floor_cell()
    assert OBJECTIVES.failed_share_max == 0.005
    # One failure in 200 attempts is exactly the cap.
    rows = requests(planned.identity.cell, 200 - failed) + requests(
        planned.identity.cell, failed, offset=200 - failed, outcome=RequestOutcome.TRANSPORT_ERROR, output=False
    )
    cell = read_one(evidence(planned, rows))
    assert (cell.failures.failed, cell.failures.attempts) == (failed, 200)
    assert cell.floors[Floor.FAILED_SHARE] is check


# --- Failures and accounting ---------------------------------------------------------------------


def test_midstream_and_connection_failures_are_failed_attempts_and_no_sample():
    planned = floor_cell()
    at = planned.identity.cell
    rows = [
        *requests(at, 100),
        # Midstream: a stream cut, and an error event inside a 200, both after some output.
        request(at, 100, outcome=RequestOutcome.TRUNCATED_STREAM, tokens=None),
        request(at, 101, outcome=RequestOutcome.ERROR_EVENT),
        # On connection: the transport failed, or the gateway refused.
        request(at, 102, outcome=RequestOutcome.TRANSPORT_ERROR, output=False, tokens=None),
        request(at, 103, outcome=RequestOutcome.HTTP_REFUSAL, output=False, tokens=None),
    ]
    cell = read_one(evidence(planned, rows))
    failures = cell.failures
    assert (failures.failed, failures.attempts) == (4, 104)
    assert all(failures.by_cause[cause] == 1 for cause in failures.by_cause)
    assert failures.usage_missing == 3
    low, high = failures.wilson_low, failures.wilson_high
    assert low is not None and high is not None and low < 4 / 104 < high
    # The two midstream failures had a first output: it is a time to first output, never a speed.
    assert (cell.speeds.count, cell.later_turns.count) == (100, 102)
    assert cell.floors[Floor.FAILED_SHARE] is Check.MISSED and cell.verdict is Verdict.MEASURED_MISS
    # What reached the client counts as delivered, a failed stream's count included.
    assert cell.engine is not None and cell.engine.delivered_output_tokens == 101 * 21
    assert cell.engine.delivered_usage_missing == 1


def test_an_opening_usage_with_no_output_is_not_delivered():
    planned = floor_cell()
    at = planned.identity.cell
    # The stream reported one output token in its opening event, then was cut before any output.
    cut = request(at, 99, outcome=RequestOutcome.TRUNCATED_STREAM, output=False, tokens=1)
    cell = read_one(evidence(planned, [*requests(at, 99), cut]))
    assert cell.failures.failed == 1
    assert cell.engine is not None and cell.engine.delivered_output_tokens == 99 * 21


def test_a_slow_first_output_counts_whatever_the_request_came_to():
    planned = floor_cell()
    at = planned.identity.cell
    rows = [
        *requests(at, 190, ttft_ms=4900),
        *requests(at, 9, offset=190, ttft_ms=9000),
        request(at, 199, ttft_ms=9000, outcome=RequestOutcome.ERROR_EVENT),
    ]
    cell = read_one(evidence(planned, rows))
    # Ranks 189 and 190 of 200 sorted times hold the p95 (rank 189.05): 4,900 + 0.05 * 4,100.
    assert cell.later_turns.count == 200 and cell.later_turns.p95_ms == pytest.approx(5105)
    assert cell.floors[Floor.TTFT_P95] is Check.MISSED and cell.speeds.count == 199


def test_one_failure_and_no_sample_is_insufficient_and_still_shown():
    planned = floor_cell()
    row = request(planned.identity.cell, 0, outcome=RequestOutcome.TRANSPORT_ERROR, output=False, tokens=None)
    cell = read_one(evidence(planned, [row]))
    assert (cell.failures.failed, cell.failures.attempts) == (1, 1)
    assert cell.verdict is Verdict.INSUFFICIENT_EVIDENCE and cell.floors[Floor.FAILED_SHARE] is Check.INSUFFICIENT
    assert cell.observed_misses == (Floor.FAILED_SHARE,)


def test_a_send_with_no_row_or_no_outcome_leaves_the_accounting_incomplete():
    planned = floor_cell()
    rows = requests(planned.identity.cell, 100)
    sent = tally_of(rows)
    lost = replace(sent, attempts=101, failures={**sent.failures, RequestOutcome.TRANSPORT_ERROR: 1})
    unfinished = replace(sent, attempts=101, unfinished=1)
    for tally in (lost, unfinished):
        cell = read_one(evidence(planned, rows, tally=tally))
        assert cell.verdict is Verdict.INVALID and Reason.ATTEMPTS_UNACCOUNTED in cell.reasons
    nothing = replace(evidence(planned, rows), tally=None)
    assert Reason.ATTEMPTS_UNACCOUNTED in read_one(nothing).reasons
    # The same row twice stands for one send: the hundredth has no row of its own.
    twice = read_one(evidence(planned, [*rows[:-1], rows[0]], tally=sent))
    assert twice.verdict is Verdict.INVALID and twice.reasons == (Reason.ATTEMPTS_UNACCOUNTED,)


def test_the_eligible_streams_are_the_ones_the_scheduler_counted():
    planned = floor_cell()
    rows = requests(planned.identity.cell, 100)
    # The rows give each a speed; the scheduler saw one output count that did not close its answer.
    cell = read_one(evidence(planned, rows, tally=replace(tally_of(rows), eligible=99)))
    assert cell.verdict is Verdict.INVALID and cell.reasons == (Reason.ELIGIBILITY_UNRECONCILED,)


def test_a_request_that_ends_after_the_drain_has_no_outcome_in_the_window():
    planned = floor_cell()
    rows = requests(planned.identity.cell, 99)
    # Sent in the window, its output all after the drain.
    late = replace(
        request(planned.identity.cell, 99),
        first_output_ms=END + 1,
        first_answer_ms=END + 1,
        last_output_ms=END + 1001,
        ended_ms=END + 1010,
    )
    cell = read_one(evidence(planned, [*rows, late]))
    assert cell.failures.attempts == 100
    assert cell.verdict is Verdict.INVALID and cell.reasons == (Reason.OUTCOME_AFTER_DRAIN,)


# --- Evidence of the hosts --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "missing", "hosts_observed", "reason", "in_service"),
    [
        ({}, [(2, HostMetric.PREEMPTIONS_TOTAL)], 2, Reason.PREEMPTIONS_UNCOVERED, None),
        (
            {(2, HostMetric.PREEMPTIONS_TOTAL): lambda at: None if 500_000 < at < 520_000 else 0.0},
            [],
            2,
            Reason.PREEMPTIONS_UNCOVERED,
            None,
        ),
        ({}, [], 3, Reason.HOSTS_UNRECONCILED, None),
        # Hosts 1 and 3 served; the samples are of hosts 1 and 2. Two hosts each, and host 3 has no series.
        ({}, [], 2, Reason.HOSTS_UNRECONCILED, {1: 1020.0, 3: 1020.0}),
        ({}, [], 2, Reason.PREEMPTIONS_UNCOVERED, {1: 1020.0, 3: 1020.0}),
    ],
)
def test_a_missing_host_series_invalidates_the_cell(given, missing, hosts_observed, reason, in_service):
    planned = floor_cell()
    found = evidence(planned, given=given, missing=missing, hosts_observed=hosts_observed, in_service=in_service)
    cell = read_one(found)
    assert cell.verdict is Verdict.INVALID and reason in cell.reasons


def test_a_preemption_counter_that_restarts_counts_its_new_value():
    planned = floor_cell()
    # 9 before the cell, then a restart that has counted 2 since.
    restarted = {(1, HostMetric.PREEMPTIONS_TOTAL): lambda at: 9.0 if at < 500_000 else 2.0}
    cell = read_one(evidence(planned, given=restarted))
    assert cell.engine is not None and cell.engine.preemptions == 2
    assert cell.floors[Floor.PREEMPTIONS] is Check.MISSED


# --- The driver -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("lag_ms", "verdict"), [(250, Verdict.PASS), (251, Verdict.INVALID)])
def test_driver_lag_beyond_the_tolerance_invalidates_the_cell(lag_ms, verdict):
    planned = floor_cell()
    assert PLAN.spec.protocol["driver"]["scheduling_lag_p95_ms_max"] == 250
    cell = read_one(evidence(planned, requests(planned.identity.cell, 100, lag_ms=lag_ms)))
    assert cell.driver_lag_p95_ms == lag_ms and cell.verdict is verdict


def test_warm_up_traffic_does_not_dilute_the_driver_lag():
    planned = floor_cell()
    at = planned.identity.cell
    steady = requests(at, 94) + requests(at, 6, offset=94, lag_ms=1000)
    warm = requests(at, 30, offset=100, traffic=Traffic.WARM_UP)
    cell = read_one(evidence(planned, steady + warm))
    assert cell.driver_lag_p95_ms == 1000
    assert cell.verdict is Verdict.INVALID and cell.reasons == (Reason.DRIVER_LAG,)
    # A lagging warm-up is held to the tolerance as well.
    lagging = read_one(
        evidence(planned, requests(at, 100) + requests(at, 30, offset=100, lag_ms=1000, traffic=Traffic.WARM_UP))
    )
    assert lagging.verdict is Verdict.INVALID and lagging.reasons == (Reason.DRIVER_LAG,)


def test_a_saturated_driver_and_a_skipped_opening_invalidate_the_cell():
    planned = floor_cell()
    saturated = read_one(evidence(planned, invalid_flags=(InvalidFlag.DRIVER_SATURATION,)))
    skipped = read_one(evidence(planned, conversations_started=19, conversations_completed=17))
    assert saturated.verdict is Verdict.INVALID and Reason.DRIVER_SATURATION in saturated.reasons
    assert skipped.verdict is Verdict.INVALID and skipped.reasons == (Reason.SKIPPED_OPENING,)


# --- How many samples -----------------------------------------------------------------------------


@pytest.mark.parametrize(("budget", "needed"), [(50, 100), (100, 100), (150, 150)])
def test_exactly_one_short_of_the_minimum_is_insufficient(budget, needed):
    sampling = replace(PLAN.workload.sampling, ttft_samples_min=budget, stream_samples_min=budget)
    planned_plan = replace(PLAN, workload=replace(PLAN.workload, sampling=sampling))
    planned = floor_cell()
    at = planned.identity.cell

    def verdict(rows: list[RequestRecord]) -> tuple[Verdict, dict[Floor, Check]]:
        found = capacity.read(planned_plan, [evidence(planned, rows)], usd_per_host_hour=PRICE)
        cell = next(cell for cell in found.cells if cell.identity.cell == at)
        return cell.verdict, dict(cell.floors)

    assert verdict(requests(at, needed))[0] is Verdict.PASS
    later, floors = verdict(requests(at, needed - 1) + requests(at, 5, offset=needed, turn=0))
    assert later is Verdict.INSUFFICIENT_EVIDENCE and floors[Floor.TTFT_P95] is Check.INSUFFICIENT
    # A later turn of one output token has no speed: as many later turns, one stream short.
    streams, floors = verdict(requests(at, needed - 1) + [request(at, needed, tokens=1)])
    assert streams is Verdict.INSUFFICIENT_EVIDENCE and floors[Floor.TTFT_P95] is Check.MET
    assert floors[Floor.SPEED_P50] is floors[Floor.SPEED_P10] is Check.INSUFFICIENT


def test_a_request_sent_in_the_drain_is_accounted_for_and_gives_no_sample():
    planned = floor_cell()
    at = planned.identity.cell
    # 99 later turns and 99 streams sent before admissions close, beside an opening that does not
    # stream; then, 10 s into the drain (request 9,090 is eligible at 1,010 s), either a new
    # conversation opens or that opened conversation sends a later turn.
    opening = request(at, 98, turn=0)
    rows = [*requests(at, 98), opening, request(at, 99, tokens=1)]
    for outcome, failed in ((RequestOutcome.COMPLETED, 0), (RequestOutcome.ERROR_EVENT, 1)):
        new = request(at, 9090, turn=0, outcome=outcome)
        later = replace(request(at, 9090, outcome=outcome), student=opening.student, conversation=opening.conversation)
        for late in (new, later):
            assert late.sent_ms == ADMITTED + 10_000
            cell = read_one(evidence(planned, [*rows, late]))
            assert (cell.failures.attempts, cell.failures.failed, cell.sent_in_drain_requests) == (101, failed, 1)
            assert (cell.later_turns.count, cell.speeds.count, cell.first_turns.count) == (99, 99, 1)
            assert cell.verdict is Verdict.INSUFFICIENT_EVIDENCE


# --- Repetitions -------------------------------------------------------------------------------------


def test_a_known_miss_is_kept_beside_an_incomplete_result():
    at = [floor_cell(repetition=repetition) for repetition in range(3)]
    # Repetition 0 misses its later-turn p95 on 100 later turns, of which only 99 have a speed.
    slow = requests(at[0].identity.cell, 99, ttft_ms=6000) + [request(at[0].identity.cell, 99, ttft_ms=6000, tokens=1)]
    found = capacity.read(
        PLAN,
        [evidence(at[0], slow), evidence(at[1], requests(at[1].identity.cell, 100), status=CellStatus.INCOMPLETE)],
        usd_per_host_hour=PRICE,
    )
    shape = found.shapes[Shape.HANDS_ON]
    assert [cell.verdict for cell in shape.repetitions] == [
        Verdict.MEASURED_MISS,
        Verdict.INVALID,
        Verdict.INSUFFICIENT_EVIDENCE,
    ]
    missed = shape.repetitions[0]
    assert missed.floors[Floor.TTFT_P95] is Check.MISSED and missed.floors[Floor.SPEED_P10] is Check.INSUFFICIENT
    assert shape.verdict is ShapeVerdict.KNOWN_MISS and shape.missed_repetitions == (0,)


def test_a_failing_repeat_shows_even_when_pooled_repeats_would_pass():
    cells = [floor_cell(repetition=repetition) for repetition in range(3)]
    found = []
    for planned in cells:
        failed = 2 if planned.identity.repetition == 1 else 0
        rows = requests(planned.identity.cell, 200 - failed) + requests(
            planned.identity.cell, failed, offset=200 - failed, outcome=RequestOutcome.HTTP_REFUSAL, output=False
        )
        found.append(evidence(planned, rows))
    # Pooled, 2 failures in 600 attempts is under the cap of 0.5%.
    assert 2 / 600 <= OBJECTIVES.failed_share_max
    shape = capacity.read(PLAN, found, usd_per_host_hour=PRICE).shapes[Shape.HANDS_ON]
    assert [cell.verdict for cell in shape.repetitions] == [Verdict.PASS, Verdict.MEASURED_MISS, Verdict.PASS]
    assert shape.verdict is ShapeVerdict.KNOWN_MISS and shape.missed_repetitions == (1,)
    assert (shape.failed_share.low, shape.failed_share.high) == (0, 0.01)


def test_every_repeat_passing_passes_the_shape():
    found = [evidence(floor_cell(repetition=repetition)) for repetition in range(3)]
    assert capacity.read(PLAN, found, usd_per_host_hour=PRICE).shapes[Shape.HANDS_ON].verdict is ShapeVerdict.PASS


# --- Throughput and cost -----------------------------------------------------------------------------


def test_a_multihost_example_with_different_service_times_is_recomputed_through_drain():
    planned = floor_cell()
    seconds = (END - START) / 1000  # 1,020 s, the drain included
    # Host 1 serves its share by the end of admissions; host 2, slower, is still streaming through
    # the drain. Host 2 was in service for half the window.
    given = {
        (1, HostMetric.OUTPUT_TOKENS_TOTAL): rising(50, until=ADMITTED),
        (2, HostMetric.OUTPUT_TOKENS_TOTAL): rising(40, until=END),
        (2, HostMetric.WAITING_REQUESTS): lambda _at: 3.0,
        (2, HostMetric.GPU_UTILIZATION): lambda _at: 0.9,
    }
    engine = read_one(evidence(planned, given=given, in_service={1: seconds, 2: seconds / 2})).engine
    assert engine is not None and engine.window_seconds == seconds
    by_host = {host.host: host for host in engine.hosts}
    assert by_host[1].counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 50 * 900
    assert by_host[2].counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 40 * seconds
    assert by_host[1].output_tokens_per_s == pytest.approx(50 * 900 / seconds)
    assert by_host[2].output_tokens_per_s == pytest.approx(40)
    assert engine.output_tokens == 45_000 + 40_800
    assert engine.output_tokens_per_s == pytest.approx((45_000 + 40_800) / seconds)
    # Delivered: 100 completed answers of 21 tokens each, whatever the engines counted.
    assert engine.delivered_output_tokens == 2100
    assert engine.cached_share == pytest.approx(0.25)
    assert engine.computed_input_tokens == 2 * 75 * 900
    # One request waits on host 1 and three on host 2 at every scrape: four wait at once in the pool.
    assert (engine.waiting_requests_mean, engine.waiting_requests_peak) == (4.0, 4.0)
    assert [host.gauges[HostMetric.WAITING_REQUESTS].peak for host in engine.hosts] == [1.0, 3.0]
    assert engine.gpu_utilization_mean == pytest.approx(0.7)
    # The hosts cost what they were in service for: 1.5 windows of host time.
    assert engine.host_hours == pytest.approx(1.5 * seconds / 3600)
    assert engine.host_cost_estimate_per_million_output_usd == pytest.approx(
        1.5 * seconds / 3600 * PRICE / ((45_000 + 40_800) / 1e6)
    )


def test_the_pool_queue_is_unknown_without_overlapping_host_observations():
    planned = floor_cell()
    waiting = HostMetric.WAITING_REQUESTS
    # Host 1 is observed early and then scrapes give nothing; host 2 only late.
    given = {
        (1, waiting): lambda at: 8.0 if at < 400_000 else None,
        (2, waiting): lambda at: 9.0 if at > 800_000 else None,
    }
    engine = read_one(evidence(planned, given=given)).engine
    assert engine is not None and (engine.waiting_requests_mean, engine.waiting_requests_peak) == (None, None)
    assert [host.gauges[waiting].peak for host in engine.hosts] == [8.0, 9.0]


def test_cost_is_undefined_when_nothing_was_generated():
    planned = floor_cell()
    flat = {(host, HostMetric.OUTPUT_TOKENS_TOTAL): lambda _at: 7.0 for host in (1, 2)}
    engine = read_one(evidence(planned, given=flat)).engine
    assert engine is not None and engine.output_tokens == 0
    assert engine.host_cost_estimate_usd == pytest.approx(2 * (END - START) / 1000 / 3600 * PRICE)
    assert engine.host_cost_estimate_per_million_output_usd is None


# --- Outside steady state -------------------------------------------------------------------------------


def test_opening_and_warm_up_are_reported_outside_steady_state():
    steady = [evidence(floor_cell(repetition=repetition)) for repetition in range(3)]
    warm_up = next(cell for cell in PLAN.cells if cell.identity.phase is Phase.WARM_UP)
    opening = next(
        cell
        for cell in PLAN.cells
        if cell.identity.phase is Phase.CLASS_OPENING and cell.identity.shape is Shape.HANDS_ON
    )
    # Slow and failing first turns: they would miss every floor if they were steady state.
    outside = [
        evidence(
            planned,
            requests(planned.identity.cell, 8, turn=0, ttft_ms=9000, traffic=traffic)
            + requests(
                planned.identity.cell,
                8,
                offset=8,
                outcome=RequestOutcome.TRANSPORT_ERROR,
                output=False,
                traffic=traffic,
            ),
        )
        for planned, traffic in ((warm_up, Traffic.WARM_UP), (opening, Traffic.BACKGROUND))
    ]
    alone = capacity.read(PLAN, steady, usd_per_host_hour=PRICE)
    together = capacity.read(PLAN, steady + outside, usd_per_host_hour=PRICE)
    assert together.shapes == alone.shapes
    assert together.shapes[Shape.HANDS_ON].verdict is ShapeVerdict.PASS
    reported = {cell.identity.cell: cell for cell in together.outside_steady_state}
    assert set(reported) == {warm_up.identity.cell, opening.identity.cell}
    assert all(cell.first_turns.p95_ms == 9000 and cell.failures.failed == 8 for cell in reported.values())


def test_warm_up_traffic_in_a_steady_cell_is_accounted_for_and_not_read():
    planned = floor_cell()
    at = planned.identity.cell
    warm = [
        request(at, 99, traffic=Traffic.WARM_UP),
        request(at, 100, outcome=RequestOutcome.TRANSPORT_ERROR, output=False, traffic=Traffic.WARM_UP),
    ]
    cell = read_one(evidence(planned, requests(at, 99) + warm))
    assert cell.warm_up_requests == 2 and not cell.reasons
    assert (cell.later_turns.count, cell.speeds.count, cell.failures.attempts, cell.failures.failed) == (99, 99, 99, 0)
    assert cell.verdict is Verdict.INSUFFICIENT_EVIDENCE
