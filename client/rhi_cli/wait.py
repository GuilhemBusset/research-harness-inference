"""`rhi status` and `rhi wait`: what state each pool is in, and waiting until one is ready (system design, section 5.3).

Both use the stored endpoint and the stored key, sent as `Authorization: Bearer`, and name a pool
by its model name. Both read the gateway's `GET /status` and nothing else, and neither starts
anything: a pool has instances while a session the instructor booked for it is active (section
5.6), and no call of a student's changes that.

`rhi status` prints one line per pool the status lists: its model name, its state (`ready`,
`warming`, `unavailable`), when it is unavailable its cause (`capacity_unavailable`,
`no_session`), and, when the gateway gives one, the time the pool's session ends, in the time zone
of the student's machine.

`rhi wait <model>` is how a student who arrives before a session's instances waits for them:

1. It reads the status at once and then every 10 seconds, counted from one call to the next
   whatever the answer took, and prints the pool's line each time its state or its cause changes.
2. It exits 0 when the state is `ready`.
3. It keeps waiting while the pool is `warming`, and while it is `unavailable` with
   `capacity_unavailable`: both mean that the session is on and its instances are not there yet.
4. It stops, prints the cause and exits 1 when the pool is `unavailable` with any other cause
   (`no_session`: no session is on, so no instance is coming), when the gateway answers a call
   with an error (`invalid_key`, say), when the status does not list the name, and after the
   minutes of `--timeout`: an answer that arrives after them is too late, even one that reads
   `ready`.

The default timeout is 20 minutes, above the 15 minutes a launch on the coursework pool is given
(section 17). Ten seconds between two status calls keeps a whole class that waits behind one campus
address far under the edge's rate limit.

Every call goes through a `Transport`, and the waiting through `sleep` and `clock`, as in
`rhi_cli.login`: the tests give all three.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from rhi_cli import login, store

STATUS_PATH = "/status"

READY = "ready"
WARMING = "warming"
UNAVAILABLE = "unavailable"
# The cause of an unavailable pool whose session is on: its instances are not there yet.
CAPACITY_UNAVAILABLE = "capacity_unavailable"

POLL_SECONDS = 10
DEFAULT_TIMEOUT_MINUTES = 20


@dataclass(frozen=True)
class Record:
    """One pool's record, as the status gives it.

    `cause` when the state is `unavailable`, and `session_ends_at`, in seconds since the epoch,
    when the gateway gives the end of the pool's session.
    """

    model: str
    state: str
    cause: str | None = None
    session_ends_at: int | None = None

    @property
    def coming(self) -> bool:
        """Whether the pool's session is on and its instances are not there yet."""
        return self.state == WARMING or (self.state == UNAVAILABLE and self.cause == CAPACITY_UNAVAILABLE)

    def line(self) -> str:
        """The state and, when the pool is unavailable, the cause."""
        return f"{self.state}  {self.cause}" if self.cause else self.state


def _record(body: Any) -> Record | None:
    if not isinstance(body, dict):
        return None
    model, state, cause, ends_at = (body.get(name) for name in ("model", "state", "cause", "session_ends_at"))
    if not isinstance(model, str) or not isinstance(state, str):
        return None
    return Record(
        model,
        state,
        cause if isinstance(cause, str) and state == UNAVAILABLE else None,
        ends_at if isinstance(ends_at, int) and not isinstance(ends_at, bool) else None,
    )


def _when(epoch: int) -> str | None:
    """That time in the machine's own time zone, with its offset; None for a number that is no time."""
    try:
        return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).astimezone().strftime("%Y-%m-%d %H:%M %z")
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class _Gateway:
    """The stored endpoint, with the stored key on every call."""

    address: str
    key: str
    transport: login.Transport

    def records(self) -> list[Record]:
        """The record of every pool the status lists; `LoginError`, with the gateway's cause, when it answers an error."""
        headers = {"Authorization": f"Bearer {self.key}"}
        status, body = self.transport(login.Call("GET", self.address + STATUS_PATH, headers=headers))
        if status != 200:
            raise login.LoginError(f"the endpoint refused the call: {login.refusal(status, body)}")
        pools = body.get("pools") if isinstance(body, dict) else None
        records = [_record(pool) for pool in pools] if isinstance(pools, list) else None
        listed = [record for record in records or [] if record is not None]
        if records is None or len(listed) != len(records):
            raise login.LoginError("the endpoint's status is not a list of pools.")
        return listed


