"""The driver's journal of a run (`evaluate.journal`): what a restart finds after a write was cut
short, who may own a run, and what a resume keeps, runs again and refuses
(docs/design/agent-session-evaluation.md, "Booking and running").

A driver that dies is a write that does not happen, then a handle that is dropped and a run that
is opened again. Everything is under the test's own directory: no network, no account.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from evaluate import journal as journals
from evaluate.contracts import (
    CellRecord,
    CellStatus,
    Document,
    FailureReason,
    Grade,
    InvalidFlag,
    RequestOutcome,
    RequestRecord,
    Route,
    Traffic,
    TrialOutcome,
    TrialRecord,
)
from evaluate.journal import Action, AttemptStatus, IntentStatus, Journal, JournalError, Pending, Refusal, Step

if TYPE_CHECKING:
    from collections.abc import Generator

REPOSITORY = Path(__file__).resolve().parents[3]
WORKLOAD, SERVED, OTHER = ("sha256:" + digit * 64 for digit in "abc")
ISSUE = 12
# What the driver is about to send for each action, and what comes back. Both stay private.
PAYLOADS: dict[Action, dict[str, Any]] = {
    Action.CREATE_ISSUE: {"title": "One host against two"},
    Action.BOOK: {"pool": "coursework", "instances": 2, "minutes": 45},
    Action.DELIVER: {"body": "Run finished: 3 of 4 trials passed."},
}
RESULTS: dict[Action, dict[str, Any]] = {
    Action.CREATE_ISSUE: {"number": ISSUE},
    Action.BOOK: {"pool": "coursework", "instances": 2, "ends_at": 1_800_002_700},
    Action.DELIVER: {"comment": 3},
}
# The step a restart does first when nothing of the action was saved.
BEFORE = {Action.CREATE_ISSUE: Step.CREATE_ISSUE, Action.BOOK: Step.RUN_CELLS, Action.DELIVER: Step.RUN_CELLS}
RECONCILE = {
    Action.CREATE_ISSUE: Step.RECONCILE_ISSUE,
    Action.BOOK: Step.RECONCILE_BOOKING,
    Action.DELIVER: Step.RECONCILE_DELIVERY,
}


def request(cell: int, number: int) -> RequestRecord:
    return RequestRecord(
        request=number,
        cell=cell,
        student=0,
        conversation=number,
        turn=0,
        traffic=Traffic.BACKGROUND,
        trial=None,
        route=Route.MESSAGES,
        eligible_ms=1_000,
        sent_ms=1_000,
        first_output_ms=None,
        first_answer_ms=None,
        last_output_ms=None,
        ended_ms=2_000,
        outcome=RequestOutcome.COMPLETED,
        finish_reason=None,
        usage=None,
    )


def trial(execution: int, outcome: TrialOutcome) -> TrialRecord:
    passed = outcome is TrialOutcome.PASSED
    return TrialRecord(
        execution=execution,
        trial=execution,
        replaces=None,
        student=0,
        started_ms=1_000,
        ended_ms=2_000,
        outcome=outcome,
        failure=None if passed else FailureReason.TIMEOUT,
        invalid=None,
        grade=Grade(properties=(True,), within_limits=True) if passed else None,
        model_turns=None,
        retries=None,
        usage=None,
    )


def cell_record(cell: int, *flags: InvalidFlag) -> CellRecord:
    """What a cell came to. With a flag it is invalid for qualification."""
    return CellRecord(
        cell=cell,
        started_ms=1_000,
        admissions_ended_ms=2_000,
        ended_ms=3_000,
        status=CellStatus.COMPLETE,
        invalid_flags=flags,
        conversations_planned=1,
        conversations_started=1,
        conversations_completed=1,
        conversations_abandoned=0,
        hosts_observed=None,
        driver_lag_p95_ms=None,
    )


def measure(journal: Journal, cell: int, *observations: Document, flags: tuple[InvalidFlag, ...] = ()) -> int:
    """A whole cell: started, observed and checkpointed. Gives its attempt."""
    attempt = journal.start_cell(cell, WORKLOAD, SERVED).ordinal
    for observation in observations:
        journal.append(attempt, observation)
    journal.checkpoint_cell(attempt, cell_record(cell, *flags))
    return attempt


class Died(Exception):
    """Stands for the driver's death inside a write."""


