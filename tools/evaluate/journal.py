"""The driver's private journal of one evaluation run: what a restart needs to go on with it
(docs/design/agent-session-evaluation.md, "Booking and running" and "Reproducing and comparing
runs").

A run creates an issue, books capacity, measures cells and posts comments. Each of those can be
cut short: the driver dies, or an answer does not arrive. The journal is what the driver saved
before, so that a restart knows which run this is, what it was about to do, and which cells it has
already measured. It calls nothing: not GitHub, not the pool. The driver does, and tells the
journal before and after.

**Where.** A run is a directory `<root>/<run ID>/`, by default under `.cache/eval/runs/`, which
the repository ignores. It is created for its owner alone (0700, its files 0600). It holds a lock
file, `journal.json` (the state) and one file of observations for each attempt of a cell. Nothing
in it is the public evidence: the journal keeps a booking's payload and a comment's text, which
are not published. What is published is a separate projection, written and checked in the format
of `evaluate.contracts`.

**One owner.** `Journal.create` and `Journal.open` take an exclusive lock on the run for the life
of the handle. A second handle on the same run, from this process or another, is refused with
`Refusal.RUN_ALREADY_OWNED`. So is the handle itself in a process forked from its owner: a child
inherits the lock's descriptor, and the run is still its parent's. The lock is the operating
system's, so it ends with the process that held it: a driver that died leaves nothing to clear.

**Transitions.** The state changes one transition at a time. Each is written whole: to a file
beside the journal, flushed to disk, then renamed over it, and the directory flushed. A reader
finds the state before or the state after, never half of one. A transition whose write failed is
not in the handle, and may or may not be in the file (the rename can have happened before the
failure). The handle does not guess: it gives the run up, every later change through it is
refused (`Refusal.JOURNAL_CLOSED`), and the next `open` reads what the file holds.

**The run ID.** `create` makes it, and saves it before anything else exists. It is the name of the
directory and is repeated in the state. The directory's entry is flushed into the root, and the
entry of each directory `create` had to make into the one above it, before the handle is
returned: a run the driver acts for can be found again. `runs` lists the runs of a root: a
directory whose first write never landed is not one.

**Intents.** Before an action outside the driver (`Action`: the creation of the issue, a booking,
the delivery of a comment), `intend` saves what is about to be sent, with a marker that names the
run. After it, `confirm` saves what came back: the issue's number, the exact booking returned, the
comment. An intent that is still pending after a restart is an action that may or may not have
happened: the driver looks for its marker in the actual record, then confirms it or rules it out
(`rule_out`). One intent of each action is pending at most, so a retry targets the same record.
A booking or a delivery is refused until the issue number is journaled. `adopt_issue` journals the
number of an issue the operator named. What an intent saved does not change afterwards: its
payload and its result are given as copies, and a change to one changes nothing in the journal.

`pending` is the one thing a restart does first, a member of `Step` and the saved intent it is
about: create the issue, reconcile its creation, reconcile a booking, reconcile a delivery, or go
on with the cells. A booking comes before a delivery: publication never delays capacity.

**Cells.** `start_cell` opens an attempt of a cell with the two fingerprints it runs under: the
workload's (`contracts.workload_identity`) and the one of what serves it. `append` adds an
observation to the running attempt: a `RequestRecord`, a `TrialRecord` or a `HostSample`, as its
one canonical line. A file of observations is only ever added to. `checkpoint_cell` ends the
attempt with its `CellRecord`, in one transition, after its observations are flushed to disk: the
checkpoint holds their count and digest, so a cell is saved whole or not at all.

An attempt that was running when its owner died is marked interrupted by the next `open`
(`interrupt` does the same for a driver that gives a cell up), with the count and digest of the
observations it has, flushed to disk first as for a checkpoint. No attempt is ever deleted. A new
attempt of a cell supersedes the earlier ones of that cell, which were interrupted or
checkpointed invalid, and each of them then names it in `replaced_by`. The attempt of a cell that
counts is its last. A cell whose checkpoint is valid for qualification is measured: a new attempt
of it is refused (`Refusal.CELL_ALREADY_MEASURED`), so that no retry puts a result aside.

**A write cut inside a line.** A driver can die halfway through an append, which leaves a last
line with no end. A record is one once its newline is written: `observations` gives the whole
lines before it and leaves the unfinished one out, and the count and digest of an attempt cover
the whole lines only. The file is not repaired, and nothing is added after an unfinished line.

**Resuming.** `resume` takes the plan's cells in order, each with the time a booking keeps for
it, the time the authorized window has for cells, and the fingerprints of the resuming
invocation. A cell whose last attempt is a checkpoint that is valid for qualification is reused
as it is, whatever it measured: a failed trial or a missed floor is a result, and it stays in
what counts. Every other cell (never started, interrupted, or checkpointed invalid) is run again
whole, in order, as long as the next one fits in what is left of the window; the rest wait for
another booking. A reusable checkpoint under another workload or serving fingerprint is not
reused and not silently measured again: the selection is refused
(`Refusal.INCOMPATIBLE_REUSE`), and so is a new attempt in a run that holds such a checkpoint.
The same holds for a reusable checkpoint whose file of observations no longer has the count and
digest it was saved with: its evidence cannot be counted, and the selection is refused
(`Refusal.OBSERVATIONS_DAMAGED`).

A refusal is one member of `Refusal`, a fixed vocabulary. It needs the standard library only, and
Linux (the lock is `flock`).
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from evaluate import contracts
from evaluate.contracts import CellRecord, ContractError, Document, HostSample, RequestRecord, TrialRecord

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SCHEMA_VERSION = 1
# The runs of this checkout, under the scratch directory the repository ignores (`.cache/`).
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / ".cache" / "eval" / "runs"
# What every marker starts with: a record that carries one shows that an evaluation wrote it.
MARKER_PREFIX = "evaluate/"
# What an attempt's file may hold.
OBSERVATIONS = (RequestRecord, TrialRecord, HostSample)

_STATE, _LOCK, _OBSERVATIONS = "journal.json", "lock", "observations"
_PRIVATE_DIRECTORY, _PRIVATE_FILE = 0o700, 0o600
_RUN_ID = re.compile(r"[0-9a-f]{16}")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_CELL = 999_999


class Refusal(StrEnum):
    """Why the journal declined: the whole vocabulary."""

    RUN_ALREADY_OWNED = "run-already-owned"
    NO_SUCH_RUN = "no-such-run"
    JOURNAL_UNREADABLE = "journal-unreadable"
    JOURNAL_CLOSED = "journal-closed"
    INVALID_VALUE = "invalid-value"
    ISSUE_NOT_JOURNALED = "issue-not-journaled"
    ISSUE_ALREADY_JOURNALED = "issue-already-journaled"
    ACTION_PENDING = "action-pending"
    INTENT_NOT_PENDING = "intent-not-pending"
    ATTEMPT_RUNNING = "attempt-running"
    ATTEMPT_NOT_RUNNING = "attempt-not-running"
    CELL_ALREADY_MEASURED = "cell-already-measured"
    NOT_AN_OBSERVATION = "not-an-observation"
    OBSERVATION_UNFINISHED = "observation-unfinished"
    OBSERVATIONS_DAMAGED = "observations-damaged"
    INCOMPATIBLE_REUSE = "incompatible-reuse"


class JournalError(Exception):
    """Something the journal refuses, with its reason."""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Action(StrEnum):
    """What the driver does outside itself, and saves an intent for first."""

    CREATE_ISSUE = "create-issue"
    BOOK = "book"
    DELIVER = "deliver"


class IntentStatus(StrEnum):
    # Saved, and not known to have happened or not.
    PENDING = "pending"
    CONFIRMED = "confirmed"
    # Looked for in the actual record and not there: it did not happen.
    RULED_OUT = "ruled-out"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    # Checkpointed with its cell record. Whether it counts is the record's to say.
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"


class Step(StrEnum):
    """What a restart does first."""

    CREATE_ISSUE = "create-issue"
    RECONCILE_ISSUE = "reconcile-issue"
    RECONCILE_BOOKING = "reconcile-booking"
    RECONCILE_DELIVERY = "reconcile-delivery"
    # Nothing is in doubt: book or use capacity and run what `resume` selects.
    RUN_CELLS = "run-cells"


_RECONCILE = {
    Action.CREATE_ISSUE: Step.RECONCILE_ISSUE,
    Action.BOOK: Step.RECONCILE_BOOKING,
    Action.DELIVER: Step.RECONCILE_DELIVERY,
}


@dataclass(frozen=True)
class Intent:
    """An action outside the driver, saved before it is taken. `payload` is what is about to be
    sent and `result` what came back; both are private. The intent holds each as its JSON text
    and gives a new copy at every read, so nothing a reader does to one reaches the journal."""

    ordinal: int
    action: Action
    marker: str
    status: IntentStatus
    payload_json: str
    result_json: str | None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    @property
    def result(self) -> dict[str, Any] | None:
        return None if self.result_json is None else json.loads(self.result_json)


@dataclass(frozen=True)
class Attempt:
    """One attempt of a cell. `observed` and `digest` are the count and the digest of its whole
    observations, set when it stops running. `record` is the checkpoint of a complete one, and
    `replaced_by` the attempt of the same cell that superseded it."""

    ordinal: int
    cell: int
    workload: str
    served: str
    status: AttemptStatus
    record: CellRecord | None
    observed: int | None
    digest: str | None
    replaced_by: int | None

    @property
    def reusable(self) -> bool:
        """Whether a resume keeps the cell as this attempt measured it: checkpointed, valid for
        qualification, and not superseded. What it measured, a pass or a miss, is not asked."""
        valid = self.record is not None and self.record.valid_for_qualification
        return self.status is AttemptStatus.COMPLETE and valid and self.replaced_by is None


@dataclass(frozen=True)
class Pending:
    """The step a restart does first, and the saved intent it reconciles."""

    step: Step
    intent: Intent | None


@dataclass(frozen=True)
class Resumption:
    """What a resume keeps and runs. `reused` are the checkpoints that count as they are,
    `selected` the cells to run now, whole and in order, `deferred` the cells left for another
    booking."""

    reused: tuple[Attempt, ...]
    selected: tuple[int, ...]
    deferred: tuple[int, ...]


@dataclass(frozen=True)
class _State:
    run_id: str
    issue_number: int | None = None
    intents: tuple[Intent, ...] = ()
    attempts: tuple[Attempt, ...] = ()


# --- The files -------------------------------------------------------------------------------


def _sync(path: Path) -> None:
    """Flushes a file, or a directory's entries, to disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_directory(path: Path) -> None:
    """Makes the directory and the missing ones above it, each flushed into the one that holds
    it."""
    missing: list[Path] = []
    while not path.is_dir():
        missing.append(path)
        path = path.parent
    for one in reversed(missing):
        one.mkdir(exist_ok=True)
        _sync(one.parent)


