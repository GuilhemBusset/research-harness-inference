"""Books a pool for an evaluation, waits for its hosts, and releases what it booked
(docs/design/agent-session-evaluation.md, "Booking and running", steps 2 to 4).

An evaluation measures a chosen number of hosts of an already-deployed pool. This module gets the
operator-side driver that capacity through the pool's session, the one operating state of a pool
(system design, section 5.6), and gives it back. It launches and terminates nothing: the deployed
scaler does, from the session it reads. It publishes nothing either.

**Two modes.** `Capacity.acquire(minutes=...)` is a *managed* booking: this invocation books the
pool for those minutes and owns that booking. `Capacity.acquire()` is a *designated existing*
session: the operator booked the pool and keeps its lifetime, and this module only reads it. In
that mode it writes nothing to the table and asks the scaler for nothing, whatever happens.

**Booking.** The bounds are the deployed ones: the pool's `max_instances` from the scaler's
function (`rhi_admin.pools.max_instances`) and `session_max_hours` from the configuration
(`rhi_admin.session.Sessions`). The write is the pipeline's conditional one (`booking.book` of
`scripts/pipeline/booking.py`), under a writer marker made for this invocation and no other. The
whole session it is about to write is saved in the journal before the write, and the session the
write returned after it: that exact record is the booking. After the write it asks the scaler for
a run (`rhi_admin.session.applied`), as it does after its stop.

**Waiting.** `wait` polls every `POLL_SECONDS` and says ready only when one poll finds all of:

- the scaler's note (`rhi_admin.session.supply`) no older than `SUPPLY_FRESH_SECONDS`, not written
  before the session was, and counting *exactly* the selected hosts in service: one of two is not
  enough, and two where one was selected would spread a one-host measurement over both;
- the gateway ready, by the driver's probe;
- exactly the selected number of hosts reporting a release, each the expected one;
- the session's end at least the batch and the cleanup away.

The last moment a batch may start is the session's end less the batch and the cleanup. No poll
starts after it, so the wait is bounded by the booking itself; capacity that turns up later is not
used.

**Reasons.** Whatever is declined carries one member of `Reason`, a fixed vocabulary: never free
text, never text of AWS. A wait that runs out gives the reason of its last poll.

**Releasing.** `release` is the pipeline's conditional stop (`booking.stop`) of the exact booking
this invocation made, and of nothing else. It runs when a wait is refused or interrupted, and when
the driver calls it after its batch. What it finds is one member of `Cleanup`. A record that is no
longer that booking, in any attribute, is someone else's from then on: an operator who changed its
instances, moved its end, stopped it or booked again owns it, and `release` writes nothing.

**Ambiguous writes.** A write whose answer does not arrive may have been stored, and the SDK's own
retry of a stored write fails its condition, so `None` from `book` or `stop` does not say the
write was lost either. In both cases the record is read again and compared, whole, with what the
journal saved before the write: the intended booking, or one of the intended stops. Only a full
match is this invocation's. The marker and the end alone are not enough, since a change of the
number of instances keeps both.

**Restart.** A driver that died leaves the journal and, at worst, a booking that its recorded end
bounds. A new `Capacity` over the same journal file starts from `reconcile`, which reads the
record and says whether the booking is still held, was released, has ended, or is no longer this
invocation's, before the driver resumes with `wait` or cleans up with `release`.

The journal is a local file of the driver. It holds the pool's name and the booking's times, which
are not published (the design, "Where results persist"): keep it out of the repository.
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

import booking as pool_booking
from rhi_admin import pools as admin_pools
from rhi_admin import session as admin_session
from rhi_admin.roster import AdminError
from rhi_common import sessions as rules
from rhi_common.records import RecordError, Session

if TYPE_CHECKING:
    from pathlib import Path

MINUTE_SECONDS = 60
# How long a wait sleeps between two polls.
POLL_SECONDS = 15
# The oldest note of the scaler a poll still counts. The scaler runs every minute: a note older
# than two runs says the scaler is not running, and counts hosts as they were.
SUPPLY_FRESH_SECONDS = 120
# How many times one `release` tries the stop, and the seconds between two tries.
STOP_ATTEMPTS = 3
RETRY_SECONDS = 5
# What every writer marker starts with: the record shows that an evaluation wrote it.
MARKER_PREFIX = "evaluate/"

# An error of AWS that does not say whether a write was stored.
_AWS = (ClientError, BotoCoreError)


class Reason(enum.StrEnum):
    """Why capacity was declined: the whole vocabulary."""

    # Another session is booked or active on the pool, or won the race for it.
    POOL_ALREADY_BOOKED = "pool-already-booked"
    # The selected hosts were not in service in time for the batch and the cleanup.
    CAPACITY_UNAVAILABLE = "capacity-unavailable"
    POOL_NOT_DEPLOYED = "pool-not-deployed"
    # The selected hosts or the minutes are outside the deployed `max_instances` and `session_max_hours`.
    OUTSIDE_POOL_LIMITS = "outside-pool-limits"
    # The session ends before the batch and the cleanup would.
    WINDOW_TOO_SHORT = "window-too-short"
    # A designated session: the pool has none whose end has not come.
    SESSION_NOT_FOUND = "session-not-found"
    # A designated session: it books another number of hosts than the selected one.
    SESSION_COUNT_MISMATCH = "session-count-mismatch"
    # The booking's write failed and the record does not show it, or could not be read.
    BOOKING_UNCONFIRMED = "booking-unconfirmed"
    # The record is no longer the booking this invocation made.
    BOOKING_CHANGED = "booking-changed"
    # The scaler's note is too old, or older than the session, to count hosts from.
    SUPPLY_STALE = "supply-stale"
    GATEWAY_NOT_READY = "gateway-not-ready"
    # A host serves another release than the expected one, or too few or too many report one.
    RELEASE_MISMATCH = "release-mismatch"


class Mode(enum.StrEnum):
    MANAGED = "managed"
    EXISTING = "existing"


class Cleanup(enum.StrEnum):
    """What became of the booking this invocation made."""

    # The record still is the booking, and its end has not come.
    HELD = "held"
    # The record is this invocation's stop of the booking.
    RELEASED = "released"
    # The record is no longer the booking: whoever wrote it owns it, and nothing was written.
    YIELDED = "yielded"
    # The booking's own end came first: there was nothing left to stop.
    ENDED = "ended"
    # This invocation made no booking: a designated session, a refusal, a lost race.
    NOT_OWNED = "not-owned"
    # The record could not be read, or the stop not confirmed: the booking's end bounds it, and
    # `release` can be called again.
    UNCONFIRMED = "unconfirmed"


# The states `release` has nothing more to do in.
_SETTLED = frozenset({Cleanup.RELEASED, Cleanup.YIELDED, Cleanup.ENDED, Cleanup.NOT_OWNED})


def _item(session: Session | None) -> dict[str, object] | None:
    return None if session is None else dict(session.to_item())


def _session(item: dict[str, object] | None) -> Session | None:
    return None if item is None else Session.from_item(item)


@dataclass
class Journal:
    """What one invocation saved of its booking, in the local file `path`.

    `intended` is the whole session a managed booking is about to write, saved before the write:
    its `set_by` is the writer marker and its `ends_at` the deadline. `booking` is the session the
    write returned, the exact booking. `stops` are the sessions its stops were about to write,
    each saved before its write. `cleanup` is what became of the booking, once that is settled.
    """

    path: Path
    mode: Mode | None = None
    intended: Session | None = None
    booking: Session | None = None
    stops: list[Session] = field(default_factory=list)
    cleanup: Cleanup | None = None

    @classmethod
    def load(cls, path: Path) -> Journal:
        """The journal saved in the file, an empty one when there is no file."""
        if not path.exists():
            return cls(path)
        saved = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path,
            mode=None if saved["mode"] is None else Mode(saved["mode"]),
            intended=_session(saved["intended"]),
            booking=_session(saved["booking"]),
            stops=[Session.from_item(item) for item in saved["stops"]],
            cleanup=None if saved["cleanup"] is None else Cleanup(saved["cleanup"]),
        )

    def save(self) -> None:
        """Writes the file whole, through a file beside it, so that a reader never finds half of it."""
        saved = {
            "mode": self.mode,
            "intended": _item(self.intended),
            "booking": _item(self.booking),
            "stops": [_item(stop) for stop in self.stops],
            "cleanup": self.cleanup,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        beside = self.path.with_name(self.path.name + ".tmp")
        beside.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        os.replace(beside, self.path)

    @property
    def marker(self) -> str | None:
        """The writer marker of this invocation's booking, `None` when it set out to make none."""
        return None if self.intended is None else self.intended.set_by

    @property
    def deadline(self) -> int | None:
        """The end this invocation's booking was written with, which bounds it whatever happens."""
        return None if self.intended is None else self.intended.ends_at


