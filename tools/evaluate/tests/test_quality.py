"""The quality of a run and the paired decision of a candidate against a reference
(docs/design/agent-session-evaluation.md, "Reading a result"), on the qualification plan of the
course suite: optimization on five harnesses, debugging on two, analysis and research on one,
three instances and three repeats each, at low load and at class load.

Every expected number is worked out by hand in its test. A live cell of the fixture lasts thirty
minutes on two hosts, and a trial one minute unless a test says otherwise.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest

from evaluate import plan, quality
from evaluate.contracts import (
    LIVE_PHASES,
    CellRecord,
    CellStatus,
    FailureReason,
    Family,
    Grade,
    Harness,
    InvalidFlag,
    InvalidReason,
    Profile,
    Shape,
    TrialOutcome,
    TrialRecord,
    Workload,
)
from evaluate.quality import Load, Outcome, QualityError, Reason, Records

if TYPE_CHECKING:
    from collections.abc import Collection

SUITE = plan.load_suite()
# Eight seats: 81 trials at each load, in twelve live cells, one for a family and a repeat.
PLAN = plan.expand(SUITE, Profile.QUALIFY, 8)
CLASS = PLAN.workload
# A host price for the fixtures, in dollars for one host-hour.
PRICE = 2.0
SCOPES = (*Shape, None)
PASSED, FAILED, INVALID = TrialOutcome.PASSED, TrialOutcome.FAILED, TrialOutcome.INVALID


def ran(
    trial: int, outcome: TrialOutcome = PASSED, *, execution: int | None = None, replaces: int | None = None
) -> TrialRecord:
    """One execution of a planned trial. Its ordinal is the trial's unless another is given."""
    return TrialRecord(
        execution=trial if execution is None else execution,
        trial=trial,
        replaces=replaces,
        student=0,
        started_ms=0,
        ended_ms=60_000,
        outcome=outcome,
        failure=FailureReason.TIMEOUT if outcome is FAILED else None,
        invalid=InvalidReason.GRADER_ERROR if outcome is INVALID else None,
        grade=Grade(properties=(True,), within_limits=True) if outcome is PASSED else None,
        model_turns=None,
        retries=None,
        usage=None,
    )


def window(cell: int, *flags: InvalidFlag, hosts: int | None = 2) -> CellRecord:
    """The record of a live cell: thirty minutes, on two hosts unless told otherwise."""
    return CellRecord(
        cell=cell,
        started_ms=0,
        admissions_ended_ms=1_800_000,
        ended_ms=1_800_000,
        status=CellStatus.COMPLETE,
        invalid_flags=flags,
        conversations_planned=0,
        conversations_started=0,
        conversations_completed=0,
        conversations_abandoned=0,
        hosts_observed=hosts,
        driver_lag_p95_ms=None,
    )


def chosen(
    load: Load,
    family: Family,
    harness: Harness | None = None,
    *,
    instance: int | None = None,
    attempt: int | None = None,
    workload: Workload = CLASS,
) -> set[int]:
    """The planned trials of a family at a load, on one harness, instance or repeat when given."""
    return {
        trial.trial
        for trial in workload.trials
        if load in quality.loads_of(workload, trial.cell)
        and trial.instance_id.startswith(family)
        and harness in (None, trial.harness)
        and (instance is None or trial.instance_id.endswith(f".i0{instance}"))
        and attempt in (None, trial.attempt)
    }


def records(
    failed: Collection[int] = (),
    *,
    invalid: Collection[int] = (),
    absent: Collection[int] = (),
    also: tuple[TrialRecord, ...] = (),
    workload: Workload = CLASS,
) -> Records:
    """A run of the plan in which every trial passes once and every live cell is valid, except the
    trials that failed, were invalid, or left no record. `also` adds executions."""
    outcomes = dict.fromkeys(failed, FAILED) | dict.fromkeys(invalid, INVALID)
    trials = [ran(trial.trial, outcomes.get(trial.trial, PASSED)) for trial in workload.trials]
    return Records(
        workload=workload,
        trials=tuple(record for record in trials if record.trial not in absent) + also,
        cells=tuple(window(cell.cell) for cell in workload.cells if cell.phase in LIVE_PHASES),
    )


def read(run: Records) -> dict[Load, quality.LoadReading]:
    return quality.read(run, usd_per_host_hour=PRICE)


