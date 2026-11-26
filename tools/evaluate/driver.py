"""The operator-started driver: typed evaluation runs submitted by a local client, run by the one
controller (docs/design/agent-session-evaluation.md, "How the operator runs it").

    scripts/evaluate.sh driver
    scripts/evaluate.sh submit --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new
    scripts/evaluate.sh inspect --submission <submission-id>
    scripts/evaluate.sh cancel --socket --submission <submission-id>

The operator starts the driver in a terminal, with the operator's scoped AWS session and the
GitHub issue writer's token in its environment (`controller.connect_live`). A coding agent, or
anyone else on the machine, holds neither: it can only ask the driver, through its socket, to
submit a run, to show one or to cancel one. The driver runs every run it admits through
`controller.execute`, the controller the terminal's `run` uses, in a thread of its own: a client
that exits, or loses the answer, leaves the run going.

**The socket.** A Unix socket in a directory only the driver's user may enter (0700: made so when
it is not there, refused and left as it is when it is there and anyone else may enter it), itself
0600, and every connection's peer, read from the kernel (`SO_PEERCRED`), must be that user: any
other is refused before its request is read. One connection carries one request and one answer,
each one JSON object on one line, and each connection is answered in a thread of its own: an
admission waiting on GitHub or the pool holds up no inspect or cancel. A driver holds its socket
by a lock beside it, and replaces a socket left at its path only when nothing answers on it: a
second driver for the same socket, or the same runs, refuses to start (`InUse`).

**The requests** are typed, and nothing else is: `submit` (a `submission` ID and the `run`'s
fields, `RUN_FIELDS`), `inspect` and `cancel` (a `submission` or a `run_id`). A request is refused
before anything is dispatched, with a reason of `Refusal`, when it is not one JSON object, when it
names another operation (a generic command), when it has a field the operation does not take or
lacks one it needs, when a value has the wrong type or range, when its booking bound is invalid
(a managed booking needs `window_ms`: whole minutes, above zero, at most `MAX_WINDOW_MS`), and
when it carries anything credential-like: a field whose name says so, a value with an
authorization or key header or a bearer token in it, or a value the public hygiene scanner finds
anything in. A refused request reaches no service and writes nothing.

**Admission.** A valid `submit` is then checked against the deployment, as `controller.admit`
checks it, which only reads and starts nothing on this host: the selected pool deployed with room
for the hosts and the minutes (its `max_instances` and `session_max_hours`), the experiment's
destination answering as the driver (a selected issue read from GitHub: it must exist, be the
driver's and record the request's workload), the designated session with `--session existing`
(on the pool, of the selected hosts, with time left), and a writable journal root. A request that
fails is refused with the failed checks and nothing of it is kept. The runtime's own checks, which
may prepare the trial image and start its containers, are the run's preflight, after step 2. One
that passes is admitted, in this order, before anything outside the driver is touched:

1. The submission's record is saved under `<runs>/submissions/`, created whole and never
   replaced: the submission ID, the digest of its fields, the whole typed request, the run ID
   chosen for it, and the time it was admitted on the runtime's clock.
2. The run's journal is created under that run ID (`controller.create_run`).
3. The run is dispatched: `controller.execute` resumes it by its ID, so the issue, then the
   booking, come after both. The answer carries the run ID and the status.

A submission ID maps to one run for good. The same ID with the same fields answers that run again
and starts nothing new; the same ID with other fields is refused (`submission-conflict`).

**What an answer holds** (`ANSWER_FIELDS`): the submission and run IDs, the step the controller
reached, the issue's URL once its number is journaled (GitHub confirmed it, in the run or in a
later `publish --resume`), the ending, the exit code, the publication, and whether a cancel was
asked for. Never the booking, its times, its writer marker, the journal or a credential: those
stay in the driver and its private run directories.

**Restart.** Nothing the driver needs lives only in its memory. A started driver reads every
submission's record: a run with no exit recorded is created if its creation was cut short and
resumed by its ID. Its `--for` still counts from its admission (`Request.started_s`), and it makes
one managed booking at most (`Request.bookings`): a resumed run that finds its booking released
books no other, and one whose booking was intended but never set out to be written makes it. A
run in a designated session resumes in that same session, untouched. A driver keeps its root
until its last run has ended, however it was closed, so the next driver never takes a journal
one of its runs still writes. A cancel is kept beside the submission's record, so it holds through
a run's preflight and through a restart.

**Cancel** asks the run to stop: before its issue nothing is created or booked; after, admissions
stop and the booking it made is released (`controller`, step 4).
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import signal
import socket
import stat
import struct
import tempfile
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evaluate import controller, publisher
from evaluate import plan as planning
from evaluate.contracts import Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

# Where a client finds the driver when no socket is named.
SOCKET_VARIABLE = "RHI_EVALUATE_SOCKET"
SUBMISSIONS = "submissions"
MINUTE_MS = 60_000
# The longest `--for` a request may carry. The deployed `session_max_hours` bounds it further.
MAX_WINDOW_MS = 7 * 24 * 60 * MINUTE_MS
MAX_REQUEST_BYTES = 64 * 1024
# The connections answered at once: one more is closed unanswered.
MAX_CONNECTIONS = 16
MAX_INSTANCES = 1024
MAX_STUDENTS = 1024
_SUBMISSION = re.compile(r"[0-9a-f]{32}")
_RUN_ID = re.compile(r"[0-9a-f]{16}")
_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
_TARGET = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# A field whose name says it carries a credential, wherever it is in a request.
_CREDENTIAL_NAME = re.compile(
    r"token|secret|passw|credential|api[_-]?key|access[_-]?key|private[_-]?key|auth|cookie|signature", re.IGNORECASE
)
# Text that carries a credential the way a request to a service does, wherever it is in a request:
# an authorization or key header, or a bearer token.
_CREDENTIAL_TEXT = re.compile(
    r"\b(?:proxy-)?authorization\s*:|\b(?:x-api-key|api-key|x-amz-security-token|cookie|set-cookie)\s*:"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{6,}",
    re.IGNORECASE,
)
_SEED_MAX = 2**31 - 1
_ISSUE_MAX = 999_999_999


class Op(StrEnum):
    """What a client may ask: the whole vocabulary."""

    SUBMIT = "submit"
    INSPECT = "inspect"
    CANCEL = "cancel"


class Refusal(StrEnum):
    """Why the driver declined a request: the whole vocabulary."""

    ACCESS_DENIED = "access-denied"
    MALFORMED = "malformed-request"
    UNKNOWN_COMMAND = "unknown-command"
    UNKNOWN_FIELD = "unknown-field"
    MISSING_FIELD = "missing-field"
    INVALID_FIELD = "invalid-field"
    INVALID_BOOKING = "invalid-booking"
    CREDENTIAL = "credential-in-request"
    NOT_ADMITTED = "not-admitted"
    SUBMISSION_CONFLICT = "submission-conflict"
    NO_SUCH_RUN = "no-such-run"
    DRIVER_ERROR = "driver-error"


# The fields of a submitted run, with what each must be.
RUN_FIELDS = (
    "suite",
    "profile",
    "pool",
    "students",
    "instances",
    "window_ms",
    "session",
    "issue",
    "repository",
    "question",
    "target",
    "input_seed",
    "schedule_seed",
    "rate",
)
_REQUIRED_RUN = frozenset({"profile", "pool", "students", "instances", "issue", "repository"})
# The fields of each operation's request. `inspect` and `cancel` name one of their two.
_OP_FIELDS = {
    Op.SUBMIT: frozenset({"op", "submission", "run"}),
    Op.INSPECT: frozenset({"op", "submission", "run_id"}),
    Op.CANCEL: frozenset({"op", "submission", "run_id"}),
}
# What an answer may hold, and nothing else.
ANSWER_FIELDS = frozenset(
    {
        "ok",
        "refused",
        "detail",
        "submission",
        "run_id",
        "step",
        "issue",
        "ending",
        "exit",
        "publication",
        "cancel_requested",
        "running",
    }
)


class DriverError(Exception):
    def __init__(self, reason: Refusal, detail: str | None = None) -> None:
        super().__init__(reason.value if detail is None else f"{reason.value}: {detail}")
        self.reason, self.detail = reason, detail


def default_socket() -> Path:
    """The driver's socket: `RHI_EVALUATE_SOCKET`, else in the user's runtime directory, else in
    a directory of the user's own under the system's temporary one. A Unix socket's path is short."""
    named = os.environ.get(SOCKET_VARIABLE)
    if named:
        return Path(named)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    return base / f"rhi-evaluate-{os.getuid()}" / "driver.sock"


