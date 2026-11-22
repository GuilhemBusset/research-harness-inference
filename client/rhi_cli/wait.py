"""`rhi status` and `rhi wait`: what state each pool is in, and starting one that is cold (system design, section 5.3).

Both use the stored endpoint and the stored key, sent as `Authorization: Bearer`, and name a pool
by its model name.

`rhi status` prints one line per pool the gateway's `GET /status` lists: its model name, its state
(`cold`, `warming`, `ready`, `unavailable`) and, when it is unavailable, its cause. A status call
starts nothing.

`rhi wait <model>` is the way to start a homework session on a cold pool, and the fallback for a
client that misbehaves while the gateway holds its request:

1. It sends `POST /wake` for the pool at once, and again every minute: an explicit wake counts as
   demand for 10 minutes or until the pool is ready, so the repeats keep the pool wanted for as
   long as the student waits. The wake answers with the pool's record, under its own model name
   (a `claude-` name wakes the default pool).
2. Between two wakes it reads `GET /status` every 10 seconds, and prints the pool's state each
   time it changes.
3. It exits 0 when the state is `ready`. It stops, prints the cause and exits 1 when a record
   reads `unavailable`, when the gateway refuses a call with an error (`invalid_key`, say), and
   after the minutes of `--timeout`: an answer that arrives after them is too late, even one that
   reads `ready`.

The default timeout is 20 minutes, above the 15 minutes a launch on the coursework pool is given
(section 17): a start that succeeds is never abandoned by the CLI first. Ten seconds between two
status calls keeps a whole class that waits behind one campus address far under the edge's rate
limit.

Every call goes through a `Transport`, and the waiting through `sleep` and `clock`, as in
`rhi_cli.login`: the tests give all three.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from rhi_cli import login, store

STATUS_PATH = "/status"
WAKE_PATH = "/wake"

READY = "ready"
UNAVAILABLE = "unavailable"

WAKE_SECONDS = 60
POLL_SECONDS = 10
DEFAULT_TIMEOUT_MINUTES = 20


@dataclass(frozen=True)
class Record:
    """One pool's record, as the status and the wake give it; `cause` when the state is `unavailable`."""

    model: str
    state: str
    cause: str | None = None


def _record(body: Any) -> Record | None:
    if not isinstance(body, dict):
        return None
    model, state, cause = body.get("model"), body.get("state"), body.get("cause")
    if not isinstance(model, str) or not isinstance(state, str):
        return None
    return Record(model, state, cause if isinstance(cause, str) and state == UNAVAILABLE else None)


@dataclass(frozen=True)
class _Gateway:
    """The stored endpoint, with the stored key on every call."""

    address: str
    key: str
    transport: login.Transport

    def call(self, method: str, path: str, fields: dict[str, str] | None = None) -> Any:
        """The body of the gateway's 200; `LoginError`, with the gateway's cause, for any other answer."""
        headers = {"Authorization": f"Bearer {self.key}"}
        status, body = self.transport(login.Call(method, self.address + path, fields, headers=headers))
        if status != 200:
            raise login.LoginError(f"the endpoint refused the call: {login.refusal(status, body)}")
        return body

    def records(self) -> list[Record]:
        """The record of every pool the status lists."""
        body = self.call("GET", STATUS_PATH)
        pools = body.get("pools") if isinstance(body, dict) else None
        records = [_record(pool) for pool in pools] if isinstance(pools, list) else None
        listed = [record for record in records or [] if record is not None]
        if records is None or len(listed) != len(records):
            raise login.LoginError("the endpoint's status is not a list of pools.")
        return listed

    def wake(self, model: str) -> Record:
        """Ask for the pool `model` names; its record."""
        record = _record(self.call("POST", WAKE_PATH, {"model": model}))
        if record is None:
            raise login.LoginError("the endpoint answered the wake with no record of the pool.")
        return record


def _gateway(transport: login.Transport) -> _Gateway:
    address, key = login.signed_in()
    return _Gateway(address, key, transport)


def status(*, transport: login.Transport = login.http_transport, out: TextIO | None = None) -> None:
    """Print each pool's model name, state and, when it is unavailable, cause; `LoginError` when it fails."""
    out = out or sys.stdout
    records = _gateway(transport).records()
    width = max((len(record.model) for record in records), default=0)
    for record in records:
        line = f"{record.model:<{width}}  {record.state}"
        print(f"{line}  {record.cause}" if record.cause else line, file=out)


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
    next_wake = clock()
    # The pool's own model name, which the status lists: known once the first wake has answered.
    name, printed = model, None

    def late() -> login.LoginError:
        return login.LoginError(
            f"{name} is not ready after {timeout_minutes:g} minutes. Run rhi wait again, or tell the instructor."
        )

    while True:
        now = clock()
        if now >= deadline:
            raise late()
        if now >= next_wake:
            record = gateway.wake(model)
            next_wake = now + WAKE_SECONDS
            name = record.model
        else:
            listed = [record for record in gateway.records() if record.model == name]
            if not listed:
                raise login.LoginError(f"the endpoint's status no longer lists {name}.")
            record = listed[0]
        if clock() >= deadline:  # the answer took the wait past its timeout: whatever it says, it came too late
            raise late()
        if record.state != printed:
            print(f"{name}: {record.state}", file=out, flush=True)
            printed = record.state
        if record.state == READY:
            return
        if record.state == UNAVAILABLE:
            raise login.LoginError(f"{name} cannot be started now: {record.cause or 'no cause was given'}")
        now = clock()
        sleep(max(0.0, min(POLL_SECONDS, next_wake - now, deadline - now)))


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
    """Wake the pool `model` names until it is ready; the command's exit status.

    0 once the pool is ready. 1, with the cause on `err`, when the pool is unavailable, when the
    gateway refuses a call, and when `timeout_minutes` have passed.
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

    parser = commands.add_parser("wait", help="start a pool and wait until it is ready")
    parser.add_argument("model", help="the model name of the pool, as rhi status prints it")
    parser.add_argument(
        "--timeout",
        type=_minutes,
        default=DEFAULT_TIMEOUT_MINUTES,
        metavar="MINUTES",
        help=f"how long to wait before giving up, in minutes ({DEFAULT_TIMEOUT_MINUTES} by default)",
    )
    parser.set_defaults(run=_run_wait)