def other(load: Load) -> Load:
    return Load.CLASS if load is Load.LOW else Load.LOW


def interval(decision: quality.Decision) -> tuple[float, float, float]:
    """The lower bound, the observed difference and the upper bound of a decision that has them."""
    lower, observed, upper = decision.lower_points, decision.difference_points, decision.upper_points
    assert lower is not None
    assert observed is not None
    assert upper is not None
    return lower, observed, upper


# --- Reading a run ----------------------------------------------------------------------------


def test_a_family_weighs_its_harnesses_equally_whatever_their_number() -> None:
    """Debugging fails on one of its two harnesses at low load. Optimization passes on its five."""
    reading = read(records(chosen(Load.LOW, Family.DEBUGGING, Harness.CODEX)))
    low = reading[Load.LOW]

    assert len(low.cells) == 5 + 2 + 1 + 1
    lost, kept = low.cell(Family.DEBUGGING, Harness.CODEX), low.cell(Family.DEBUGGING, Harness.CLAUDE_CODE)
    assert (lost.successes, lost.attempts, lost.instances_all_passed, lost.floor_outcome) == (0, 9, 0, Outcome.FAIL)
    assert (kept.successes, kept.attempts, kept.instances_all_passed, kept.floor_outcome) == (9, 9, 3, Outcome.PASS)
    # Debugging is (0 + 1) / 2 and optimization 1, so hands-on is 3/4: not the share of its
    # attempts, (45 + 9) / (45 + 18), which five optimization harnesses would carry.
    assert low.family(Family.DEBUGGING).rate == 0.5
    assert low.family(Family.OPTIMIZATION).rate == 1.0
    assert low.shapes == {Shape.HANDS_ON: 0.75, Shape.SEVERAL_AGENTS: 1.0}
    assert low.combined == 0.875
    hands_on = [cell for cell in low.cells if cell.family in quality.SHAPE_FAMILIES[Shape.HANDS_ON]]
    assert Fraction(sum(cell.successes for cell in hands_on), sum(cell.attempts for cell in hands_on)) == Fraction(6, 7)
    # The other load is read on its own trials.
    assert reading[Load.CLASS].shapes == {Shape.HANDS_ON: 1.0, Shape.SEVERAL_AGENTS: 1.0}
    assert all(cell.floor_outcome is Outcome.PASS for cell in reading[Load.CLASS].cells)


def test_a_failing_research_family_stays_visible_beside_rates_above_the_floor() -> None:
    """One research instance fails its three repeats at class load: 6 of 9, under the 80% floor."""
    reading = read(records(chosen(Load.CLASS, Family.RESEARCH, instance=2)))
    loaded = reading[Load.CLASS]
    research = loaded.cell(Family.RESEARCH, Harness.CLAUDE_CODE)

    assert (research.successes, research.attempts, research.floor) == (6, 9, 0.8)
    assert research.success_share == 6 / 9
    assert (research.instances_all_passed, research.instances) == (2, 3)
    assert research.floor_outcome is Outcome.FAIL
    # Every other cell meets the floor, at this load and at the other.
    assert [cell.floor_outcome for cell in loaded.cells if cell is not research] == [Outcome.PASS] * 8
    assert [cell.floor_outcome for cell in reading[Load.LOW].cells] == [Outcome.PASS] * 9
    # Several-agents is (1 + 2/3) / 2 and the combined result (1 + 5/6) / 2: both above 80%.
    assert loaded.family(Family.RESEARCH).rate == 2 / 3
    assert loaded.shapes == {Shape.HANDS_ON: 1.0, Shape.SEVERAL_AGENTS: 5 / 6}
    assert loaded.combined == 11 / 12