def _gateway(transport: login.Transport) -> _Gateway:
    address, key = login.signed_in()
    return _Gateway(address, key, transport)


def status(*, transport: login.Transport = login.http_transport, out: TextIO | None = None) -> None:
    """Print each pool's model name, state, cause when it is unavailable, and the end of its session when the
    gateway gives one; `LoginError` when it fails."""
    out = out or sys.stdout
    records = _gateway(transport).records()
    width = max((len(record.model) for record in records), default=0)
    for record in records:
        ends = _when(record.session_ends_at) if record.session_ends_at is not None else None
        line = f"{record.model:<{width}}  {record.line()}"
        print(f"{line}  session ends {ends}" if ends else line, file=out)


def _wait(
    model: str,
    timeout_minutes: float,
    transport: login.Transport,
    sleep: Callable[[float], object],
    clock: Callable[[], float],
    out: TextIO,
) -> None:
    gateway = _gateway(transport)
    deadline = clock() + timeout_minutes * 60
    printed = None

    def late() -> login.LoginError:
        return login.LoginError(
            f"{model} is not ready after {timeout_minutes:g} minutes. Run rhi wait again, or tell the instructor."
        )

    while True:
        asked = clock()
        if asked >= deadline:
            raise late()
        listed = [record for record in gateway.records() if record.model == model]
        if not listed:
            raise login.LoginError(f"the endpoint's status does not list {model}: rhi status prints the model names.")
        record = listed[0]
        if clock() >= deadline:  # the answer took the wait past its timeout: whatever it says, it came too late
            raise late()
        if record.line() != printed:
            printed = record.line()
            print(f"{model}: {printed}", file=out, flush=True)
        if record.state == READY:
            return
        if not record.coming:
            raise login.LoginError(
                f"{model} is {record.state}, and no instance is coming: {record.cause or 'no cause was given'}"
            )
        # Ten seconds from one call to the next, whatever the answer took; at once after a slower one.
        sleep(max(0.0, min(asked + POLL_SECONDS, deadline) - clock()))


def wait(
    model: str,
    timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES,
    *,
    transport: login.Transport = login.http_transport,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Read the status until the pool `model` names is ready, starting nothing; the command's exit status.

    0 once the pool is ready. 1, with the cause on `err`, when the pool has no session, when the
    gateway answers an error, when the status does not list the name, and when `timeout_minutes`
    have passed.
    """
    try:
        _wait(model, timeout_minutes, transport, sleep, clock, out or sys.stdout)
    except (login.LoginError, store.StoreError) as error:
        print(f"rhi wait: {error}", file=err or sys.stderr)
        return 1
    return 0


def _minutes(text: str) -> float:
    minutes = float(text)
    if not 0 < minutes < float("inf"):
        raise ValueError(text)
    return minutes


def _run_status(arguments: argparse.Namespace) -> int:
    try:
        status(transport=login.http_transport)
    except (login.LoginError, store.StoreError) as error:
        print(f"rhi status: {error}", file=sys.stderr)
        return 1
    return 0


def _run_wait(arguments: argparse.Namespace) -> int:
    return wait(arguments.model, arguments.timeout, transport=login.http_transport, sleep=time.sleep)


def register(commands: argparse._SubParsersAction) -> None:
    """Add `rhi status` and `rhi wait` to the CLI's commands."""
    parser = commands.add_parser("status", help="print the state of each pool")
    parser.set_defaults(run=_run_status)

    parser = commands.add_parser("wait", help="wait until a pool is ready, starting nothing")
    parser.add_argument("model", help="the model name of the pool, as rhi status prints it")
    parser.add_argument(
        "--timeout",
        type=_minutes,
        default=DEFAULT_TIMEOUT_MINUTES,
        metavar="MINUTES",
        help=f"how long to wait before giving up, in minutes ({DEFAULT_TIMEOUT_MINUTES} by default)",
    )
    parser.set_defaults(run=_run_wait)