@dataclass(frozen=True)
class Selection:
    """What the evaluation needs: `instances` hosts of the pool in service, each serving `release`,
    for a batch of `batch_seconds` and then `cleanup_seconds` to drain and clean up."""

    pool: str
    instances: int
    release: str
    batch_seconds: int
    cleanup_seconds: int


@dataclass(frozen=True)
class Access:
    """How the driver reaches the deployed pool. `sessions` holds the pool-state table, the scaler's
    function and `session_max_hours`, as the admin's commands take them. `gateway_ready` says
    whether the gateway is ready for the pool, and `releases` gives the release each host in
    service reports, one entry a host. `clock` gives the time in seconds since the epoch."""

    sessions: admin_session.Sessions
    gateway_ready: Callable[[], bool]
    releases: Callable[[], Sequence[str]]
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Outcome:
    """What `acquire` or `wait` came to. Ready: no `reason`, and `session` is the session the batch
    runs in. Declined: the `reason`, and `cleanup` says what became of this invocation's booking."""

    reason: Reason | None
    session: Session | None
    cleanup: Cleanup

    @property
    def ready(self) -> bool:
        return self.reason is None


def new_marker() -> str:
    """A writer marker no other invocation has: what a managed booking is written under."""
    return MARKER_PREFIX + secrets.token_hex(16)