def test_invalid_and_superseded_executions_are_kept_and_left_out_of_the_denominator() -> None:
    (retried,) = chosen(Load.CLASS, Family.ANALYSIS, instance=1, attempt=0)
    (broken,) = chosen(Load.CLASS, Family.RESEARCH, instance=2, attempt=1)
    (missing,) = chosen(Load.CLASS, Family.OPTIMIZATION, Harness.PI, instance=1, attempt=0)
    timed_out = chosen(Load.CLASS, Family.OPTIMIZATION, Harness.PI, instance=1, attempt=1)
    run = records(
        timed_out,
        invalid={retried, broken},
        absent={missing},
        # An invalid execution replaced by a failure.
        also=(ran(retried, FAILED, execution=1000, replaces=retried),),
    )
    loaded = read(run)[Load.CLASS]

    def counts(family: Family, harness: Harness) -> tuple[int, int, int, int]:
        cell = loaded.cell(family, harness)
        return cell.executions, cell.attempts, cell.successes, cell.instances_all_passed

    # The replaced invalid execution is kept and the trial counts once, as the failure: 8 of 9.
    assert counts(Family.ANALYSIS, Harness.CLAUDE_CODE) == (10, 9, 8, 2)
    assert loaded.cell(Family.ANALYSIS, Harness.CLAUDE_CODE).floor_outcome is Outcome.PASS
    # An invalid execution that nothing replaced is out of the denominator, and the cell is not
    # read as 8 of 8: a planned trial is missing, so its floor is not met.
    research = loaded.cell(Family.RESEARCH, Harness.CLAUDE_CODE)
    assert counts(Family.RESEARCH, Harness.CLAUDE_CODE) == (9, 8, 8, 2)
    assert (research.planned, research.complete, research.floor_outcome) == (9, False, Outcome.INCONCLUSIVE)
    # A trial with no record is missing too. Its instance has one success in two attempts and the
    # other two instances three in three: the rate is (1/2 + 1 + 1) / 3, not 7 of 8.
    pi = loaded.cell(Family.OPTIMIZATION, Harness.PI)
    assert counts(Family.OPTIMIZATION, Harness.PI) == (8, 8, 7, 2)
    assert (pi.success_share, pi.rate, pi.floor_outcome) == (7 / 8, 5 / 6, Outcome.INCONCLUSIVE)
    # Analysis is 8/9 and research 3/3 on what counts.
    assert loaded.shapes[Shape.SEVERAL_AGENTS] == float(Fraction(17, 18))


def test_a_cell_that_is_not_valid_for_qualification_leaves_its_trials_missing() -> None:
    (first,) = {CLASS.trials[trial].cell for trial in chosen(Load.CLASS, Family.DEBUGGING, attempt=0)}
    run = records()
    flagged = tuple(
        window(cell.cell, InvalidFlag.EXTERNAL_TRAFFIC) if cell.cell == first else cell for cell in run.cells
    )
    reading = read(replace(run, cells=flagged))

    for harness in (Harness.CLAUDE_CODE, Harness.CODEX):
        cell = reading[Load.CLASS].cell(Family.DEBUGGING, harness)
        assert (cell.executions, cell.attempts, cell.successes) == (9, 6, 6)
        assert cell.floor_outcome is Outcome.INCONCLUSIVE
    assert all(cell.floor_outcome is Outcome.PASS for cell in reading[Load.LOW].cells)
    with pytest.raises(QualityError):
        read(replace(run, trials=(*run.trials, ran(len(CLASS.trials)))))


def test_a_cell_run_again_counts_only_what_ran_in_its_valid_window() -> None:
    """The first research cell at class load is flagged with three passes in it, and run again
    from the 40th minute: two of its trials have an execution there, a pass and a timeout."""
    absent, passed, timed_out = sorted(chosen(Load.CLASS, Family.RESEARCH, attempt=0))
    cell = CLASS.trials[absent].cell
    again = replace(window(cell), started_ms=2_400_000, admissions_ended_ms=4_200_000, ended_ms=4_200_000)
    run = records()
    run = replace(
        run,
        cells=(
            *(window(one.cell, InvalidFlag.EXTERNAL_TRAFFIC) if one.cell == cell else one for one in run.cells),
            again,
        ),
        trials=(
            *run.trials,
            replace(ran(passed, execution=1000, replaces=passed), started_ms=2_400_000, ended_ms=2_460_000),
            replace(
                ran(timed_out, FAILED, execution=1001, replaces=timed_out), started_ms=2_460_000, ended_ms=2_520_000
            ),
        ),
    )
    loaded = read(run)[Load.CLASS]
    research = loaded.cell(Family.RESEARCH, Harness.CLAUDE_CODE)

    # The 6 trials of the two other cells, and the 2 run again: 7 of 8, and not the pass that
    # the flagged window holds for the ninth, nor the one it holds for the timeout.
    assert (research.executions, research.attempts, research.successes) == (11, 8, 7)
    assert (research.instances_all_passed, research.floor_outcome) == (1, Outcome.INCONCLUSIVE)
    # Both windows of the cell are paid for: 13 windows of thirty minutes on two hosts.
    assert (loaded.throughput.elapsed_hours, loaded.throughput.instance_hours) == (6.5, 13.0)
    # Two valid records of one cell, or two windows of it that overlap, measure nothing.
    with pytest.raises(QualityError):
        read(replace(run, cells=(*records().cells, again)))
    with pytest.raises(QualityError):
        read(replace(run, cells=(*run.cells[:-1], replace(again, started_ms=1_000_000))))


