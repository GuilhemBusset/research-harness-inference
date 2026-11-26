"""The capacity of an evaluation (tools/evaluate/booking.py): a managed booking that is written
once, under its own marker, waited for within its own window and stopped only while the record
still is that booking; and a designated session, which is read and never written.

moto holds the pool-state table in process, and `Deployed` stands for the rest of the deployed
pool: the scaler, the gateway probe, the hosts' releases, and a clock that only a sleep moves. No
account, no credentials, no network.
"""

from __future__ import annotations

import dataclasses
import io
import json
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from moto import mock_aws

import booking as pool_booking
from evaluate import booking as evaluation
from evaluate.booking import Cleanup, Reason
from rhi_admin import pools as admin_pools
from rhi_admin import session as admin_session
from rhi_common import sessions as rules
from rhi_common.records import SESSION_RECORD, ScalerNote, Session
from rhi_common.tables import POOL_STATE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

NOW = 1_800_000_000
HOURS = 12
POOL = "coursework"
# The pool's deployed `max_instances`, in the scaler's settings.
MOST = 2
RELEASE, OTHER_RELEASE = "image-a/revision-1", "image-b/revision-1"
# A booking of 30 minutes for a batch of 15 and a cleanup of 5: the batch may start in the first 10.
MINUTES, BATCH, CLEANUP = 30, 900, 300
END = NOW + MINUTES * 60
LAST_START = END - BATCH - CLEANUP
OPERATOR = "user/operator"

ENTRY = {
    "engine": "mock",
    "instance_types": ["g6e.xlarge"],
    "prices": {"g6e.xlarge": 1.86},
    "max_instances": MOST,
    "warm_instances": 1,
    "wake": "scheduled-only",
    "target_concurrency": 8,
    "context_length": 32_768,
    "largest_output": 4_096,
}
LOST = ReadTimeoutError(endpoint_url="https://dynamodb.example.com")


def _refused() -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


class Deployed:
    """The deployed pool as the driver reaches it: the table, Lambda, the probes and the clock.

    The scaler writes its note when it is asked for a run and at every tick of the clock, while
    `scaler_runs`: `hosts` in service, the rest of what a session asks for waiting. A write of the
    driver goes through `put_item`, which keeps it in `puts` and does what `faults` says of it:
    `lost` stores it and loses the answer, `retried` stores it and answers as the SDK's own retry
    of a stored write is answered, `failed` loses it before it is stored. `before_put` and
    `after_put` run once, around the next write. `at` holds what happens at a time of the clock.
    """

    def __init__(self, table: Any) -> None:
        self.table, self.now, self.lag = table, NOW, 0
        self.hosts, self.scaler_runs, self.run_fails, self.runs = 0, True, False, 0
        self.gateway = True
        self.served: list[str] | None = None
        self.puts: list[Session] = []
        self.faults: list[str] = []
        self.before_put: Callable[[], None] | None = None
        self.after_put: Callable[[], None] | None = None
        self.at: list[tuple[int, Callable[[], None]]] = []
        self.sleeps: list[float] = []

    # The clock.
    def clock(self) -> float:
        return float(self.now)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += int(seconds) + self.lag
        due = [event for event in self.at if event[0] <= self.now]
        self.at = [event for event in self.at if event[0] > self.now]
        for _, happens in due:
            happens()
        self.note()

    def hosts_at(self, at: int, hosts: int) -> None:
        """From the time `at` of the clock, that many hosts are in service."""

        def arrive() -> None:
            self.hosts = hosts

        self.at.append((at, arrive))

    # The scaler.
    def note(self, *, written_at: int | None = None) -> None:
        if written_at is None and not self.scaler_runs:
            return
        session = stored(self)
        asked = rules.requested(session, self.now, max_instances=MOST, session_max_hours=HOURS)
        note = ScalerNote(
            pool=POOL,
            capacity_asked=asked,
            no_capacity_at=None,
            written_at=self.now if written_at is None else written_at,
            requested=asked,
            in_service=self.hosts,
            waiting=max(0, asked - self.hosts),
        )
        self.table.put_item(Item=note.to_item())

    def get_function_configuration(self, **_: str) -> dict[str, Any]:
        settings = json.dumps({"pools": {POOL: {"entry": ENTRY}}})
        return {"Environment": {"Variables": {"RHI_SCALER_SETTINGS": settings}}}

    def invoke(self, **_: object) -> dict[str, Any]:
        self.runs += 1
        if self.run_fails:
            return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(b"{}")}
        self.note()
        return {"StatusCode": 200, "Payload": io.BytesIO(b'{"pools": []}')}

    # The probes.
    def gateway_ready(self) -> bool:
        return self.gateway

    def releases(self) -> list[str]:
        return [RELEASE] * self.hosts if self.served is None else self.served

    # The table.
    def get_item(self, **request: Any) -> dict[str, Any]:
        return self.table.get_item(**request)

    def put_item(self, **request: Any) -> dict[str, Any]:
        self.puts.append(Session.from_item(request["Item"]))
        before, self.before_put = self.before_put, None
        if before is not None:
            before()
        fault = self.faults.pop(0) if self.faults else None
        if fault == "failed":
            raise LOST
        answer = self.table.put_item(**request)
        after, self.after_put = self.after_put, None
        if after is not None:
            after()
        if fault == "lost":
            raise LOST
        if fault == "retried":
            raise _refused()
        return answer