def _write_whole(path: Path, text: str) -> None:
    """Replaces the file by one that holds `text`, so that a reader finds the old or the new."""
    beside = path.with_name(path.name + ".tmp")
    fd = os.open(beside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(fd)
    os.replace(beside, path)
    _sync(path.parent)


def _append_line(path: Path, line: bytes) -> None:
    """Adds one line at the end of the file, never after a line that has no end."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, _PRIVATE_FILE)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            raise JournalError(Refusal.OBSERVATION_UNFINISHED)
        while line:
            line = line[os.write(fd, line) :]
    finally:
        os.close(fd)


def _whole_lines(path: Path) -> tuple[bytes, bool]:
    """The lines of the file that have their end, and whether an unfinished one follows them."""
    data = path.read_bytes() if path.exists() else b""
    end = data.rfind(b"\n") + 1
    return data[:end], end < len(data)


def _seal(lines: bytes) -> tuple[int, str]:
    return lines.count(b"\n"), "sha256:" + hashlib.sha256(lines).hexdigest()


def _lock(directory: Path) -> int:
    """The run's lock, held by the returned descriptor until it is closed."""
    fd = os.open(directory / _LOCK, os.O_RDWR | os.O_CREAT, _PRIVATE_FILE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise JournalError(Refusal.RUN_ALREADY_OWNED) from None
    return fd


# --- The state, written and read -------------------------------------------------------------


def _encode(state: _State) -> str:
    attempts = [
        {
            **{field.name: getattr(attempt, field.name) for field in dataclasses.fields(attempt)},
            "record": None if attempt.record is None else json.loads(contracts.dumps(attempt.record)),
        }
        for attempt in state.attempts
    ]
    saved = {
        "schema_version": SCHEMA_VERSION,
        "run_id": state.run_id,
        "issue_number": state.issue_number,
        "intents": [
            {
                "ordinal": intent.ordinal,
                "action": intent.action,
                "marker": intent.marker,
                "status": intent.status,
                "payload": intent.payload,
                "result": intent.result,
            }
            for intent in state.intents
        ],
        "attempts": attempts,
    }
    return json.dumps(saved, indent=2, sort_keys=True) + "\n"


def _decode(text: str) -> _State:
    try:
        saved = json.loads(text)
        if saved["schema_version"] != SCHEMA_VERSION:
            raise JournalError(Refusal.JOURNAL_UNREADABLE)
        intents: list[Intent] = []
        for item in saved["intents"]:
            result = None if item["result"] is None else _private(item["result"])
            action, status = Action(item["action"]), IntentStatus(item["status"])
            intents.append(Intent(item["ordinal"], action, item["marker"], status, _private(item["payload"]), result))
        attempts: list[Attempt] = []
        for item in saved["attempts"]:
            record = None if item["record"] is None else contracts.loads(json.dumps(item["record"]), CellRecord)
            read: dict[str, Any] = {**item, "status": AttemptStatus(item["status"]), "record": record}
            attempts.append(Attempt(**read))
        return _State(saved["run_id"], saved["issue_number"], tuple(intents), tuple(attempts))
    except (KeyError, TypeError, ValueError, JournalError) as error:  # a `ContractError` is a `ValueError`
        raise JournalError(Refusal.JOURNAL_UNREADABLE) from error


def _private(value: Mapping[str, object]) -> str:
    """A payload or a result as the journal keeps it: the text of a JSON object."""
    try:
        return json.dumps(dict(value), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise JournalError(Refusal.INVALID_VALUE) from error


def _whole_number(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise JournalError(Refusal.INVALID_VALUE)
    return value


def runs(root: Path = DEFAULT_ROOT) -> tuple[str, ...]:
    """The IDs of the runs journaled under `root`, in order."""
    if not root.is_dir():
        return ()
    found = (one.name for one in root.iterdir() if _RUN_ID.fullmatch(one.name) and (one / _STATE).is_file())
    return tuple(sorted(found))


class Journal:
    """The journal of one run, owned by this handle until `close` (or the end of a `with`)."""

    def __init__(self, directory: Path, lock: int, state: _State) -> None:
        self.directory = directory
        self._lock: int | None = lock
        # The process that took the lock. A forked one inherits the descriptor, not the run.
        self._owner = os.getpid()
        self._state = state

    @classmethod
    def create(cls, root: Path = DEFAULT_ROOT) -> Self:
        """A new run under `root`: its ID is made here and saved before the handle is returned."""
        _make_directory(root)
        run_id = secrets.token_hex(8)
        directory = root / run_id
        # Never an existing directory: an ID is one run's.
        directory.mkdir(mode=_PRIVATE_DIRECTORY)
        (directory / _OBSERVATIONS).mkdir(mode=_PRIVATE_DIRECTORY)
        journal = cls(directory, _lock(directory), _State(run_id))
        try:
            journal._transition()
            # The run is found by its entry in the root: that entry is on disk before the driver
            # is given a run to act for.
            _sync(root)
        except BaseException:
            journal.close()
            raise
        return journal

    @classmethod
    def open(cls, root: Path, run_id: str) -> Self:
        """The run `run_id` under `root`, for the one handle that owns it. An attempt its last
        owner left running is marked interrupted before the handle is returned."""
        directory = root / run_id
        if not _RUN_ID.fullmatch(run_id) or not (directory / _STATE).is_file():
            raise JournalError(Refusal.NO_SUCH_RUN)
        journal = cls(directory, _lock(directory), _State(run_id))
        try:
            journal._state = _decode((directory / _STATE).read_text(encoding="utf-8"))
            if journal._state.run_id != run_id:
                raise JournalError(Refusal.JOURNAL_UNREADABLE)
            for attempt in journal.attempts:
                if attempt.status is AttemptStatus.RUNNING:
                    journal.interrupt(attempt.ordinal)
        except BaseException:
            journal.close()
            raise
        return journal

    def close(self) -> None:
        """Gives the run up. It writes nothing: what is saved is what each transition saved."""
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    # --- What is saved ---

    @property
    def run_id(self) -> str:
        return self._state.run_id

    @property
    def issue_number(self) -> int | None:
        return self._state.issue_number

    @property
    def intents(self) -> tuple[Intent, ...]:
        return self._state.intents

    @property
    def attempts(self) -> tuple[Attempt, ...]:
        """Every attempt of every cell, in the order they started. None is ever removed."""
        return self._state.attempts

    def pending(self) -> Pending:
        """The step a restart does first. A booking in doubt comes before a delivery in doubt."""
        waiting = {intent.action: intent for intent in self.intents if intent.status is IntentStatus.PENDING}
        if self.issue_number is None and not waiting:
            return Pending(Step.CREATE_ISSUE, None)
        for action in Action:
            if action in waiting:
                return Pending(_RECONCILE[action], waiting[action])
        return Pending(Step.RUN_CELLS, None)

    def _own(self) -> None:
        """Refuses a handle that gave the run up, and one in a process forked from its owner."""
        if self._lock is None:
            raise JournalError(Refusal.JOURNAL_CLOSED)
        if os.getpid() != self._owner:
            raise JournalError(Refusal.RUN_ALREADY_OWNED)

    def _transition(self, **changes: Any) -> None:
        """One change of the state, saved whole before the handle shows it. A write that fails
        may or may not have put the change in the file: the handle gives the run up, so that
        nothing more is written from a state the file may no longer hold."""
        self._own()
        after = dataclasses.replace(self._state, **changes)
        text = _encode(after)
        try:
            _write_whole(self.directory / _STATE, text)
        except BaseException:
            self.close()
            raise
        self._state = after

    # --- Intents ---

    def intend(self, action: Action, payload: Mapping[str, object]) -> Intent:
        """Saves that `action` is about to be taken with `payload`, and gives its marker."""
        if any(intent.action is action and intent.status is IntentStatus.PENDING for intent in self.intents):
            raise JournalError(Refusal.ACTION_PENDING)
        if action is Action.CREATE_ISSUE and self.issue_number is not None:
            raise JournalError(Refusal.ISSUE_ALREADY_JOURNALED)
        if action is not Action.CREATE_ISSUE and self.issue_number is None:
            raise JournalError(Refusal.ISSUE_NOT_JOURNALED)
        ordinal = len(self.intents)
        marker = f"{MARKER_PREFIX}{self.run_id}/{ordinal}"
        intent = Intent(ordinal, action, marker, IntentStatus.PENDING, _private(payload), None)
        self._transition(intents=(*self.intents, intent))
        return intent

    def _settle(self, ordinal: int, status: IntentStatus, result: str | None, **changes: Any) -> Intent:
        if type(ordinal) is not int or not 0 <= ordinal < len(self.intents):
            raise JournalError(Refusal.INTENT_NOT_PENDING)
        if self.intents[ordinal].status is not IntentStatus.PENDING:
            raise JournalError(Refusal.INTENT_NOT_PENDING)
        settled = dataclasses.replace(self.intents[ordinal], status=status, result_json=result)
        intents = (*self.intents[:ordinal], settled, *self.intents[ordinal + 1 :])
        self._transition(intents=intents, **changes)
        return settled

    def confirm(self, ordinal: int, result: Mapping[str, object]) -> Intent:
        """Saves that the intent's action happened, and what it returned. The creation of the
        issue returns its `number`, which becomes the run's."""
        saved = _private(result)
        known = type(ordinal) is int and 0 <= ordinal < len(self.intents)
        if known and self.intents[ordinal].action is Action.CREATE_ISSUE:
            number = _whole_number(json.loads(saved).get("number"), 1, contracts.MAX_COUNT)
            return self._settle(ordinal, IntentStatus.CONFIRMED, saved, issue_number=number)
        return self._settle(ordinal, IntentStatus.CONFIRMED, saved)

    def rule_out(self, ordinal: int) -> Intent:
        """Saves that the intent's action did not happen: the actual record does not show it."""
        return self._settle(ordinal, IntentStatus.RULED_OUT, None)

    def adopt_issue(self, number: int) -> None:
        """Journals the number of an issue the operator named: nothing is created for the run."""
        if self.issue_number is not None:
            raise JournalError(Refusal.ISSUE_ALREADY_JOURNALED)
        if self.pending().step is Step.RECONCILE_ISSUE:
            raise JournalError(Refusal.ACTION_PENDING)
        self._transition(issue_number=_whole_number(number, 1, contracts.MAX_COUNT))

    # --- Cells ---

    def _attempt(self, ordinal: int, *, running: bool = False) -> Attempt:
        if type(ordinal) is not int or not 0 <= ordinal < len(self.attempts):
            raise JournalError(Refusal.INVALID_VALUE)
        attempt = self.attempts[ordinal]
        if running and attempt.status is not AttemptStatus.RUNNING:
            raise JournalError(Refusal.ATTEMPT_NOT_RUNNING)
        return attempt

    def _file(self, attempt: Attempt) -> Path:
        return self.directory / _OBSERVATIONS / f"cell-{attempt.cell}-attempt-{attempt.ordinal}.jsonl"

    def _compatible(self, workload: str, served: str) -> None:
        """Refuses fingerprints other than those of a checkpoint a resume would reuse."""
        for attempt in self.attempts:
            if attempt.reusable and (attempt.workload, attempt.served) != (workload, served):
                raise JournalError(Refusal.INCOMPATIBLE_REUSE)

    def _sealed(self, attempt: Attempt) -> tuple[int, str, bool]:
        """The count and the digest of the attempt's whole observations, once they are on disk
        (the file flushed, and its entry in the directory), and whether an unfinished line
        follows them. What a transition then saves about them, a restart finds in the file."""
        file = self._file(attempt)
        if file.exists():
            _sync(file)
            _sync(file.parent)
        lines, unfinished = _whole_lines(file)
        return (*_seal(lines), unfinished)

    def start_cell(self, cell: int, workload: str, served: str) -> Attempt:
        """Opens a new attempt of `cell` under the two fingerprints. Cells run one at a time. The
        earlier attempts of the cell are kept, and name this one as their replacement. A cell
        that has a checkpoint a resume reuses is not attempted again: what it measured stays."""
        _whole_number(cell, 0, _MAX_CELL)
        if not all(isinstance(one, str) and _FINGERPRINT.fullmatch(one) for one in (workload, served)):
            raise JournalError(Refusal.INVALID_VALUE)
        if any(attempt.status is AttemptStatus.RUNNING for attempt in self.attempts):
            raise JournalError(Refusal.ATTEMPT_RUNNING)
        if any(attempt.cell == cell and attempt.reusable for attempt in self.attempts):
            raise JournalError(Refusal.CELL_ALREADY_MEASURED)
        self._compatible(workload, served)
        new = Attempt(len(self.attempts), cell, workload, served, AttemptStatus.RUNNING, None, None, None, None)
        earlier = (
            dataclasses.replace(one, replaced_by=new.ordinal) if one.cell == cell and one.replaced_by is None else one
            for one in self.attempts
        )
        self._transition(attempts=(*earlier, new))
        return new

    def _stop(self, attempt: Attempt, **changes: Any) -> Attempt:
        stopped = dataclasses.replace(attempt, **changes)
        attempts = (*self.attempts[: attempt.ordinal], stopped, *self.attempts[attempt.ordinal + 1 :])
        self._transition(attempts=attempts)
        return stopped

    def append(self, ordinal: int, observation: Document) -> None:
        """Adds an observation to a running attempt, as one line at the end of its file."""
        self._own()
        attempt = self._attempt(ordinal, running=True)
        # A request and a host sample say their cell; a trial's is the plan's to say.
        if type(observation) not in OBSERVATIONS or getattr(observation, "cell", attempt.cell) != attempt.cell:
            raise JournalError(Refusal.NOT_AN_OBSERVATION)
        _append_line(self._file(attempt), contracts.dumps(observation).encode("utf-8") + b"\n")

    def checkpoint_cell(self, ordinal: int, record: CellRecord) -> Attempt:
        """Ends a running attempt with its cell record: the cell is saved whole, or not at all."""
        self._own()
        attempt = self._attempt(ordinal, running=True)
        if type(record) is not CellRecord or record.cell != attempt.cell:
            raise JournalError(Refusal.INVALID_VALUE)
        observed, digest, unfinished = self._sealed(attempt)
        if unfinished:
            raise JournalError(Refusal.OBSERVATION_UNFINISHED)
        return self._stop(attempt, status=AttemptStatus.COMPLETE, record=record, observed=observed, digest=digest)

    def interrupt(self, ordinal: int) -> Attempt:
        """Ends a running attempt that will have no checkpoint. Its whole observations stay."""
        self._own()
        attempt = self._attempt(ordinal, running=True)
        observed, digest, _unfinished = self._sealed(attempt)
        return self._stop(attempt, status=AttemptStatus.INTERRUPTED, observed=observed, digest=digest)

    def observations(self, ordinal: int) -> tuple[Document, ...]:
        """The observations of an attempt, in the order they were added: the whole lines of its
        file. A last line a write left unfinished is not one. For an attempt that stopped, they
        are the ones it stopped with, or the file is refused as damaged."""
        attempt = self._attempt(ordinal)
        lines, _unfinished = _whole_lines(self._file(attempt))
        if attempt.status is not AttemptStatus.RUNNING and _seal(lines) != (attempt.observed, attempt.digest):
            raise JournalError(Refusal.OBSERVATIONS_DAMAGED)
        try:
            return tuple(contracts.parse(line) for line in lines.splitlines())
        except ContractError as error:
            raise JournalError(Refusal.OBSERVATIONS_DAMAGED) from error

    # --- Resuming ---

    def resume(self, cells: Sequence[tuple[int, int]], window_ms: int, workload: str, served: str) -> Resumption:
        """What a resume under these fingerprints keeps and runs. `cells` are the plan's, in
        order, each with the time a booking keeps for it; `window_ms` is the time the authorized
        window has for cells. It changes nothing. A checkpoint is reused with its evidence: one
        whose observations are not the ones it was saved with refuses the selection."""
        self._compatible(workload, served)
        last = {attempt.cell: attempt for attempt in self.attempts}
        reused: list[Attempt] = []
        selected: list[int] = []
        deferred: list[int] = []
        left = window_ms
        for cell, reserved_ms in cells:
            attempt = last.get(cell)
            if attempt is not None and attempt.reusable:
                self.observations(attempt.ordinal)
                reused.append(attempt)
            elif not deferred and reserved_ms <= left:
                selected.append(cell)
                left -= reserved_ms
            else:
                deferred.append(cell)
        return Resumption(tuple(reused), tuple(selected), tuple(deferred))