def test_a_completed_attempt_of_a_measured_cell_cannot_be_run_again_into_a_pass() -> None:
    """One research instance times out on its three repeats at class load, in valid cells: 6 of 9
    is the result. Passes that name the timeouts as replaced are refused, and passes that name
    nothing leave the three trials with two executions each, which count for nothing."""
    timed_out = sorted(chosen(Load.CLASS, Family.RESEARCH, instance=1))
    for replaces in (True, False):
        passes = tuple(ran(trial, execution=1000 + trial, replaces=trial if replaces else None) for trial in timed_out)
        run = records(timed_out, also=passes)
        if replaces:
            with pytest.raises(QualityError):
                read(run)
        else:
            research = read(run)[Load.CLASS].cell(Family.RESEARCH, Harness.CLAUDE_CODE)
            assert (research.executions, research.attempts, research.successes) == (12, 6, 6)
            assert research.floor_outcome is Outcome.INCONCLUSIVE


def test_time_and_host_cost_count_failed_and_background_work_and_no_success_leaves_the_ratio_undefined() -> None:
    """At low load every research trial fails, and an analysis trial takes two minutes."""
    slow = chosen(Load.LOW, Family.ANALYSIS)
    run = records(chosen(Load.LOW, Family.RESEARCH))
    run = replace(run, trials=tuple(replace(one, ended_ms=120_000) if one.trial in slow else one for one in run.trials))
    reading = read(run)
    low, loaded = reading[Load.LOW], reading[Load.CLASS]

    # Research at low load: three cells of thirty minutes on two hosts, and no success.
    research = low.family(Family.RESEARCH).throughput
    assert (research.successes, research.attempts) == (0, 9)
    assert (research.elapsed_hours, research.instance_hours, research.host_cost_estimate_usd) == (1.5, 3.0, 6.0)
    assert research.tasks_per_hour == 0.0
    assert research.successful_task_time_ms is None
    assert research.host_cost_estimate_per_success_usd is None
    cell = low.cell(Family.RESEARCH, Harness.CLAUDE_CODE)
    assert (cell.successes, cell.attempts, cell.success_share, cell.floor_outcome) == (0, 9, 0.0, Outcome.FAIL)
    # Analysis: 9 successes in 1.5 hours, for 3 instance-hours at 2 dollars.
    analysis = low.family(Family.ANALYSIS).throughput
    assert (analysis.successful_task_time_ms, analysis.tasks_per_hour) == (120_000.0, 6.0)
    assert analysis.host_cost_estimate_per_success_usd == 6.0 / 9
    # The load: twelve cells are 6 hours and 12 instance-hours, 24 dollars, which the 72 successes
    # share though 9 trials failed. 63 of them took one minute and 9 two: 67.5 seconds.
    assert (low.throughput.successes, low.throughput.attempts) == (72, 81)
    assert (low.throughput.elapsed_hours, low.throughput.instance_hours) == (6.0, 12.0)
    assert (low.throughput.tasks_per_hour, low.throughput.successful_task_time_ms) == (12.0, 67_500.0)
    assert low.throughput.host_cost_estimate_per_success_usd == 24.0 / 72
    # At class load the 81 trials took 81 minutes, and the hosts are paid for the whole of the
    # loaded cells, which the background traffic fills: 12 instance-hours again.
    assert (loaded.throughput.instance_hours, loaded.throughput.host_cost_estimate_usd) == (12.0, 24.0)
    assert loaded.throughput.host_cost_estimate_per_success_usd == 24.0 / 81