@pytest.fixture
def pool() -> Iterator[Deployed]:
    """The deployed pool over the pool-state table, in moto, with no item."""
    with mock_aws():
        keys = [(POOL_STATE.partition_key, "HASH"), (POOL_STATE.sort_key, "RANGE")]
        yield Deployed(
            boto3.resource("dynamodb", region_name="us-east-1").create_table(
                TableName="pool-state-under-test",
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key is not None
                ],
                KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys if key is not None],
            )
        )


def stored(pool: Deployed) -> Session | None:
    item = pool.table.get_item(Key={"pool": POOL, "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


def held(pool: Deployed) -> Session:
    session = stored(pool)
    assert session is not None
    return session


def capacity(pool: Deployed, journal: Path, *, instances: int = 2, name: str = POOL) -> evaluation.Capacity:
    """The capacity of an invocation over the journal file, as a new process would build it."""
    pools = admin_pools.Pools(table=pool, functions=pool, scaler="scaler-under-test", caller=lambda: "user/driver")
    sessions = admin_session.Sessions(pools=pools, time_zone="UTC", session_max_hours=HOURS, default_pool=None)
    access = evaluation.Access(sessions, pool.gateway_ready, pool.releases, clock=pool.clock, sleep=pool.sleep)
    selection = evaluation.Selection(name, instances, RELEASE, BATCH, CLEANUP)
    return evaluation.Capacity(selection, access, evaluation.Journal.load(journal))


def write(pool: Deployed, session: Session) -> Callable[[], None]:
    """Someone else's write of the pool's session, past the driver."""
    return lambda: pool.table.put_item(Item=session.to_item())


def edit(pool: Deployed, **changes: Any) -> Callable[[], None]:
    """An edit of the session as it is stored then."""
    return lambda: write(pool, dataclasses.replace(held(pool), **changes))()


def stopped_under_the_saved_marker(pool: Deployed, journal: evaluation.Journal) -> bool:
    """Whether the record is this invocation's stop: written under the marker saved before the
    booking, ended no later than the deadline saved with it, and no longer booked."""
    record = held(pool)
    assert journal.marker is not None and journal.deadline is not None
    return (
        record in journal.stops
        and record.set_by == journal.marker
        and record.ends_at <= journal.deadline
        and not pool_booking.booked(pool.table, POOL, now=pool.now, session_max_hours=HOURS)
    )


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    return tmp_path / "eval" / "booking.json"


# A managed booking.


def test_a_managed_booking_is_saved_exactly_under_its_own_marker_and_stopped_by_its_release(
    pool: Deployed, journal: Path, tmp_path: Path
) -> None:
    pool.hosts = 2
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert outcome.ready and outcome.cleanup is Cleanup.HELD
    booked = held(pool)
    assert booked == Session(POOL, 2, NOW, END, run.journal.marker or "", NOW)
    # The exact record is what was returned and what the journal file holds, before and after the write.
    saved = evaluation.Journal.load(journal)
    assert outcome.session == booked == saved.booking == saved.intended
    assert saved.marker == booked.set_by and saved.deadline == END
    # One write, then one run of the scaler; no sleep, the hosts being there.
    assert pool.puts == [booked] and pool.runs == 1 and pool.sleeps == []

    assert run.release() is Cleanup.RELEASED
    assert stopped_under_the_saved_marker(pool, run.journal)
    assert len(pool.puts) == 2 and pool.runs == 2
    assert evaluation.Journal.load(journal).cleanup is Cleanup.RELEASED

    # Another invocation writes under another marker.
    other = capacity(pool, tmp_path / "other.json")
    assert other.acquire(minutes=MINUTES).ready
    assert other.journal.marker not in (None, run.journal.marker)


@pytest.mark.parametrize(
    ("instances", "minutes", "name", "reason"),
    [
        (MOST + 1, MINUTES, POOL, Reason.OUTSIDE_POOL_LIMITS),
        (2, HOURS * 60 + 1, POOL, Reason.OUTSIDE_POOL_LIMITS),
        (2, MINUTES, "elsewhere", Reason.POOL_NOT_DEPLOYED),
        (2, (BATCH + CLEANUP) // 60 - 1, POOL, Reason.WINDOW_TOO_SHORT),
    ],
    ids=["more hosts than max_instances", "longer than session_max_hours", "no such pool", "shorter than the batch"],
)
def test_a_booking_outside_the_deployed_limits_or_too_short_for_the_batch_is_refused_unwritten(
    pool: Deployed, journal: Path, instances: int, minutes: int, name: str, reason: Reason
) -> None:
    outcome = capacity(pool, journal, instances=instances, name=name).acquire(minutes=minutes)

    assert (outcome.reason, outcome.cleanup, outcome.session) == (reason, Cleanup.NOT_OWNED, None)
    assert pool.puts == [] and stored(pool) is None and pool.runs == 0


def test_a_scaler_run_that_fails_does_not_undo_the_booking_or_its_release(pool: Deployed, journal: Path) -> None:
    pool.run_fails = True
    pool.hosts_at(NOW + 30, 2)
    run = capacity(pool, journal)

    # The scaler's own next run brings the hosts.
    assert run.acquire(minutes=MINUTES).ready and pool.now == NOW + 30
    assert run.release() is Cleanup.RELEASED and pool.runs == 2


# What a wait requires.


@pytest.mark.parametrize(
    ("hosts", "served", "gateway", "reason"),
    [
        (1, None, True, Reason.CAPACITY_UNAVAILABLE),
        (2, [RELEASE], True, Reason.RELEASE_MISMATCH),
        (2, [RELEASE, OTHER_RELEASE], True, Reason.RELEASE_MISMATCH),
        (2, None, False, Reason.GATEWAY_NOT_READY),
    ],
    ids=["one of two in service", "one of two reports a release", "a host serves another release", "gateway"],
)
def test_a_wait_that_is_never_ready_ends_at_the_last_start_and_releases_the_booking(
    pool: Deployed, journal: Path, hosts: int, served: list[str] | None, gateway: bool, reason: Reason
) -> None:
    pool.hosts, pool.served, pool.gateway = hosts, served, gateway
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert (outcome.reason, outcome.cleanup, outcome.session) == (reason, Cleanup.RELEASED, None)
    # Polled at the interval, up to the last moment the batch could start by the saved deadline.
    assert run.journal.deadline == END and pool.now == LAST_START
    assert pool.sleeps == [evaluation.POLL_SECONDS] * ((LAST_START - NOW) // evaluation.POLL_SECONDS)
    assert stopped_under_the_saved_marker(pool, run.journal) and pool.runs == 2


@pytest.mark.parametrize("designated", [False, True], ids=["managed", "designated"])
def test_more_hosts_in_service_than_selected_is_not_ready(pool: Deployed, journal: Path, designated: bool) -> None:
    pool.hosts = 2
    if designated:
        write(pool, Session(POOL, 1, NOW - 600, END, OPERATOR, NOW - 600))()
        pool.note()

    outcome = capacity(pool, journal, instances=1).acquire(minutes=None if designated else MINUTES)

    assert outcome.reason is Reason.CAPACITY_UNAVAILABLE and pool.now == LAST_START


def test_hosts_counted_before_the_booking_by_a_scaler_that_no_longer_runs_are_not_counted(
    pool: Deployed, journal: Path
) -> None:
    # Two hosts in service, by a note written a moment before the booking; no run of the scaler since.
    pool.hosts = 2
    pool.note(written_at=NOW - 1)
    pool.scaler_runs = False
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert (outcome.reason, outcome.cleanup) == (Reason.SUPPLY_STALE, Cleanup.RELEASED)
    assert pool.now == LAST_START and stopped_under_the_saved_marker(pool, run.journal)


def test_capacity_that_is_delayed_past_the_last_start_is_not_used_and_the_booking_is_released(
    pool: Deployed, journal: Path
) -> None:
    # The hosts are asked for and waiting; they come a second too late. Each sleep overruns, as a
    # real one does, so a poll falls after the last start.
    pool.lag = 7
    pool.hosts_at(LAST_START + 1, 2)
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert (outcome.reason, outcome.cleanup, outcome.session) == (Reason.CAPACITY_UNAVAILABLE, Cleanup.RELEASED, None)
    assert pool.hosts == 2 and LAST_START < pool.now < LAST_START + evaluation.POLL_SECONDS
    assert stopped_under_the_saved_marker(pool, run.journal)


def _cancel() -> None:
    raise KeyboardInterrupt


def test_a_cancelled_wait_stops_the_booking_it_made(pool: Deployed, journal: Path) -> None:
    pool.at = [(NOW + 60, _cancel)]
    run = capacity(pool, journal)

    with pytest.raises(KeyboardInterrupt):
        run.acquire(minutes=MINUTES)

    assert held(pool).ends_at == NOW + 60 and stopped_under_the_saved_marker(pool, run.journal)
    assert evaluation.Journal.load(journal).cleanup is Cleanup.RELEASED


# No takeover.


@pytest.mark.parametrize("meanwhile", [False, True], ids=["booked before", "booked between the read and the write"])
def test_a_pool_someone_else_booked_is_left_as_it_is(pool: Deployed, journal: Path, meanwhile: bool) -> None:
    theirs = Session(POOL, 1, NOW, NOW + 3600, OPERATOR, NOW)
    if meanwhile:
        pool.before_put = write(pool, theirs)
    else:
        write(pool, theirs)()
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert (outcome.reason, outcome.cleanup) == (Reason.POOL_ALREADY_BOOKED, Cleanup.NOT_OWNED)
    assert run.journal.booking is None and pool.runs == 0 and pool.sleeps == []
    assert run.release() is Cleanup.NOT_OWNED
    # At most the one refused write of the lost race: the record is theirs, as they wrote it.
    assert len(pool.puts) == int(meanwhile) and stored(pool) == theirs


EDITS = {
    "another number of hosts": {"instances": 1, "set_by": OPERATOR, "set_at": NOW + 60},
    "a later end": {"ends_at": END + 3600, "set_by": OPERATOR, "set_at": NOW + 60},
    "stopped": {"ends_at": NOW + 60, "set_by": OPERATOR, "set_at": NOW + 60},
    # What `booking.changed` of the pipeline writes: the marker and the end are kept.
    "the same marker and end": {"instances": 1, "set_at": NOW + 60},
}


@pytest.mark.parametrize("changes", EDITS.values(), ids=EDITS)
def test_a_booking_edited_during_the_wait_is_the_editor_s_from_then_on(
    pool: Deployed, journal: Path, changes: dict[str, Any]
) -> None:
    pool.at = [(NOW + 60, edit(pool, **changes))]
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert (outcome.reason, outcome.cleanup) == (Reason.BOOKING_CHANGED, Cleanup.YIELDED)
    booked = run.journal.booking
    assert booked is not None and pool.now == NOW + 60
    assert run.release() is Cleanup.YIELDED
    # The booking was this invocation's only write: no stop, and no second booking.
    assert pool.puts == [booked] and stored(pool) == dataclasses.replace(booked, **changes) and pool.runs == 1


def test_an_edit_between_the_stop_s_read_and_its_write_is_not_written_over(pool: Deployed, journal: Path) -> None:
    pool.hosts = 2
    run = capacity(pool, journal)
    booked = run.acquire(minutes=MINUTES).session
    assert booked is not None
    pool.now += BATCH
    pool.before_put = edit(pool, ends_at=END + 3600, set_by=OPERATOR, set_at=pool.now)

    assert run.release() is Cleanup.YIELDED
    assert stored(pool) == dataclasses.replace(booked, ends_at=END + 3600, set_by=OPERATOR, set_at=NOW + BATCH)
    assert pool.runs == 1


# A designated session.

DESIGNATED = Session(POOL, 2, NOW - 3600, END, OPERATOR, NOW - 7200)


@pytest.mark.parametrize(
    ("session", "hosts", "reason"),
    [
        (DESIGNATED, 2, None),
        (DESIGNATED, 1, Reason.CAPACITY_UNAVAILABLE),
        (dataclasses.replace(DESIGNATED, instances=1), 1, Reason.SESSION_COUNT_MISMATCH),
        (dataclasses.replace(DESIGNATED, ends_at=NOW + BATCH + CLEANUP - 1), 2, Reason.WINDOW_TOO_SHORT),
        (dataclasses.replace(DESIGNATED, ends_at=NOW), 2, Reason.SESSION_NOT_FOUND),
        (None, 2, Reason.SESSION_NOT_FOUND),
    ],
    ids=["ready", "one of two in service", "another count", "too little time left", "ended", "no session"],
)
def test_a_designated_session_is_checked_and_neither_stopped_nor_extended(
    pool: Deployed, journal: Path, session: Session | None, hosts: int, reason: Reason | None
) -> None:
    if session is not None:
        write(pool, session)()
    pool.hosts = hosts
    pool.note()
    run = capacity(pool, journal)

    outcome = run.acquire()

    assert (outcome.reason, outcome.cleanup) == (reason, Cleanup.NOT_OWNED)
    assert outcome.session == (session if reason is None else None)
    assert run.release() is Cleanup.NOT_OWNED and run.reconcile() is Cleanup.NOT_OWNED
    # Whatever came of it: no write, the session as the operator wrote it, and no run asked of the scaler.
    assert pool.puts == [] and stored(pool) == session and pool.runs == 0
    assert run.journal.marker is None


def test_a_cancelled_wait_leaves_a_designated_session_as_it_is(pool: Deployed, journal: Path) -> None:
    write(pool, DESIGNATED)()
    pool.note()
    pool.at = [(NOW + 60, _cancel)]

    with pytest.raises(KeyboardInterrupt):
        capacity(pool, journal).acquire()

    assert pool.puts == [] and stored(pool) == DESIGNATED and pool.runs == 0


@pytest.mark.parametrize(("age", "ready"), [(evaluation.SUPPLY_FRESH_SECONDS, True), (301, False)])
def test_hosts_are_counted_only_from_a_note_of_the_scaler_that_is_fresh(
    pool: Deployed, journal: Path, age: int, ready: bool
) -> None:
    write(pool, DESIGNATED)()
    pool.hosts = 2
    pool.note(written_at=NOW - age)
    pool.scaler_runs = False

    outcome = capacity(pool, journal).acquire()

    assert outcome.reason is (None if ready else Reason.SUPPLY_STALE)
    assert pool.now == (NOW if ready else LAST_START)


# Ambiguous writes.


@pytest.mark.parametrize("fault", ["lost", "retried"])
def test_a_booking_stored_with_its_answer_lost_is_adopted_by_the_whole_saved_record(
    pool: Deployed, journal: Path, fault: str
) -> None:
    pool.hosts, pool.faults = 2, [fault]
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert outcome.ready and outcome.cleanup is Cleanup.HELD
    saved = evaluation.Journal.load(journal)
    assert held(pool) == outcome.session == saved.booking == saved.intended
    assert (held(pool).set_by, held(pool).ends_at) == (saved.marker, saved.deadline)
    # Written once and not again, and the scaler asked as after any booking.
    assert len(pool.puts) == 1 and pool.runs == 1
    assert run.release() is Cleanup.RELEASED and stopped_under_the_saved_marker(pool, run.journal)


def test_a_booking_whose_answer_was_lost_is_not_adopted_once_it_no_longer_reads_whole(
    pool: Deployed, journal: Path
) -> None:
    # Stored, the answer lost, and changed before it is read again: the marker and the end still match.
    pool.faults, pool.after_put = ["lost"], edit(pool, instances=1)
    run = capacity(pool, journal, instances=2)

    outcome = run.acquire(minutes=MINUTES)

    assert (outcome.reason, outcome.cleanup) == (Reason.POOL_ALREADY_BOOKED, Cleanup.NOT_OWNED)
    changed = held(pool)
    assert (changed.set_by, changed.ends_at, changed.instances) == (run.journal.marker, run.journal.deadline, 1)
    assert run.journal.booking is None and run.release() is Cleanup.NOT_OWNED
    assert len(pool.puts) == 1 and stored(pool) == changed and pool.runs == 0


@pytest.mark.parametrize("taken", [False, True], ids=["nothing stored", "someone else's stored"])
def test_a_booking_that_failed_adopts_nothing(pool: Deployed, journal: Path, taken: bool) -> None:
    theirs = Session(POOL, 2, NOW, END, OPERATOR, NOW)
    pool.faults = ["failed"]
    if taken:
        pool.before_put = write(pool, theirs)
    run = capacity(pool, journal)

    outcome = run.acquire(minutes=MINUTES)

    assert outcome.reason is (Reason.POOL_ALREADY_BOOKED if taken else Reason.BOOKING_UNCONFIRMED)
    assert outcome.cleanup is Cleanup.NOT_OWNED and run.journal.booking is None
    assert stored(pool) == (theirs if taken else None) and pool.runs == 0


@pytest.mark.parametrize(
    ("faults", "writes"),
    [(["lost"], 1), (["retried"], 1), (["failed"], 2), (["failed", "lost"], 2)],
    ids=["answer lost", "answered as a retry", "failed once", "failed, then answer lost"],
)
def test_an_owned_stop_is_retried_against_the_saved_record(
    pool: Deployed, journal: Path, faults: list[str], writes: int
) -> None:
    pool.hosts = 2
    run = capacity(pool, journal)
    booked = run.acquire(minutes=MINUTES).session
    pool.now += BATCH
    pool.puts, pool.faults = [], list(faults)

    assert run.release() is Cleanup.RELEASED

    # The retry read its own stop, or the booking unchanged: stopped once, at the time of the write stored.
    stop = held(pool)
    assert stopped_under_the_saved_marker(pool, run.journal) and NOW + BATCH <= stop.set_at <= pool.now
    assert booked is not None and stop == dataclasses.replace(booked, ends_at=stop.set_at, set_at=stop.set_at)
    assert len(pool.puts) == writes and pool.runs == 2
    assert run.release() is Cleanup.RELEASED and len(pool.puts) == writes


def test_a_stop_that_keeps_failing_is_bounded_and_is_not_retried_past_the_saved_deadline(
    pool: Deployed, journal: Path
) -> None:
    pool.hosts = 2
    run = capacity(pool, journal)
    booked = run.acquire(minutes=MINUTES).session
    pool.puts, pool.faults = [], ["failed"] * 10

    assert run.release() is Cleanup.UNCONFIRMED
    assert len(pool.puts) == evaluation.STOP_ATTEMPTS and stored(pool) == booked

    # Its own end, saved before it was written, has bounded it: there is nothing left to stop.
    assert run.journal.deadline is not None
    pool.now = run.journal.deadline
    assert run.release() is Cleanup.ENDED
    assert len(pool.puts) == evaluation.STOP_ATTEMPTS and stored(pool) == booked


# Restart.


def restarted(pool: Deployed, journal: Path, *, answered: bool) -> evaluation.Capacity:
    """A new process over the journal of one that booked and died: after the booking's answer, or
    with the booking stored and only the intent of it saved."""
    pool.hosts = 2
    assert capacity(pool, journal).acquire(minutes=MINUTES).ready
    if not answered:
        saved = evaluation.Journal.load(journal)
        saved.booking = None
        saved.save()
    pool.now += 120
    pool.puts = []
    return capacity(pool, journal)


@pytest.mark.parametrize("answered", [True, False], ids=["booking saved", "only the intent saved"])
def test_a_restart_finds_its_unchanged_booking_and_resumes_or_releases_it(
    pool: Deployed, journal: Path, answered: bool
) -> None:
    run = restarted(pool, journal, answered=answered)

    assert run.reconcile() is Cleanup.HELD and pool.puts == []
    assert run.journal.booking == run.journal.intended == stored(pool)
    assert run.wait().ready
    assert run.release() is Cleanup.RELEASED and stopped_under_the_saved_marker(pool, run.journal)
    assert len(pool.puts) == 1


@pytest.mark.parametrize("answered", [True, False], ids=["booking saved", "only the intent saved"])
def test_a_restart_leaves_a_record_that_is_no_longer_its_booking(pool: Deployed, journal: Path, answered: bool) -> None:
    run = restarted(pool, journal, answered=answered)
    # Changed while no driver ran, as `booking.changed` changes it: the marker and the end are kept.
    edit(pool, instances=1)()
    changed = stored(pool)

    assert run.reconcile() is (Cleanup.YIELDED if answered else Cleanup.NOT_OWNED)
    assert run.release() is run.reconcile()
    assert pool.puts == [] and stored(pool) == changed


def test_a_restart_after_the_saved_deadline_has_nothing_to_stop(pool: Deployed, journal: Path) -> None:
    run = restarted(pool, journal, answered=True)
    pool.now = END

    assert run.reconcile() is Cleanup.ENDED and run.release() is Cleanup.ENDED
    assert pool.puts == []