# --- Checking a request ------------------------------------------------------------------------


def _strings(value: object, keys: list[str]) -> Iterator[str]:
    """Every string in a JSON value; the keys of every object are added to `keys`."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, inner in value.items():
            keys.append(str(key))
            yield from _strings(inner, keys)
    elif isinstance(value, list):
        for inner in value:
            yield from _strings(inner, keys)


def _whole(value: object, low: int, high: int, name: str, reason: Refusal = Refusal.INVALID_FIELD) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DriverError(reason, name)
    return value


def _optional_whole(value: object, low: int, high: int, name: str) -> int | None:
    return None if value is None else _whole(value, low, high, name)


def _text(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DriverError(Refusal.INVALID_FIELD, name)
    return value


def _fields(message: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str]) -> None:
    unknown = sorted(set(message) - allowed)
    if unknown:
        raise DriverError(Refusal.UNKNOWN_FIELD, unknown[0])
    missing = sorted(required - set(message))
    if missing:
        raise DriverError(Refusal.MISSING_FIELD, missing[0])


def typed_run(run: object) -> dict[str, Any]:
    """The run's fields checked and completed: every field of `RUN_FIELDS`, in one canonical
    form. `DriverError` names the first that is wrong."""
    if not isinstance(run, dict):
        raise DriverError(Refusal.INVALID_FIELD, "run")
    _fields(run, frozenset(RUN_FIELDS), _REQUIRED_RUN)
    profiles = {profile.value for profile in Profile}
    if run["profile"] not in profiles:
        raise DriverError(Refusal.INVALID_FIELD, "profile")
    session = run.get("session")
    if session not in (None, "existing"):
        raise DriverError(Refusal.INVALID_FIELD, "session")
    window = run.get("window_ms")
    if window is not None or session is None:
        # The booking's bound: whole minutes, above zero, and no longer than a driver books.
        window = _whole(window, MINUTE_MS, MAX_WINDOW_MS, "window_ms", Refusal.INVALID_BOOKING)
        if window % MINUTE_MS:
            raise DriverError(Refusal.INVALID_BOOKING, "window_ms")
    issue = run["issue"]
    if issue != "new":
        _whole(issue, 1, _ISSUE_MAX, "issue")
    if not publisher.valid_repository(run["repository"]):
        raise DriverError(Refusal.INVALID_FIELD, "repository")
    question = run.get("question")
    if question is not None and (not isinstance(question, str) or not 0 < len(question) <= 500):
        raise DriverError(Refusal.INVALID_FIELD, "question")
    rate = run.get("rate")
    if rate is not None and (
        isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate) or not 0 < rate < 10**6
    ):
        raise DriverError(Refusal.INVALID_FIELD, "rate")
    return {
        "suite": _text(run.get("suite", planning.DEFAULT_SUITE), _NAME, "suite"),
        "profile": run["profile"],
        "pool": _text(run["pool"], _NAME, "pool"),
        "students": _whole(run["students"], 1, MAX_STUDENTS, "students"),
        "instances": _whole(run["instances"], 1, MAX_INSTANCES, "instances"),
        "window_ms": window,
        "session": session,
        "issue": issue,
        "repository": run["repository"],
        "question": question,
        "target": _text(run.get("target", "reference"), _TARGET, "target"),
        "input_seed": _optional_whole(run.get("input_seed"), 0, _SEED_MAX, "input_seed"),
        "schedule_seed": _optional_whole(run.get("schedule_seed"), 0, _SEED_MAX, "schedule_seed"),
        "rate": None if rate is None else float(rate),
    }


@dataclass(frozen=True)
class Message:
    """A request that passed every check: its operation and what it names."""

    op: Op
    submission: str | None = None
    run_id: str | None = None
    run: dict[str, Any] | None = None


def parse(raw: bytes, scan: Callable[[str, str], object]) -> Message:
    """One request, checked whole before anything is done for it. `scan` is the public hygiene
    scanner's `scan_text`."""
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise DriverError(Refusal.MALFORMED) from error
    if not isinstance(message, dict):
        raise DriverError(Refusal.MALFORMED)
    keys: list[str] = []
    texts = list(_strings(message, keys))
    if (
        any(_CREDENTIAL_NAME.search(key) for key in keys)
        or any(_CREDENTIAL_TEXT.search(text) for text in texts)
        or any(scan(text, "evaluate-request.json") for text in texts)
    ):
        raise DriverError(Refusal.CREDENTIAL)
    try:
        op = Op(message.get("op"))
    except ValueError as error:
        raise DriverError(Refusal.UNKNOWN_COMMAND) from error
    if op is Op.SUBMIT:
        _fields(message, _OP_FIELDS[op], _OP_FIELDS[op])
        return Message(
            op, submission=_text(message["submission"], _SUBMISSION, "submission"), run=typed_run(message["run"])
        )
    _fields(message, _OP_FIELDS[op], frozenset({"op"}))
    if ("submission" in message) == ("run_id" in message):
        raise DriverError(Refusal.MISSING_FIELD, "submission or run_id")
    if "submission" in message:
        return Message(op, submission=_text(message["submission"], _SUBMISSION, "submission"))
    return Message(op, run_id=_text(message["run_id"], _RUN_ID, "run_id"))