def test_no_counted_attempt_and_no_host_count_leave_what_rests_on_them_undefined() -> None:
    run = records(invalid=chosen(Load.LOW, Family.RESEARCH))
    unsized = tuple(window(cell.cell, hosts=None) for cell in run.cells)
    low = read(replace(run, cells=unsized))[Load.LOW]
    research = low.cell(Family.RESEARCH, Harness.CLAUDE_CODE)

    assert (research.executions, research.attempts, research.success_share, research.rate) == (9, 0, None, None)
    assert research.floor_outcome is Outcome.INCONCLUSIVE
    assert (low.shapes[Shape.SEVERAL_AGENTS], low.combined, low.shapes[Shape.HANDS_ON]) == (None, None, 1.0)
    assert (low.throughput.elapsed_hours, low.throughput.tasks_per_hour) == (6.0, 12.0)
    assert (low.throughput.instance_hours, low.throughput.host_cost_estimate_per_success_usd) == (None, None)


def test_one_seat_reads_both_loads_from_the_same_trials_each_once() -> None:
    for seats in (1, 8):
        planned = plan.expand(SUITE, Profile.QUALIFY, seats)
        for trial in planned.workload.trials:
            assert set(quality.loads_of(planned.workload, trial.cell)) == set(planned.loads_of(trial))
    alone = plan.expand(SUITE, Profile.QUALIFY, 1).workload
    assert len(alone.trials) == 81
    worse = records(chosen(Load.LOW, Family.RESEARCH, instance=1, workload=alone), workload=alone)
    reading = read(worse)

    for load in Load:
        research = reading[load].cell(Family.RESEARCH, Harness.CLAUDE_CODE)
        assert (research.executions, research.attempts, research.successes) == (9, 9, 6)
        assert (reading[load].throughput.attempts, reading[load].throughput.successes) == (81, 78)
    comparison = quality.compare(worse, records(workload=alone))
    for shape in SCOPES:
        assert replace(comparison.of(Load.LOW, shape), load=Load.CLASS) == comparison.of(Load.CLASS, shape)
    assert comparison.of(Load.CLASS, Shape.SEVERAL_AGENTS).reason is Reason.SPANS_MARGIN


def test_a_load_the_plan_does_not_run_has_no_reading_and_no_decision() -> None:
    screen = plan.expand(SUITE, Profile.SCREEN, 8).workload
    assert set(read(records(workload=screen))) == {Load.CLASS}
    comparison = quality.compare(records(workload=screen), records(workload=screen))
    for shape in SCOPES:
        unread = comparison.of(Load.LOW, shape)
        assert (unread.outcome, unread.reason) == (Outcome.INCONCLUSIVE, Reason.MISSING_PAIRS)


# --- Deciding a candidate against a reference -------------------------------------------------


def test_the_difference_is_candidate_minus_reference() -> None:
    """One run fails one research instance at low load, on its three repeats. A resample holds
    that instance 0 to 3 times, so research changes by 0, 1/3, 2/3 or 1 and several-agents, with
    analysis unchanged, by half of it: 0 to 50 points, each with more than 2.5% of the draws."""
    worse, same = records(chosen(Load.LOW, Family.RESEARCH, instance=1)), records()
    as_candidate = quality.compare(worse, same).of(Load.LOW, Shape.SEVERAL_AGENTS)
    as_reference = quality.compare(same, worse).of(Load.LOW, Shape.SEVERAL_AGENTS)

    assert as_candidate.difference_points == pytest.approx(-100 / 6)
    assert (as_candidate.lower_points, as_candidate.upper_points) == (-50.0, 0.0)
    assert (as_candidate.outcome, as_candidate.reason) == (Outcome.INCONCLUSIVE, Reason.SPANS_MARGIN)
    assert as_reference.difference_points == pytest.approx(100 / 6)
    assert (as_reference.lower_points, as_reference.upper_points) == (0.0, 50.0)
    assert as_reference.outcome is Outcome.PASS


def test_repeats_are_resampled_within_each_instance() -> None:
    """The candidate fails the first repeat of every research instance at low load. Each instance
    loses 1/3, so resampling instances alone would change nothing: the interval comes from the 9
    sampled repeats, K of them the failed one, and several-agents loses 50 K / 9 points. K is 6
    or more in 4.2% of resamples and 7 or more in 0.8%: the lower bound is at K = 6. K is 0 in
    2.6%, so the upper bound is at K = 0 or K = 1."""
    decision = quality.compare(records(chosen(Load.LOW, Family.RESEARCH, attempt=0)), records()).of(
        Load.LOW, Shape.SEVERAL_AGENTS
    )
    lower, observed, upper = interval(decision)

    assert (lower, observed) == (pytest.approx(-100 / 3), pytest.approx(-50 / 3))
    assert upper in (pytest.approx(-50 / 9), 0.0)