@contextlib.contextmanager
def dies_in(call: str) -> Generator[None]:
    """The driver dies in the journal's next `os.replace`, so the state it wrote is never put in
    place, or in its next `os.write`, halfway through the line of an observation."""
    real = getattr(os, call)

    def die(*arguments: Any) -> None:
        if call == "write":
            descriptor, data = arguments
            real(descriptor, data[: len(data) // 2])
        raise Died

    with pytest.MonkeyPatch.context() as patch, pytest.raises(Died):
        patch.setattr(os, call, die)
        yield


def restart(journal: Journal, root: Path) -> Journal:
    """The driver gone and started again. Its lock ended with it; the root holds the one run, by
    the ID saved at the start, and the new driver opens it."""
    journal.close()
    assert journals.runs(root) == (journal.run_id,)
    again = Journal.open(root, journal.run_id)
    assert again.run_id == journal.run_id
    return again


@contextlib.contextmanager
def refused(reason: Refusal) -> Generator[None]:
    with pytest.raises(JournalError) as raised:
        yield
    assert raised.value.reason is reason


# --- Writes cut short ------------------------------------------------------------------------


@pytest.mark.parametrize("action", Action)
def test_an_intent_whose_write_was_cut_has_not_happened(tmp_path: Path, action: Action) -> None:
    journal = Journal.create(tmp_path)
    if action is not Action.CREATE_ISSUE:
        journal.adopt_issue(ISSUE)
    earlier = measure(journal, 1, request(1, 0), trial(0, TrialOutcome.FAILED))

    with dies_in("replace"):
        journal.intend(action, PAYLOADS[action])
    assert journal.intents == ()  # not in the handle either: the driver did not go on to act

    again = restart(journal, tmp_path)
    assert again.pending() == Pending(BEFORE[action], None)
    assert again.intents == ()
    assert again.observations(earlier) == (request(1, 0), trial(0, TrialOutcome.FAILED))
    assert again.intend(action, PAYLOADS[action]).status is IntentStatus.PENDING


@pytest.mark.parametrize("action", Action)
def test_an_action_whose_confirmation_was_cut_is_the_one_to_reconcile(tmp_path: Path, action: Action) -> None:
    journal = Journal.create(tmp_path)
    if action is not Action.CREATE_ISSUE:
        journal.adopt_issue(ISSUE)
    earlier = measure(journal, 1, request(1, 0), trial(0, TrialOutcome.FAILED))
    intent = journal.intend(action, PAYLOADS[action])
    assert journal.run_id in intent.marker

    with dies_in("replace"):
        journal.confirm(intent.ordinal, RESULTS[action])

    again = restart(journal, tmp_path)
    # The intent is back as it was saved before the action: its marker and what was sent.
    assert again.pending() == Pending(RECONCILE[action], intent)
    assert intent.payload == PAYLOADS[action]
    assert again.observations(earlier) == (request(1, 0), trial(0, TrialOutcome.FAILED))
    # A second one of the same action would be a second issue, booking or comment.
    with refused(Refusal.ACTION_PENDING):
        again.intend(action, PAYLOADS[action])

    # The actual record shows the marker: the action is confirmed, and the run goes on.
    assert again.confirm(intent.ordinal, RESULTS[action]).result == RESULTS[action]
    assert again.pending() == Pending(Step.RUN_CELLS, None)
    assert again.issue_number == ISSUE


def test_a_checkpoint_that_was_cut_leaves_the_cell_interrupted_and_every_observation(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    journal.adopt_issue(ISSUE)
    earlier = measure(journal, 1, request(1, 0))
    cut = journal.start_cell(2, WORKLOAD, SERVED).ordinal
    journal.append(cut, request(2, 1))

    with dies_in("replace"):
        journal.checkpoint_cell(cut, cell_record(2))

    again = restart(journal, tmp_path)
    assert again.pending() == Pending(Step.RUN_CELLS, None)
    assert again.attempts[earlier].status is AttemptStatus.COMPLETE
    assert again.observations(earlier) == (request(1, 0),)
    # The cell was not saved in part: it has no record, and what it observed is still there.
    assert (again.attempts[cut].status, again.attempts[cut].record) == (AttemptStatus.INTERRUPTED, None)
    assert again.observations(cut) == (request(2, 1),)
    assert again.resume([(1, 10), (2, 10)], 60, WORKLOAD, SERVED).selected == (2,)


def test_an_observation_cut_inside_its_line_is_left_out_and_the_ones_before_it_stay(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    cut = journal.start_cell(1, WORKLOAD, SERVED).ordinal
    whole = (request(1, 0), trial(0, TrialOutcome.FAILED))
    for observation in whole:
        journal.append(cut, observation)

    with dies_in("write"):
        journal.append(cut, request(1, 1))
    # A driver that survived the failed write cannot add to the unfinished line, or checkpoint it.
    with refused(Refusal.OBSERVATION_UNFINISHED):
        journal.append(cut, request(1, 2))
    with refused(Refusal.OBSERVATION_UNFINISHED):
        journal.checkpoint_cell(cut, cell_record(1))

    again = restart(journal, tmp_path)
    assert again.observations(cut) == whole
    assert (again.attempts[cut].status, again.attempts[cut].observed) == (AttemptStatus.INTERRUPTED, 2)

    replacement = measure(again, 1, request(1, 2))
    last = restart(again, tmp_path)
    assert last.attempts[cut].replaced_by == replacement
    assert last.observations(cut) == whole
    assert last.observations(replacement) == (request(1, 2),)


# --- One owner, and a private place ----------------------------------------------------------


def test_a_run_has_one_owner_at_a_time(tmp_path: Path) -> None:
    owner = Journal.create(tmp_path)

    with refused(Refusal.RUN_ALREADY_OWNED):
        Journal.open(tmp_path, owner.run_id)
    owner.adopt_issue(ISSUE)  # the refused one took nothing from the owner

    owner.close()
    with refused(Refusal.JOURNAL_CLOSED):
        owner.intend(Action.BOOK, PAYLOADS[Action.BOOK])
    with Journal.open(tmp_path, owner.run_id) as successor:
        assert successor.issue_number == ISSUE


def test_a_run_is_kept_private_under_the_ignored_output_directory(tmp_path: Path) -> None:
    assert journals.DEFAULT_ROOT.relative_to(REPOSITORY).parts[:2] == (".cache", "eval")
    assert ".cache/" in (REPOSITORY / ".gitignore").read_text(encoding="utf-8").splitlines()

    with Journal.create(tmp_path) as journal:
        measure(journal, 1, request(1, 0))
        written = [journal.directory, *journal.directory.rglob("*")]
    assert len(written) > 3  # the state, the lock, the observations and their directory
    assert all(one.stat().st_mode & 0o077 == 0 for one in written)


def test_only_an_observation_of_a_running_attempt_of_its_cell_is_added(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    attempt = journal.start_cell(1, WORKLOAD, SERVED).ordinal
    journal.append(attempt, request(1, 0))

    for other in (cell_record(1), request(2, 1)):  # the checkpoint's record, and another cell's request
        with refused(Refusal.NOT_AN_OBSERVATION):
            journal.append(attempt, other)
    with refused(Refusal.NOT_AN_OBSERVATION):
        journal.append(attempt, {"kind": "request"})  # ty: ignore[invalid-argument-type] - what is refused
    journal.checkpoint_cell(attempt, cell_record(1))
    with refused(Refusal.ATTEMPT_NOT_RUNNING):
        journal.append(attempt, request(1, 1))

    assert journal.observations(attempt) == (request(1, 0),)


# --- Intents and checkpoints -----------------------------------------------------------------


def test_the_issue_number_is_journaled_before_a_booking_or_a_delivery(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    assert journal.pending() == Pending(Step.CREATE_ISSUE, None)
    for action in (Action.BOOK, Action.DELIVER):
        with refused(Refusal.ISSUE_NOT_JOURNALED):
            journal.intend(action, PAYLOADS[action])

    creation = journal.intend(Action.CREATE_ISSUE, PAYLOADS[Action.CREATE_ISSUE])
    with refused(Refusal.ISSUE_NOT_JOURNALED):  # asked for, and not known to exist yet
        journal.intend(Action.BOOK, PAYLOADS[Action.BOOK])
    journal.confirm(creation.ordinal, RESULTS[Action.CREATE_ISSUE])
    assert journal.issue_number == ISSUE

    # A booking the record does not show is ruled out, and the next one is another intent.
    lost = journal.intend(Action.BOOK, PAYLOADS[Action.BOOK])
    journal.rule_out(lost.ordinal)
    booking = journal.intend(Action.BOOK, PAYLOADS[Action.BOOK])
    assert booking.marker != lost.marker
    journal.confirm(booking.ordinal, RESULTS[Action.BOOK])

    again = restart(journal, tmp_path)
    assert [intent.status for intent in again.intents] == [
        IntentStatus.CONFIRMED,
        IntentStatus.RULED_OUT,
        IntentStatus.CONFIRMED,
    ]
    assert again.intents[booking.ordinal].result == RESULTS[Action.BOOK]  # the exact booking returned


def test_a_checkpoint_holds_the_whole_cell_and_its_fingerprints(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    attempt = measure(journal, 1, request(1, 0), request(1, 1))

    again = restart(journal, tmp_path)
    saved = again.attempts[attempt]
    assert (saved.workload, saved.served) == (WORKLOAD, SERVED)
    assert (saved.status, saved.record, saved.observed) == (AttemptStatus.COMPLETE, cell_record(1), 2)

    # The checkpoint is of those two observations: a file that lost one is not read as the cell.
    (file,) = (again.directory / "observations").iterdir()
    file.write_bytes(file.read_bytes().splitlines(keepends=True)[0])
    with refused(Refusal.OBSERVATIONS_DAMAGED):
        again.observations(attempt)


# --- Resuming --------------------------------------------------------------------------------


def test_a_completed_failure_stays_in_the_denominator_when_another_cell_is_incomplete(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    measured = measure(journal, 1, trial(0, TrialOutcome.PASSED), trial(1, TrialOutcome.FAILED))
    journal.append(journal.start_cell(2, WORKLOAD, SERVED).ordinal, trial(2, TrialOutcome.PASSED))

    again = restart(journal, tmp_path)  # cell 2 was running: it is interrupted
    resumption = again.resume([(1, 10), (2, 10)], 60, WORKLOAD, SERVED)

    # The cell that was measured is not run again for a better result, and its failure counts.
    assert [attempt.ordinal for attempt in resumption.reused] == [measured]
    assert resumption.selected == (2,)
    counted = [
        observation.outcome
        for attempt in resumption.reused
        for observation in again.observations(attempt.ordinal)
        if isinstance(observation, TrialRecord) and observation.counts_as_attempt
    ]
    assert counted == [TrialOutcome.PASSED, TrialOutcome.FAILED]


def test_a_cell_is_reused_only_under_the_fingerprints_it_was_measured_with(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    measure(journal, 1, request(1, 0))
    before = journal.attempts

    for workload, served in ((OTHER, SERVED), (WORKLOAD, OTHER)):
        with refused(Refusal.INCOMPATIBLE_REUSE):
            journal.resume([(1, 10), (2, 10)], 60, workload, served)
        with refused(Refusal.INCOMPATIBLE_REUSE):  # nor measured beside it under another identity
            journal.start_cell(2, workload, served)
    assert journal.attempts == before

    resumption = journal.resume([(1, 10), (2, 10)], 60, WORKLOAD, SERVED)
    assert (resumption.reused, resumption.selected) == (before, (2,))


def test_a_resume_runs_what_is_not_measured_whole_and_in_order_within_the_window(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    measure(journal, 1, request(1, 0))
    measure(journal, 2, request(2, 1), flags=(InvalidFlag.EXTERNAL_TRAFFIC,))
    cells = [(1, 30), (2, 30), (3, 30), (4, 10)]

    def resume(window_ms: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
        resumption = journal.resume(cells, window_ms, WORKLOAD, SERVED)
        return resumption.selected, resumption.deferred

    # The invalid cell and the one never started are run; the valid one takes none of the window.
    assert resume(60) == ((2, 3), (4,))
    # A cell that does not fit is not cut to fit, and no later cell goes before it.
    assert resume(45) == ((2,), (3, 4))
    assert resume(29) == ((), (2, 3, 4))


def test_a_replaced_attempt_keeps_its_evidence_and_names_its_replacement(tmp_path: Path) -> None:
    journal = Journal.create(tmp_path)
    partial = journal.start_cell(1, WORKLOAD, SERVED).ordinal
    journal.append(partial, request(1, 0))
    journal = restart(journal, tmp_path)  # interrupted: the driver died during the cell
    invalid = measure(journal, 1, request(1, 1), request(1, 2), flags=(InvalidFlag.HOST_TURNOVER,))
    valid = measure(journal, 1, request(1, 3))

    again = restart(journal, tmp_path)
    assert [(attempt.status, attempt.replaced_by) for attempt in again.attempts] == [
        (AttemptStatus.INTERRUPTED, invalid),
        (AttemptStatus.COMPLETE, valid),
        (AttemptStatus.COMPLETE, None),
    ]
    assert again.observations(partial) == (request(1, 0),)
    assert again.observations(invalid) == (request(1, 1), request(1, 2))
    assert again.attempts[invalid].record == cell_record(1, InvalidFlag.HOST_TURNOVER)
    # Only the last attempt of the cell is the one a resume counts.
    assert again.resume([(1, 10)], 60, WORKLOAD, SERVED).reused == (again.attempts[valid],)