class Capacity:
    """The capacity of one invocation: `acquire`, then the driver's batch, then `release`. After a
    restart, over the journal the earlier process left: `reconcile`, then `wait` or `release`."""

    def __init__(self, selection: Selection, access: Access, journal: Journal) -> None:
        self.selection, self.access, self.journal = selection, access, journal
        self._hours = access.sessions.session_max_hours
        self._reserved = selection.batch_seconds + selection.cleanup_seconds

    @property
    def _table(self) -> Any:
        return self.access.sessions.pools.table

    def _now(self) -> int:
        return int(self.access.clock())

    def _read(self) -> Session | None:
        """The pool's record. One that does not read as a session is not this invocation's: `None`."""
        try:
            return pool_booking.read(self._table, self.selection.pool)
        except RecordError:
            return None

    def _scaler_pass(self) -> None:
        """Asks the scaler for a run. One that is refused or fails changes nothing here: the
        scaler's next run is within a minute, and it reads the same record."""
        with contextlib.suppress(AdminError, *_AWS):
            admin_session.applied(self.access.sessions)

    def acquire(self, *, minutes: int | None = None) -> Outcome:
        """Books the pool for `minutes` and waits for its hosts, or, with no `minutes`, waits for
        the hosts of the session the operator designated. A journal serves one invocation."""
        if self.journal.mode is not None:
            raise ValueError("the journal already holds an invocation: reconcile it, or start a new one")
        self.journal.mode = Mode.EXISTING if minutes is None else Mode.MANAGED
        self.journal.save()
        try:
            most = admin_pools.max_instances(self.access.sessions.pools).get(self.selection.pool)
            if most is None:
                reason = Reason.POOL_NOT_DEPLOYED
            elif not 1 <= self.selection.instances <= most:
                reason = Reason.OUTSIDE_POOL_LIMITS
            else:
                reason = None if minutes is None else self._booked(minutes, most)
        except BaseException:
            self.release()
            raise
        return self.wait() if reason is None else self._outcome(reason)

    def _booked(self, minutes: int, most: int) -> Reason | None:
        """Books the pool, of which a session may book `most` hosts; why not, when it did not."""
        chosen, journal, now = self.selection, self.journal, self._now()
        bounds: dict[str, Any] = dict(max_instances=most, session_max_hours=self._hours)
        marker = new_marker()
        try:
            # The session `booking.book` writes, by the rule it writes with: saved before the write.
            intended = rules.started(
                None,
                pool=chosen.pool,
                instances=chosen.instances,
                starts_at=now,
                ends_at=now + minutes * MINUTE_SECONDS,
                set_by=marker,
                now=now,
                **bounds,
            )
        except rules.SessionError:
            return Reason.OUTSIDE_POOL_LIMITS
        if intended.ends_at - now < self._reserved:
            return Reason.WINDOW_TOO_SHORT
        journal.intended = intended
        journal.save()
        answered = True
        try:
            written = pool_booking.book(
                self._table, chosen.pool, minutes=minutes, set_by=marker, now=now, instances=chosen.instances, **bounds
            )
        except _AWS:
            written, answered = None, False
        if written is None:
            # Not booked, or booked with the answer lost: only the record says which.
            try:
                actual = self._read()
            except _AWS:
                return Reason.BOOKING_UNCONFIRMED
            if actual != intended:
                taken = actual is not None and now < rules.end(actual, session_max_hours=self._hours)
                return Reason.POOL_ALREADY_BOOKED if answered or taken else Reason.BOOKING_UNCONFIRMED
            written = intended
        journal.booking = written
        journal.save()
        self._scaler_pass()
        return None

    def wait(self) -> Outcome:
        """Polls until the selected hosts are ready for the batch, or until the last moment the
        batch may start. A wait that is refused or interrupted releases this invocation's booking."""
        try:
            reason, session = self._waited()
        except BaseException:
            self.release()
            raise
        if reason is not None:
            return self._outcome(reason)
        held = Cleanup.HELD if self.journal.mode is Mode.MANAGED else Cleanup.NOT_OWNED
        return Outcome(None, session, held)

    def _outcome(self, reason: Reason) -> Outcome:
        return Outcome(reason, None, self.release())

    def _waited(self) -> tuple[Reason | None, Session | None]:
        last = None
        while True:
            now = self._now()
            session = self._session(now)
            if isinstance(session, Reason):
                return session, None
            left = rules.end(session, session_max_hours=self._hours) - self._reserved - now
            if left < 0:
                # Too late for the batch: for the reason the poll before found, when there was one.
                return last or Reason.WINDOW_TOO_SHORT, None
            last = self._unready(session, now)
            if last is None:
                return None, session
            if left == 0:
                return last, None
            self.access.sleep(min(POLL_SECONDS, left))

    def _session(self, now: int) -> Session | Reason:
        """The session the batch would run in, as the table holds it now."""
        actual = self._read()
        if self.journal.mode is Mode.MANAGED:
            if self.journal.booking is None:
                return Reason.BOOKING_UNCONFIRMED
            return actual if actual is not None and actual == self.journal.booking else Reason.BOOKING_CHANGED
        if actual is None or now >= rules.end(actual, session_max_hours=self._hours):
            return Reason.SESSION_NOT_FOUND
        return actual if actual.instances == self.selection.instances else Reason.SESSION_COUNT_MISMATCH

    def _unready(self, session: Session, now: int) -> Reason | None:
        """What one poll finds missing, `None` when the selected hosts are ready."""
        chosen = self.selection
        supplies = admin_session.supply(self.access.sessions, now=now)
        note = next((supply.note for supply in supplies if supply.pool == chosen.pool), None)
        if note is None:
            return Reason.POOL_NOT_DEPLOYED
        if now - note.written_at > SUPPLY_FRESH_SECONDS or note.written_at < session.set_at:
            return Reason.SUPPLY_STALE
        if note.in_service != chosen.instances:
            return Reason.CAPACITY_UNAVAILABLE
        if not self.access.gateway_ready():
            return Reason.GATEWAY_NOT_READY
        served = list(self.access.releases())
        if len(served) != chosen.instances or any(release != chosen.release for release in served):
            return Reason.RELEASE_MISMATCH
        return None

    def _settled(self, cleanup: Cleanup) -> Cleanup:
        self.journal.cleanup = cleanup
        self.journal.save()
        if cleanup is Cleanup.RELEASED:
            self._scaler_pass()
        return cleanup

    def reconcile(self) -> Cleanup:
        """What the journal's booking is in the table now. Nothing is written to the table."""
        journal = self.journal
        if journal.cleanup is not None and journal.cleanup in _SETTLED:
            return journal.cleanup
        if journal.mode is None:
            return Cleanup.NOT_OWNED
        if journal.mode is not Mode.MANAGED or journal.intended is None:
            return self._settled(Cleanup.NOT_OWNED)
        try:
            actual = self._read()
        except _AWS:
            return Cleanup.UNCONFIRMED
        mine = journal.booking or journal.intended
        if actual == mine:
            if journal.booking is None:
                # The write whose answer never came was stored: this is the booking.
                journal.booking = mine
                journal.save()
            ended = self._now() >= rules.end(mine, session_max_hours=self._hours)
            return self._settled(Cleanup.ENDED) if ended else Cleanup.HELD
        if actual in journal.stops:
            return self._settled(Cleanup.RELEASED)
        return self._settled(Cleanup.NOT_OWNED if journal.booking is None else Cleanup.YIELDED)

    def release(self) -> Cleanup:
        """Stops the booking this invocation made, while the record still is that booking. Each
        try reads the record first, so a stop that was stored with its answer lost is found, and a
        record someone changed is left as it is."""
        for attempt in range(STOP_ATTEMPTS):
            if attempt:
                self.access.sleep(RETRY_SECONDS)
            found = self.reconcile()
            if found is Cleanup.HELD:
                found = self._stopped()
            if found in _SETTLED:
                return found
        found = self.reconcile()
        return Cleanup.UNCONFIRMED if found is Cleanup.HELD else found

    def _stopped(self) -> Cleanup:
        """One conditional stop of the booking."""
        booking, now = self.journal.booking, self._now()
        if booking is None:
            return Cleanup.UNCONFIRMED
        stop = rules.stopped(booking, set_by=booking.set_by, now=now, session_max_hours=self._hours)
        if stop is None:
            return self._settled(Cleanup.ENDED)
        # The session `booking.stop` writes, by the rule it writes with: saved before the write.
        self.journal.stops.append(stop)
        self.journal.save()
        try:
            written = pool_booking.stop(self._table, booking, now=now, session_max_hours=self._hours)
        except _AWS:
            return Cleanup.UNCONFIRMED
        # `None` does not say why nothing was written: the next read of the record does.
        return Cleanup.UNCONFIRMED if written is None else self._settled(Cleanup.RELEASED)