def test_a_draw_keeps_every_harness_of_a_family_at_its_equal_weight() -> None:
    """Debugging is planned as two tasks: one instance on Claude Code and Codex, and two on
    Claude Code alone. The candidate fails it on Codex and the reference on Claude Code, so it
    gains 1 on one harness and loses 1 on the other: 0 as observed. A draw without the Codex
    instance would read the gain alone, +50 points of hands-on. No draw is: the Codex instance
    is in each, and every draw gives 0."""
    task = next(one for one in SUITE.suite.tasks if one.family is Family.DEBUGGING)
    both = replace(task, instances=task.instances[:1])
    alone = replace(
        task,
        task_id="debugging.t02",
        harnesses=task.harnesses[:1],
        instances=tuple(replace(one, instance_id=one.instance_id.replace("t01", "t02")) for one in task.instances[1:]),
    )
    assert [assignment.harness for assignment in alone.harnesses] == [Harness.CLAUDE_CODE]
    tasks = sorted([one for one in SUITE.suite.tasks if one != task] + [both, alone], key=lambda one: one.task_id)
    spec = replace(
        SUITE,
        suite=replace(SUITE.suite, tasks=tuple(tasks)),
        background=SUITE.background | {alone.task_id: Shape.HANDS_ON},
    )
    workload = plan.expand(spec, Profile.QUALIFY, 8).workload

    def failing_on(harness: Harness) -> Records:
        return records(chosen(Load.LOW, Family.DEBUGGING, harness, workload=workload), workload=workload)

    decision = quality.compare(failing_on(Harness.CODEX), failing_on(Harness.CLAUDE_CODE)).of(Load.LOW, Shape.HANDS_ON)

    assert (decision.outcome, decision.reason) == (Outcome.INCONCLUSIVE, Reason.DEGENERATE_PRECISION)
    assert (decision.difference_points, decision.lower_points, decision.upper_points) == (0.0, None, None)


def test_the_same_records_give_the_same_intervals_in_any_order() -> None:
    assert CLASS.sampling.bootstrap_draws == 10_000
    candidate = records({trial.trial for trial in CLASS.trials if trial.trial % 7 == 0})
    reference = records({trial.trial for trial in CLASS.trials if trial.trial % 5 == 0})
    first = quality.compare(candidate, reference)
    again = quality.compare(
        replace(candidate, trials=candidate.trials[::-1], cells=candidate.cells[::-1]),
        replace(reference, trials=reference.trials[::-1], cells=reference.cells[::-1]),
    )

    assert first == again
    assert len(first.decisions) == 6
    for decision in first.decisions:
        assert decision.draws == 10_000
        lower, observed, upper = interval(decision)
        assert lower < observed < upper


@pytest.mark.parametrize("load", Load)
def test_a_lower_bound_exactly_on_the_margin_passes(load: Load) -> None:
    """The candidate gains optimization on two of its five harnesses and loses debugging on one
    of its two: 2/5 and -1/2, so hands-on changes by -1/20 in every resample. It also gains one
    optimization instance on a third harness, which a resample holds 0 to 3 times: hands-on is
    -5 points plus 0, 10/3, 20/3 or 10. No instance in 8 of 27 resamples: the 2.5th percentile is
    -5 itself, and all three in 1 of 27: the 97.5th is +5."""
    gained = chosen(load, Family.OPTIMIZATION, Harness.CODEX) | chosen(load, Family.OPTIMIZATION, Harness.OPENCODE)
    gained |= chosen(load, Family.OPTIMIZATION, Harness.PI, instance=1)
    decision = quality.compare(records(chosen(load, Family.DEBUGGING, Harness.CODEX)), records(gained)).of(
        load, Shape.HANDS_ON
    )

    assert decision.margin_points == CLASS.objectives.paired_margin_points == -5.0
    assert (decision.lower_points, decision.upper_points) == (-5.0, 5.0)
    assert decision.difference_points == pytest.approx(-5 + 10 / 3)
    assert (decision.outcome, decision.reason) == (Outcome.PASS, None)