def digest(run: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(run, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- The driver's own files --------------------------------------------------------------------


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, value: Mapping[str, Any]) -> bool:
    """Creates the file whole, private, never over one that is there: False if it was."""
    beside = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(beside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(beside, path)
        except FileExistsError:
            return False
        _sync(path.parent)
        return True
    finally:
        beside.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def request_of(record: Mapping[str, Any], fallback_rate: tuple[float, str]) -> controller.Request:
    """The controller's request for a submission's record: its run resumed by the ID it was
    admitted under, `--for` counted from its admission, one booking at most."""
    run = record["run"]
    return controller.Request(
        suite=run["suite"],
        profile=Profile(run["profile"]),
        pool=run["pool"],
        students=run["students"],
        instances=run["instances"],
        repository=run["repository"],
        window_ms=run["window_ms"],
        existing=run["session"] == "existing",
        issue=None if run["issue"] == "new" else run["issue"],
        resume=record["run_id"],
        question=run["question"],
        target=run["target"],
        input_seed=run["input_seed"],
        schedule_seed=run["schedule_seed"],
        rate=None if run["rate"] is None else (run["rate"], "supplied"),
        fallback_rate=fallback_rate,
        started_s=record["started_s"],
        bookings=1,
    )


# --- The driver --------------------------------------------------------------------------------


class InUse(RuntimeError):
    """Another driver holds the runs root or the socket: this one starts nothing."""


def _locked(path: Path) -> int:
    """The file's exclusive lock, held by the descriptor returned; `InUse` when another holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        raise InUse(path.name) from error
    return fd


def _private_directory(folder: Path) -> None:
    """The socket's directory: made 0700 when it is not there. One that is there is never
    changed: it must be a directory of this user's that no one else may enter."""
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=0o700, parents=True)
    found = folder.lstat()
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
        raise PermissionError("the socket's directory is not this user's alone")


def _answers(socket_path: Path) -> bool:
    """Whether something listens on the socket. Only a socket that refuses a connection is stale."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        try:
            probe.connect(str(socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except OSError:
            return True
        return True


class Driver:
    """The driver of the runs under `root`, listening on `socket_path` for the user `owner` (this
    process's user unless given). `connect` opens the runtime of a scope, as `controller.execute`
    takes it: in the operator's driver, `controller.connect_live`, with the driver's credentials.
    `on_admitted` runs once a submission's record is saved, before its journal is created.

    One driver holds the root and the socket at a time, each by a lock: the root's from `start`
    until its last run and its last connection have ended, whatever `close` was told to wait, so
    that a driver started after it never takes a journal one of its runs still writes; the
    socket's while it listens, so that a socket another driver answers on is never replaced."""

    def __init__(
        self,
        root: Path,
        socket_path: Path,
        *,
        connect: controller.Connect,
        owner: int | None = None,
        fallback_rate: tuple[float, str] = (1.0, "example"),
        on_admitted: Callable[[str], None] | None = None,
    ) -> None:
        self.root, self.socket_path, self.connect = root, socket_path, connect
        self.owner = os.getuid() if owner is None else owner
        self.fallback_rate = fallback_rate
        self.on_admitted = on_admitted
        self.submissions = root / SUBMISSIONS
        # Guards the threads, the locks' descriptors and the admission locks.
        self._guard = threading.Lock()
        # One admission at a time for each submission ID; other IDs and other requests go on.
        self._admitting: dict[str, threading.Lock] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._handlers: set[threading.Thread] = set()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None
        self._server: threading.Thread | None = None
        self._root_lock: int | None = None
        self._socket_lock: int | None = None
        self._scan: Callable[[str, str], object] = publisher.hygiene_scan()

    # --- Lifetime ---

    def start(self) -> None:
        """Takes the root for this driver alone, then the socket, then resumes every run its
        submissions left unfinished. `InUse` when another driver holds either: nothing is
        changed then, of the root, the socket or the other driver."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.submissions.mkdir(mode=0o700, exist_ok=True)
        self._root_lock = _locked(self.root / "driver.lock")
        try:
            self._listener = self._listen()
            self._server = threading.Thread(target=self._serve, name="evaluate-driver", daemon=True)
            self._server.start()
            for record in self._records():
                self._ensure_running(record)
        except BaseException:
            self.close(wait=0)
            raise

    def _listen(self) -> socket.socket:
        folder = self.socket_path.parent
        _private_directory(folder)
        self._socket_lock = _locked(folder / f"{self.socket_path.name}.lock")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            # Only a socket no driver answers on is replaced; anything else is left as it is.
            if not self.socket_path.is_socket() or _answers(self.socket_path):
                raise InUse(self.socket_path.name)
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            listener.listen(16)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            raise
        return listener

    def close(self, *, wait: float | None = 60.0) -> None:
        """Stops listening and gives the socket up, then waits up to `wait` seconds for the runs.
        The root is given up once every run and connection of this driver has ended: a run still
        going is resumed by the next driver of the root after that. Closing again does nothing more."""
        self._stopping.set()
        server = self._server
        if server is not None and server is not threading.current_thread():
            server.join()
        with self._guard:
            listener, self._listener = self._listener, None
            held, self._socket_lock = self._socket_lock, None
        if listener is not None:
            listener.close()
            # This driver holds the socket's lock: the path is still its own.
            self.socket_path.unlink(missing_ok=True)
        if held is not None:
            os.close(held)
        for thread in self._busy():
            thread.join(wait)
        if not self._busy():
            self._release_root()
        else:
            threading.Thread(target=self._release_root_when_idle, name="evaluate-driver-handover", daemon=True).start()

    def _busy(self) -> list[threading.Thread]:
        """This driver's runs and connections still going, but the calling thread."""
        with self._guard:
            threads = [*self._workers.values(), *self._handlers]
        return [one for one in threads if one.is_alive() and one is not threading.current_thread()]

    def _release_root_when_idle(self) -> None:
        while busy := self._busy():
            for thread in busy:
                thread.join()
        self._release_root()

    def _release_root(self) -> None:
        with self._guard:
            held, self._root_lock = self._root_lock, None
        if held is not None:
            os.close(held)

    def serve_forever(self) -> None:
        """Starts the driver and keeps it until the process is interrupted or terminated. The runs
        still going are left to their journals and resumed by the next driver of the root."""
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: self._stopping.set())
        self.start()
        try:
            while not self._stopping.wait(1.0):
                pass
        finally:
            self.close(wait=0)

    # --- The socket ---

    def _peer(self, connection: socket.socket) -> int:
        _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid

    def _serve(self) -> None:
        """Each connection in a thread of its own, so that an admission waiting on GitHub or the
        pool never holds up an inspect or a cancel. Whatever ends the loop otherwise than an
        `Exception` (the process ending) ends the driver's answers, as its process ending would."""
        listener = self._listener
        assert listener is not None
        try:
            while not self._stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                if not self._slots.acquire(blocking=False):
                    connection.close()  # too many at once: the client sees no answer, and asks again
                    continue
                handler = threading.Thread(target=self._connection, args=(connection,), daemon=True)
                with self._guard:
                    self._handlers.add(handler)
                handler.start()
        except BaseException:  # noqa: BLE001 - the driver stops answering, as if its process had ended
            self._stopping.set()

    def _connection(self, connection: socket.socket) -> None:
        """One connection's request and answer. What ends it otherwise than an `Exception` (the
        process ending) ends the driver's answers: this connection gets none."""
        try:
            with connection:
                self._answer(connection)
        except BaseException:  # noqa: BLE001 - the driver stops answering, as if its process had ended
            self._stopping.set()
        finally:
            self._slots.release()
            with self._guard:
                self._handlers.discard(threading.current_thread())

    def _answer(self, connection: socket.socket) -> None:
        connection.settimeout(5.0)
        if self._peer(connection) != self.owner:
            answer: dict[str, Any] = {"ok": False, "refused": Refusal.ACCESS_DENIED.value}
        else:
            answer = self.handle(_receive(connection))
        with contextlib.suppress(OSError):
            connection.sendall(json.dumps({k: v for k, v in answer.items() if k in ANSWER_FIELDS}).encode() + b"\n")

    def handle(self, raw: bytes | None) -> dict[str, Any]:
        """One request's answer."""
        try:
            if raw is None:
                raise DriverError(Refusal.MALFORMED)
            message = parse(raw, self._scan)
            if message.op is Op.SUBMIT:
                return self.submit(message)
            record = self._find(message)
            if message.op is Op.CANCEL:
                _write_new(self.submissions / f"{record['submission']}.cancel", {"cancel": True})
            return self.shown(record)
        except DriverError as refused:
            return {"ok": False, "refused": refused.reason.value, "detail": refused.detail}
        except controller.ControllerError as refused:
            return {"ok": False, "refused": Refusal.NOT_ADMITTED.value, "detail": str(refused.reason)}
        except Exception as error:  # noqa: BLE001 - a fault of the driver is answered, and named by its type alone
            return {"ok": False, "refused": Refusal.DRIVER_ERROR.value, "detail": type(error).__name__}

    # --- Submissions ---

    def _admission(self, submission: str) -> threading.Lock:
        with self._guard:
            return self._admitting.setdefault(submission, threading.Lock())

    def submit(self, message: Message) -> dict[str, Any]:
        assert message.submission is not None and message.run is not None
        with self._admission(message.submission):
            path = self.submissions / f"{message.submission}.json"
            wanted = digest(message.run)
            record = _read(path)
            if record is None:
                admission = dataclasses.replace(
                    request_of({"run": message.run, "run_id": None, "started_s": None}, self.fallback_rate),
                    resume=False,
                    bookings=None,
                )
                started = controller.admit(admission, connect=self.connect, root=self.root)
                record = {
                    "submission": message.submission,
                    "digest": wanted,
                    "run": message.run,
                    "run_id": secrets.token_hex(8),
                    "started_s": started,
                }
                if not _write_new(path, record):
                    record = _read(path)
                elif self.on_admitted is not None:
                    self.on_admitted(record["run_id"])
            if record is None or record.get("digest") != wanted:
                raise DriverError(Refusal.SUBMISSION_CONFLICT)
            self._ensure_running(record)
            return self.shown(record)

    def _records(self) -> list[dict[str, Any]]:
        found = []
        for path in sorted(self.submissions.glob("*.json")):
            record = _read(path)
            if record is not None and _SUBMISSION.fullmatch(path.stem) and record.get("submission") == path.stem:
                found.append(record)
        return found

    def _find(self, message: Message) -> dict[str, Any]:
        if message.submission is not None:
            record = _read(self.submissions / f"{message.submission}.json")
        else:
            record = next((one for one in self._records() if one.get("run_id") == message.run_id), None)
        if record is None:
            raise DriverError(Refusal.NO_SUCH_RUN)
        return record

    def _exit_of(self, record: Mapping[str, Any]) -> int | None:
        found = _read(self.submissions / f"{record['submission']}.exit")
        return None if found is None else found.get("exit")

    def _cancel_requested(self, record: Mapping[str, Any]) -> bool:
        return (self.submissions / f"{record['submission']}.cancel").exists()

    def _ensure_running(self, record: dict[str, Any]) -> None:
        """Completes the run's creation and dispatches it, unless it ended or is running. A driver
        that is stopping starts no run: the next driver of the root resumes it."""
        submission = record["submission"]
        with self._guard:
            worker = self._workers.get(submission)
            if self._stopping.is_set() or self._exit_of(record) is not None or (worker is not None and worker.is_alive()):
                return
            request = request_of(record, self.fallback_rate)
            controller.create_run(self.root, request, record["run_id"])
            worker = threading.Thread(
                target=self._work, args=(record, request), name=f"run-{record['run_id']}", daemon=True
            )
            self._workers[submission] = worker
            worker.start()

    def _work(self, record: Mapping[str, Any], request: controller.Request) -> None:
        """One run, by the controller. Its output goes to the run's private log. Whatever ends it
        otherwise than by returning leaves no exit, so the next driver resumes it."""
        try:
            log = os.open(self.root / record["run_id"] / "driver.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(log, "a", encoding="utf-8") as out:
                code = controller.execute(
                    request,
                    connect=self.connect,
                    root=self.root,
                    out=out,
                    cancelled=lambda: self._cancel_requested(record),
                )
            _write_new(self.submissions / f"{record['submission']}.exit", {"exit": int(code)})
        except BaseException:  # noqa: BLE001 - the run is left to the journal and the next driver, as if the process had ended
            return

    def shown(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """What a client is told of a submission's run: allow-listed fields of its status, and the
        issue's URL once its number is journaled, which only GitHub's confirmation does, whichever
        invocation (the run's, or `publish --resume`) got it."""
        run_id = record["run_id"]
        try:
            status = controller.run_status(self.root, run_id)
            number = controller.saved_state(self.root, run_id).get("issue_number")
        except controller.ControllerError:
            status, number = {}, None
        confirmed = isinstance(number, int) and not isinstance(number, bool) and number > 0
        with self._guard:
            worker = self._workers.get(record["submission"])
        return {
            "ok": True,
            "submission": record["submission"],
            "run_id": run_id,
            "step": status.get("step", "admitted"),
            "issue": controller.issue_url(record["run"]["repository"], number) if confirmed else None,
            "ending": status.get("ending"),
            "exit": self._exit_of(record),
            "publication": status.get("publication"),
            "cancel_requested": self._cancel_requested(record),
            "running": worker is not None and worker.is_alive(),
        }


def _receive(connection: socket.socket) -> bytes | None:
    """The request's one line, or `None` when it is longer than a request may be or is cut."""
    chunks, size = [], 0
    while True:
        try:
            chunk = connection.recv(4096)
        except OSError:
            return None
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            return None
        if b"\n" in chunk:
            break
    line, newline, _ = b"".join(chunks).partition(b"\n")
    return line if newline or line else None


# --- The client --------------------------------------------------------------------------------


class Unanswered(Exception):
    """The driver could not be reached, or closed the connection with no answer."""


def call(socket_path: Path, message: Mapping[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
    """Sends one request to the driver and returns its answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(socket_path))
            connection.sendall(json.dumps(message).encode() + b"\n")
            connection.shutdown(socket.SHUT_WR)
            answer = b""
            while not answer.endswith(b"\n"):
                chunk = connection.recv(4096)
                if not chunk:
                    break
                answer += chunk
        except OSError as error:
            raise Unanswered(type(error).__name__) from error
    try:
        found = json.loads(answer)
    except ValueError as error:
        raise Unanswered("no answer") from error
    if not isinstance(found, dict):
        raise Unanswered("no answer")
    return found


def persist_submission(outbox: Path, submission: str | None, run: Mapping[str, Any]) -> str:
    """The submission's ID, saved with its fields in the client's private outbox before anything
    is sent: a client that loses the answer submits again with the same ID. A new ID is made when
    none is given; a saved one is kept as it is."""
    found = submission or secrets.token_hex(16)
    if not _SUBMISSION.fullmatch(found):
        raise DriverError(Refusal.INVALID_FIELD, "submission")
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_new(outbox / f"{found}.json", {"submission": found, "run": dict(run)})
    return found