@pytest.mark.parametrize("load", Load)
def test_each_shape_is_decided_at_each_load_on_its_own(load: Load) -> None:
    """At `load`, hands-on passes and several-agents fails. At the other load, hands-on spans the
    margin and several-agents lacks a pair."""
    elsewhere = other(load)
    (lost,) = chosen(elsewhere, Family.RESEARCH, instance=3, attempt=0)
    # The reference failed one debugging instance on Codex, which the candidate passes: debugging
    # gains 0 to 1/2 and hands-on 0 to 25 points, 100/12 as observed.
    reference = records(chosen(load, Family.DEBUGGING, Harness.CODEX, instance=1))
    candidate = records(
        # Two research instances fail every repeat and the third two of three: research loses 8/9
        # as observed, and no resample with more than 2.5% of the draws loses less than 1/9.
        chosen(load, Family.RESEARCH, instance=1)
        | chosen(load, Family.RESEARCH, instance=2)
        | chosen(load, Family.RESEARCH, instance=3, attempt=0)
        | chosen(load, Family.RESEARCH, instance=3, attempt=1)
        # One debugging instance fails on both harnesses: hands-on loses 0 to 50 points.
        | chosen(elsewhere, Family.DEBUGGING, instance=1),
        invalid={lost},
    )
    comparison = quality.compare(candidate, reference)

    passing = comparison.of(load, Shape.HANDS_ON)
    assert (passing.outcome, passing.lower_points, passing.upper_points) == (Outcome.PASS, 0.0, 25.0)
    assert passing.difference_points == pytest.approx(100 / 12)
    failing = comparison.of(load, Shape.SEVERAL_AGENTS)
    assert (failing.outcome, failing.reason) == (Outcome.FAIL, None)
    assert failing.difference_points == pytest.approx(-400 / 9)
    lower, _observed, upper = interval(failing)
    assert -50.0 <= lower < upper < -5.0
    # The combined result weighs the two shapes equally.
    assert comparison.of(load).difference_points == pytest.approx((100 / 12 - 400 / 9) / 2)

    spanning = comparison.of(elsewhere, Shape.HANDS_ON)
    assert (spanning.outcome, spanning.reason) == (Outcome.INCONCLUSIVE, Reason.SPANS_MARGIN)
    assert (spanning.lower_points, spanning.upper_points) == (-50.0, 0.0)
    for shape in (Shape.SEVERAL_AGENTS, None):
        unpaired = comparison.of(elsewhere, shape)
        assert (unpaired.outcome, unpaired.reason) == (Outcome.INCONCLUSIVE, Reason.MISSING_PAIRS)
        assert (unpaired.difference_points, unpaired.lower_points, unpaired.upper_points) == (None, None, None)


def test_resamples_that_all_agree_report_the_observed_difference_and_no_interval() -> None:
    """Research fails whole at class load: every resample loses it whole, 50 points of the shape."""
    comparison = quality.compare(records(chosen(Load.CLASS, Family.RESEARCH)), records())

    for load, shape in ((load, shape) for load in Load for shape in SCOPES):
        decision = comparison.of(load, shape)
        lost = {Shape.SEVERAL_AGENTS: -50.0, None: -25.0}.get(shape, 0.0) if load is Load.CLASS else 0.0
        assert (decision.outcome, decision.reason) == (Outcome.INCONCLUSIVE, Reason.DEGENERATE_PRECISION)
        assert (decision.difference_points, decision.lower_points, decision.upper_points) == (lost, None, None)


@pytest.mark.parametrize(
    "changed",
    [
        replace(CLASS, suite_digest="sha256:" + "1" * 64),
        replace(CLASS, input_seed=CLASS.input_seed + 1),
        replace(CLASS, protocol_digest="sha256:" + "2" * 64),
        replace(CLASS, limits=replace(CLASS.limits, trial_wall_time_ms=600_000)),
    ],
    ids=["task-inputs", "seeds", "background-load", "limits"],
)
def test_runs_of_two_workloads_are_not_compared(changed: Workload) -> None:
    """The trials keep their instance, harness and repeat; what they ran under is not the same."""
    better = records(workload=changed)
    comparison = quality.compare(better, records(chosen(Load.LOW, Family.RESEARCH, instance=1)))

    assert not comparison.matched
    assert len(comparison.decisions) == 6
    for decision in comparison.decisions:
        assert (decision.outcome, decision.reason) == (Outcome.INCONCLUSIVE, Reason.UNMATCHED_WORKLOAD)
        assert decision.difference_points is None
