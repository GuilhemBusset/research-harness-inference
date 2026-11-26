"""A trial's forwarder, its shared budget and its supervisor
(docs/design/agent-session-evaluation.md, "Course quality and the grader").

A trial reaches the endpoint only through a forwarder of its own, on the operator's side. The
forwarder is the agent tree's one way to the gateway, so what it counts, it counts for the whole
tree: the main loop, its subagents and their side queries, however many run at once. It behaves as
the forwarder of the spikes does (`spikes/forwarder.py`), and adds what a trial needs.

**One gateway, one pool, one student** (`Forwarder`). Every request goes to the gateway address the
forwarder was built with, and only the requests the gateway serves are sent on: the three routes
that generate (Messages, Responses and Chat Completions), a token count and the list of models.
Anything else is answered here. Every body that is sent on carries the pool's name as its `model`,
whatever the trial named, the token count's included, so no request reaches another pool. The
forwarder drops whatever credential the trial sent and writes the trial's student key itself. The
key is held in the forwarder's memory and nowhere else: the trial is given the forwarder's address
and a placeholder (`trial_environment`), in an environment built from a short list and nothing
inherited from the driver. The answer goes back chunk by chunk as it arrives, a stream of events
or, for a request that asked for no stream, one JSON document.

What the forwarder keeps from a trial is what it never hands over. What a process may read on the
host is not its to bound: a trial runs in a container that mounts its task files, its home and its
output directory and nothing of the driver's (the design, "Course quality and the grader"), and the
command the supervisor runs is then that container's.

**The limits** (`Budget`) are those of the plan's `contracts.Limits`: a wall time, model turns and
generated tokens. A request is admitted in one step, under one lock, or refused without reaching
the gateway:

- past the deadline, it is refused `timeout`;
- a new model turn when every turn is taken is refused `turn_limit`;
- an admitted request holds an *allowance* of generated tokens until it ends: what it asked for, at
  most what is left once the tokens already charged and the allowances still held are taken from
  the cap. A request that declares no output limit is allowed all that is left. The forwarder
  writes the allowance as the request's output limit, so the endpoint stops the answer there. A
  Chat Completions request that asks for several choices holds the allowance of each of them. The
  tokens charged and the allowances held never sum past the cap, so overlapping calls cannot
  generate past it together;
- when the tokens charged leave less than one token for each choice, the request is refused
  `token_limit`. When tokens are left but requests still in flight hold them all, the request
  waits for one of them to end, as long as its client does: an allowance is given back for what
  was not generated, and a trial within its limits is never failed for what another call only
  held. A driver that wants calls to overlap however much they declare gives the budget a
  per-answer limit (`request_tokens`); the plan has none.

A refusal at a limit ends the trial: the limit is its failure, and nothing is admitted after it.

**Turns, side calls and retries.** A request on a generating route is a model turn unless the
harness is known to send side queries and this is one. Claude Code, OpenCode and Pi send them, and
theirs declare no tool (`spikes/s3_harness_task.py`, "The turns"); Codex and the SDK tool loop send
none, so every request of theirs is a turn, tools or not. A request sent again with the same body
after an attempt of it failed is a retry of that attempt: it takes no second turn, and is counted
as a retry. One failed attempt is retried by one request: a second request with the same body, at
the same time or after an answer, is a new request and takes its own turn.

**What is measured and what is charged** are read apart, each by a reader of `evaluate.streams`.
The measurement is the answer as it reached the trial: a chunk counts once it was written to the
trial, so an answer whose last event the trial did not receive is a failed attempt, closed by the
client. The charge is read from what the gateway sent, and settles the allowance exactly once:

- nothing, when the gateway refused the request with a status or it never left the forwarder;
- the output tokens the answer reported, when the gateway sent it whole and it reported them;
- its whole allowance otherwise: an answer that reported no usage, one cut short by the endpoint,
  by the client or by the supervisor. What it generated is not known and is at most the allowance,
  so the count is never guessed low. Its usage stays recorded as missing.

An endpoint that reports more than the allowance it was given is charged what it reports, and a
total past the cap is `token_limit`.

**The supervisor** (`Supervisor`) runs the trial's process and watches the budget. On a limit, an
interruption or the process's end it closes admission, which fails a trial whose deadline has
passed even if its process has just ended, and cancels every exchange in flight: both sides are
shut and its allowance is settled then, whether or not it has reached the gateway yet. It stops the
process and its process group, first asked then killed, within `CANCEL_S` seconds in all. A process
that outlives that is a driver error: the accounting is not whole.

**What is kept** (`Evidence`, `Measurement`) holds labels, counts and times in milliseconds on the
run's clock: each attempt as `evaluate.streams` reads it, whether it was a side call, its retry
number, its allowance and its charge. No body, header, key, address or path is kept: a request's
body is read for its model, its tools, its choices and its output limit, hashed to tell a retry,
and dropped. The reasons are those of `contracts.FailureReason` (`timeout`, `turn_limit`,
`token_limit`, and `endpoint_failure` when the process ended in failure with a turn the endpoint
left unanswered) and `contracts.InvalidReason` (`driver_error`, `interrupted`).
`Evidence.trial_record` writes a trial that crossed a limit as failed whatever its grade says.

It needs the standard library, `evaluate.contracts` and `evaluate.streams`.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import select
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Self

from evaluate.contracts import (
    FailureReason,
    Harness,
    InvalidReason,
    RequestOutcome,
    RequestRecord,
    Route,
    Traffic,
    TrialOutcome,
    TrialRecord,
    Usage,
)
from evaluate.streams import Attempts, StreamReader

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from evaluate.contracts import Grade, Limits
    from evaluate.streams import Attempt, Tally

# The routes that generate, by the path a harness sends them on.
ROUTES = {
    "/v1/messages": Route.MESSAGES,
    "/v1/responses": Route.RESPONSES,
    "/v1/chat/completions": Route.CHAT_COMPLETIONS,
}
# The requests that generate nothing and are sent on all the same: a token count, the list of models.
PASSED = frozenset({("POST", "/v1/messages/count_tokens"), ("GET", "/v1/models")})
# The fields that hold a request's output limit, the first being the one written when it has none.
OUTPUT_LIMITS = {
    Route.MESSAGES: ("max_tokens",),
    Route.RESPONSES: ("max_output_tokens",),
    Route.CHAT_COMPLETIONS: ("max_completion_tokens", "max_tokens"),
}
# The harnesses that send side queries, which declare no tool. Any other request is a turn.
SIDE_CALLS_DECLARE_NO_TOOLS = frozenset({Harness.CLAUDE_CODE, Harness.OPENCODE, Harness.PI})
# Seconds: the longest a forwarded exchange may be silent.
UPSTREAM_TIMEOUT = 20 * 60
# Seconds: the longest a cancellation takes, and within it how long a process is given to end when asked.
CANCEL_S = 5.0
TERMINATE_S = 2.0
# Seconds: how often a request that waits for tokens others hold looks at the clock and at its client.
HELD_POLL_S = 0.05
# What the trial is given in place of a key: the forwarder drops it and writes the student's.
PLACEHOLDER_KEY = "held-by-the-forwarder"
# The variables of the driver's environment a trial's environment keeps: none holds a credential.
INHERITED = ("PATH", "LANG", "LC_ALL", "TZ")

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)
# What the trial sent as its identity: none of it is passed on.
_CREDENTIALS = frozenset({"authorization", "x-api-key", "cookie"})


def run_clock() -> Callable[[], int]:
    """A clock of a run: the milliseconds since this call, never going back."""
    origin = time.monotonic()
    return lambda: int((time.monotonic() - origin) * 1000)


def trial_environment(endpoint: str, home: Path) -> dict[str, str]:
    """The whole environment of a trial whose home is `home`: the forwarder's address under the
    names the harnesses and the SDKs read, a placeholder where they want a key, and of the driver's
    own environment only `INHERITED`."""
    kept = {name: os.environ[name] for name in INHERITED if name in os.environ}
    return {
        **kept,
        "HOME": str(home),
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_API_KEY": PLACEHOLDER_KEY,
        "OPENAI_BASE_URL": endpoint + "/v1",
        "OPENAI_API_KEY": PLACEHOLDER_KEY,
    }


# The budget.


class Refused(Exception):
    """A request the budget does not admit: `reason` is the limit, or `None` when it is no limit's
    doing (the trial is over, or the request's client gave up waiting)."""

    def __init__(self, reason: FailureReason | None) -> None:
        super().__init__(reason or "closed")
        self.reason = reason


class Reservation:
    """What an admitted request holds until it ends: its allowance of generated tokens."""

    __slots__ = ("settled", "tokens")

    def __init__(self, tokens: int) -> None:
        self.tokens = tokens
        self.settled = False


class Budget:
    """The limits of one trial, shared by every request of its agent tree. `clock` gives the
    milliseconds of the run; the trial starts when the budget is made. `request_tokens`, when
    given, is the most any one answer is allowed."""

    def __init__(self, limits: Limits, clock: Callable[[], int], *, request_tokens: int | None = None) -> None:
        if request_tokens is not None and not 1 <= request_tokens <= limits.trial_generated_tokens:
            raise ValueError("an answer's allowance is from 1 token to the trial's cap")
        self.limits = limits
        self.clock = clock
        self.request_tokens = request_tokens
        self.started_ms = clock()
        self.deadline_ms = self.started_ms + limits.trial_wall_time_ms
        self._changed = threading.Condition()
        self._turns = self._charged = self._reserved = self._open = 0
        self._failure: FailureReason | None = None
        self._closed = False

    def _fail(self, reason: FailureReason) -> FailureReason:
        """The limit the trial crossed: the first one, which no later one replaces."""
        self._failure = self._failure or reason
        return self._failure

    def _expire(self) -> None:
        if not self._closed and self.clock() >= self.deadline_ms:
            self._fail(FailureReason.TIMEOUT)

    def admit(
        self, *, turn: bool, wanted: int | None = None, choices: int = 1, patient: Callable[[], bool] | None = None
    ) -> Reservation:
        """Admits a request for `choices` answers of `wanted` generated tokens each, a new model
        turn when `turn`, or refuses it: its turn and its allowance are taken together or not at
        all. While requests in flight hold every token that is left, it waits for one to end,
        unless `patient` says the request no longer does."""
        cap = self.limits.trial_generated_tokens
        with self._changed:
            while True:
                self._expire()
                if self._closed or self._failure is not None:
                    raise Refused(self._failure)
                if turn and self._turns >= self.limits.trial_model_turns:
                    raise Refused(self._fail(FailureReason.TURN_LIMIT))
                if cap - self._charged < choices:
                    raise Refused(self._fail(FailureReason.TOKEN_LIMIT))
                each = (cap - self._charged - self._reserved) // choices
                if each >= 1:
                    break
                if patient is not None and not patient():
                    raise Refused(None)
                self._changed.wait(HELD_POLL_S)
            declared = [bound for bound in (wanted, self.request_tokens) if bound is not None and bound >= 1]
            tokens = min([each, *declared]) * choices
            self._turns += turn
            self._reserved += tokens
            self._open += 1
            return Reservation(tokens)

    def settle(self, reservation: Reservation, tokens: int) -> None:
        """Ends a reservation and charges what its request generated: once, however often asked."""
        with self._changed:
            if reservation.settled:
                return
            reservation.settled = True
            self._reserved -= reservation.tokens
            self._open -= 1
            self._charged += tokens
            if self._charged > self.limits.trial_generated_tokens:
                self._fail(FailureReason.TOKEN_LIMIT)
            self._changed.notify_all()

    def close(self) -> None:
        """The trial is over: a deadline that has passed by now is its failure, nothing is admitted
        any more, and the deadline no longer runs."""
        with self._changed:
            self._expire()
            self._closed = True
            self._changed.notify_all()

    @property
    def failure(self) -> FailureReason | None:
        """The limit the trial crossed, the deadline included; `None` while it is within them all."""
        with self._changed:
            self._expire()
            return self._failure

    @property
    def model_turns(self) -> int:
        return self._turns

    @property
    def generated_tokens(self) -> int:
        """The tokens charged so far."""
        return self._charged

    @property
    def reserved_tokens(self) -> int:
        """The allowances held by requests that have not ended."""
        return self._reserved

    @property
    def open_reservations(self) -> int:
        return self._open


# What is kept.


@dataclass(frozen=True, kw_only=True)
class Measurement:
    """One attempt the forwarder sent to the gateway, and what it cost the trial."""

    attempt: Attempt
    """The attempt as the trial received it: an answer whose end did not reach the trial failed."""
    received_ms: int
    """When the request reached the forwarder, before it was admitted."""
    side_call: bool
    closed_by_client: bool
    """The trial closed the exchange before its answer was whole."""
    cancelled: bool
    """The supervisor cut the exchange."""
    allowance_tokens: int
    charged_tokens: int

    def request_record(
        self, *, request: int, cell: int, student: int, conversation: int, turn: int, trial: int
    ) -> RequestRecord:
        """The attempt as a record of the run, under the ordinals the driver gives it: live traffic."""
        return RequestRecord(
            request=request,
            cell=cell,
            student=student,
            conversation=conversation,
            turn=turn,
            traffic=Traffic.LIVE,
            trial=trial,
            eligible_ms=self.received_ms,
            finish_reason=None,
            **self.attempt.measured(),
        )


def _total(counts: Sequence[int | None]) -> int | None:
    """The sum of the counts that were reported; `None` when none was."""
    reported = [count for count in counts if count is not None]
    return sum(reported) if reported else None


@dataclass(frozen=True, kw_only=True)
class Evidence:
    """What a trial's forwarder and supervisor observed of it."""

    started_ms: int
    ended_ms: int
    failure: FailureReason | None
    invalid: InvalidReason | None
    model_turns: int
    side_calls: int
    retries: int
    refusals: int
    """Requests the forwarder refused at a limit: none of them reached the gateway."""
    generated_tokens: int
    """The tokens charged against the cap: reported ones, and the allowance where none was known."""
    open_reservations: int
    tally: Tally
    requests: tuple[Measurement, ...]

    @property
    def may_pass(self) -> bool:
        """Whether a grader's acceptance can still make this trial a pass."""
        return self.failure is None and self.invalid is None

    @property
    def usage(self) -> Usage | None:
        """The tokens the trial's attempts reported, summed count by count; nothing is guessed for
        an attempt that reported none (`tally.usage_missing` counts those)."""
        reported = [one.attempt.usage for one in self.requests if one.attempt.usage is not None]
        whole = _total([usage.input_tokens for usage in reported])
        cached = _total([usage.cached_input_tokens for usage in reported])
        output = _total([usage.output_tokens for usage in reported])
        if whole is None and cached is None and output is None:
            return None
        if whole is not None and cached is not None and cached > whole:
            cached = None
        return Usage(input_tokens=whole, cached_input_tokens=cached, output_tokens=output)

    def trial_record(
        self, *, execution: int, trial: int, student: int, grade: Grade | None, replaces: int | None = None
    ) -> TrialRecord:
        """The trial as a record of the run. A broken measurement is invalid and a crossed limit is
        a failure, whatever `grade` says; within the limits the grade decides, and is needed."""
        failure, kept = self.failure, grade
        if self.invalid is not None:
            outcome, failure, kept = TrialOutcome.INVALID, None, None
        elif failure is not None:
            outcome = TrialOutcome.FAILED
            kept = None if grade is None or grade.accepted else grade
        elif grade is None:
            raise ValueError("a trial within its limits needs its grade")
        elif grade.accepted:
            outcome = TrialOutcome.PASSED
        else:
            outcome, failure = TrialOutcome.FAILED, FailureReason.REJECTED
        return TrialRecord(
            execution=execution,
            trial=trial,
            replaces=replaces,
            student=student,
            started_ms=self.started_ms,
            ended_ms=self.ended_ms,
            outcome=outcome,
            failure=failure,
            invalid=self.invalid,
            grade=kept,
            model_turns=self.model_turns,
            retries=self.retries,
            usage=self.usage,
        )


# The forwarder.


def _object(body: bytes) -> dict[str, Any] | None:
    try:
        held = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return held if isinstance(held, dict) else None


def _encoded(held: Mapping[str, Any]) -> bytes:
    return json.dumps(held, separators=(",", ":")).encode()


def _wanted(route: Route, held: Mapping[str, Any]) -> int | None:
    """The output limit a request declares: the smallest, when it declares it twice."""
    declared = [held.get(name) for name in OUTPUT_LIMITS[route]]
    whole = [value for value in declared if type(value) is int and value >= 1]
    return min(whole) if whole else None


def _choices(route: Route, held: Mapping[str, Any]) -> int | None:
    """How many answers a request asks for, each generated up to the output limit: `n` on Chat
    Completions, one anywhere else. `None` for a number that cannot be read."""
    asked = held.get("n") if route is Route.CHAT_COMPLETIONS else None
    if asked is None:
        return 1
    return asked if type(asked) is int and asked >= 1 else None


def _directed(route: Route, held: Mapping[str, Any], pool: str, tokens: int) -> bytes:
    """The request as it is sent on: the pool as its model, and `tokens` as its output limit."""
    names = OUTPUT_LIMITS[route]
    limits = {name: tokens for name in names if name in held} or {names[0]: tokens}
    return _encoded({**held, "model": pool, **limits})


def _charge(generated: Attempt, allowance: int, *, reached: bool) -> int:
    """What a request is charged, from the answer as the gateway sent it (the module's text)."""
    if not reached or generated.outcome is RequestOutcome.HTTP_REFUSAL:
        return 0
    reported = None if generated.usage is None else generated.usage.output_tokens
    if generated.outcome is RequestOutcome.COMPLETED and reported is not None:
        return reported
    return max(allowance, reported or 0)


def _gone(client: socket.socket) -> bool:
    """Whether the other side has closed its connection."""
    try:
        readable, _, _ = select.select([client], [], [], 0)
        return bool(readable) and not client.recv(1, socket.MSG_PEEK)
    except (OSError, ValueError):
        return True


@dataclass(frozen=True, kw_only=True)
class _Sent:
    """What an admitted request holds until it ends."""

    reservation: Reservation
    delivered: StreamReader
    """Reads the answer as it reached the trial: the measurement."""
    generated: StreamReader
    """Reads the answer as the gateway sent it: the charge."""
    digest: bytes
    retry: int
    side_call: bool
    received_ms: int


class _Exchange:
    """One request in flight: the two connections a cancellation shuts, and how it ended."""

    def __init__(self, client: socket.socket, upstream: http.client.HTTPConnection, *, admitting: bool) -> None:
        self.client, self.upstream = client, upstream
        # The gateway's socket, kept from the connection's start: `http.client` lets go of its own
        # as soon as an answer says the connection closes after it, while the answer is still read.
        self.gateway: socket.socket | None = None
        self.admitting = admitting
        self.sent: _Sent | None = None
        self.reached = False
        self.broke = False
        self.closed_by_client = False
        self.cancelled = False
        self.ended = False

    def shut(self, *, client: bool) -> None:
        for held in (self.gateway, self.client if client else None):
            with contextlib.suppress(OSError):
                if held is not None:
                    held.shutdown(socket.SHUT_RDWR)


class Forwarder:
    """The local HTTP server one trial sends its requests to. It serves inside its `with` block,
    for that one trial: once aborted it sends nothing on.

    `upstream` is the gateway's address, `pool` the model every request is sent to, `key` the
    student's key and `harness` the trial's harness, which says what a side call is."""

    def __init__(self, upstream: str, *, pool: str, key: str, harness: Harness, budget: Budget) -> None:
        parts = urllib.parse.urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the gateway's address is not an http or https address")
        self._parts = parts
        self._pool, self._key, self._harness = pool, key, harness
        self.budget = budget
        # Guards everything below, and each exchange's readers and flags. Taken before the budget's.
        self._changed = threading.Condition()
        self._attempts = Attempts()
        self._measurements: list[Measurement] = []
        # The failed attempts no request has retried yet, by the digest of their request: for each,
        # how often the request was sent, and whether it is a turn the endpoint left unanswered.
        self._failed: dict[bytes, list[tuple[int, bool]]] = {}
        self._side_calls = self._refusals = 0
        self._in_flight: set[_Exchange] = set()
        self._aborted = False
        forwarder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                forwarder._serve(self)

            def do_POST(self) -> None:
                forwarder._serve(self)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)

    @property
    def url(self) -> str:
        """The forwarder's address, which the trial is given as the endpoint's."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    # What it observed.

    def evidence(self, *, invalid: InvalidReason | None = None, exit_status: int | None = None) -> Evidence:
        """What was observed so far, as the trial's evidence. `invalid` is what broke the
        measurement, if anything did, and `exit_status` how the trial's process ended, if it has."""
        budget, failure = self.budget, self.budget.failure
        with self._changed:
            unanswered = any(left for owed in self._failed.values() for _, left in owed)
            if failure is None and exit_status and unanswered:
                failure = FailureReason.ENDPOINT_FAILURE
            tally = self._attempts.tally()
            return Evidence(
                started_ms=budget.started_ms,
                ended_ms=budget.clock(),
                failure=failure,
                invalid=invalid,
                model_turns=budget.model_turns,
                side_calls=self._side_calls,
                retries=tally.retries,
                refusals=self._refusals,
                generated_tokens=budget.generated_tokens,
                open_reservations=budget.open_reservations,
                tally=tally,
                requests=tuple(self._measurements),
            )

    # Cancelling.

    def abort(self) -> None:
        """Ends the trial's traffic: the budget is closed, every exchange in flight is shut on both
        sides and settled now, wherever it is, and none begins."""
        self.budget.close()
        with self._changed:
            self._aborted = True
            for exchange in tuple(self._in_flight):
                exchange.cancelled = True
                exchange.shut(client=True)
                # One that is still being admitted holds nothing yet: the closed budget ends it.
                if not exchange.admitting:
                    self._end(exchange)

    def drained(self, timeout: float) -> bool:
        """Waits until no exchange is in flight, `timeout` seconds at most, and says whether none is."""
        with self._changed:
            return self._changed.wait_for(lambda: not self._in_flight, max(0.0, timeout))

    # Serving.

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        handler.close_connection = True
        try:
            body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
        except (OSError, ValueError):
            return
        path = urllib.parse.urlsplit(handler.path).path
        route = ROUTES.get(path) if handler.command == "POST" else None
        if route is None and (handler.command, path) not in PASSED:
            self._refuse(handler, None, HTTPStatus.NOT_FOUND)
            return
        if handler.command == "POST":
            held = _object(body)
            if held is None:
                # Neither its model nor its output limit can be written: it is not sent on.
                self._refuse(handler, None, HTTPStatus.BAD_REQUEST)
                return
            if route is not None:
                self._generate(handler, route, held, hashlib.sha256(route.value.encode() + b"\0" + body).digest())
                return
            body = _encoded({**held, "model": self._pool})
        self._pass(handler, body)

    def _pass(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        """A request that generates nothing (a token count, the list of models): sent on while the
        trial runs, with the student's key, and not measured."""
        failure = self.budget.failure
        exchange = None if failure is not None else self._begin(handler, admitting=False)
        if exchange is None:
            self._refuse(handler, failure)
            return
        try:
            self._relay(handler, exchange, body)
        finally:
            self._end(exchange)
            exchange.upstream.close()

    def _generate(self, handler: BaseHTTPRequestHandler, route: Route, held: Mapping[str, Any], digest: bytes) -> None:
        received = self.budget.clock()
        choices = _choices(route, held)
        if choices is None:
            self._refuse(handler, None, HTTPStatus.BAD_REQUEST)
            return
        side_call = self._harness in SIDE_CALLS_DECLARE_NO_TOOLS and not held.get("tools")
        # In flight from before its admission to after its settlement: once nothing is in flight,
        # nothing holds a reservation.
        exchange = self._begin(handler, admitting=True)
        if exchange is None:
            self._refuse(handler, None)
            return
        client = exchange.client
        with self._changed:
            # The failed attempt this request retries is taken with it: no other request retries it.
            owed = self._failed.get(digest)
            retry, unanswered = owed.pop() if owed else (0, False)
        reservation: Reservation | None = None
        reason: FailureReason | None = None
        try:
            reservation = self.budget.admit(
                turn=not side_call and not retry,
                wanted=_wanted(route, held),
                choices=choices,
                patient=lambda: not _gone(client),
            )
        except Refused as refused:
            reason = refused.reason
        with self._changed:
            exchange.admitting = False
            if reservation is not None and exchange.cancelled:
                # Cut while it was admitted: it is not sent, and holds nothing.
                self.budget.settle(reservation, 0)
                reservation = None
            if reservation is None:
                if retry:
                    self._failed.setdefault(digest, []).append((retry, unanswered))
                self._refusals += reason is not None
            else:
                self._side_calls += side_call and not retry
                now = self.budget.clock()
                exchange.sent = _Sent(
                    reservation=reservation,
                    delivered=self._attempts.send(route, now, retry=retry),
                    generated=StreamReader(route, now, retry=retry),
                    digest=digest,
                    retry=retry,
                    side_call=side_call,
                    received_ms=received,
                )
        try:
            if reservation is None:
                self._refuse(handler, reason)
            else:
                self._relay(handler, exchange, _directed(route, held, self._pool, reservation.tokens // choices))
        finally:
            self._end(exchange)
            exchange.upstream.close()

    def _end(self, exchange: _Exchange) -> None:
        """Ends an exchange, once, whoever asks first: the request's own thread when its answer is
        over, or a cancellation. Its attempt is kept and its allowance settled."""
        with self._changed:
            if exchange.ended:
                return
            exchange.ended = True
            self._in_flight.discard(exchange)
            sent = exchange.sent
            if sent is not None:
                now = self.budget.clock()
                cut = exchange.closed_by_client or exchange.cancelled
                attempt = sent.delivered.end(now, transport_error=exchange.broke and not cut)
                charged = _charge(sent.generated.end(now), sent.reservation.tokens, reached=exchange.reached)
                self.budget.settle(sent.reservation, charged)
                if attempt.failed:
                    self._failed.setdefault(sent.digest, []).append((sent.retry + 1, not sent.side_call and not cut))
                self._measurements.append(
                    Measurement(
                        attempt=attempt,
                        received_ms=sent.received_ms,
                        side_call=sent.side_call,
                        closed_by_client=exchange.closed_by_client and attempt.failed,
                        cancelled=exchange.cancelled and attempt.failed,
                        allowance_tokens=sent.reservation.tokens,
                        charged_tokens=charged,
                    )
                )
            self._changed.notify_all()

    def _refuse(
        self, handler: BaseHTTPRequestHandler, reason: FailureReason | None, status: int = HTTPStatus.TOO_MANY_REQUESTS
    ) -> None:
        """The forwarder's own answer to a request it does not send on: a label, never a text."""
        label = reason.value if reason is not None else "not_forwarded"
        payload = json.dumps({"type": "error", "error": {"type": label, "code": label, "message": label}}).encode()
        with contextlib.suppress(OSError):
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)

    def _begin(self, handler: BaseHTTPRequestHandler, *, admitting: bool) -> _Exchange | None:
        """An exchange with the gateway, not connected yet, counted in flight until `_end`; `None`
        once the forwarder is aborted."""
        host, port = self._parts.hostname or "", self._parts.port
        upstream: http.client.HTTPConnection
        if self._parts.scheme == "https":
            context = ssl.create_default_context()
            upstream = http.client.HTTPSConnection(host, port, timeout=UPSTREAM_TIMEOUT, context=context)
        else:
            upstream = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT)
        exchange = _Exchange(handler.connection, upstream, admitting=admitting)
        with self._changed:
            if self._aborted:
                return None
            self._in_flight.add(exchange)
        return exchange

    def _headers(self, handler: BaseHTTPRequestHandler, length: int) -> dict[str, str]:
        """The trial's headers without the hop's own and without its credential, and the student's key."""
        dropped = _HOP_BY_HOP | _CREDENTIALS
        headers = {name: value for name, value in handler.headers.items() if name.lower() not in dropped}
        return {
            **headers,
            "Content-Length": str(length),
            "Accept-Encoding": "identity",
            "Authorization": "Bearer " + self._key,
        }

    def _relay(self, handler: BaseHTTPRequestHandler, exchange: _Exchange, body: bytes) -> None:
        """Sends the request on and the answer back as it arrives. Each chunk is read for the
        charge when the gateway sent it, and for the measurement once it was written to the trial."""
        done = threading.Event()
        # Which side the exchange is at: an error while writing to the trial is the trial closing
        # it; one while reading the gateway is the gateway's, unless the exchange was cut.
        to_client = False
        upstream, sent = exchange.upstream, exchange.sent
        try:
            upstream.connect()
            with self._changed:
                exchange.gateway = upstream.sock
                if exchange.cancelled:
                    # Cut before it was connected: it is not sent.
                    raise ConnectionAbortedError
                # From here the gateway may hold the request, whenever the exchange is cut.
                exchange.reached = True
            threading.Thread(target=self._watch, args=(exchange, done), daemon=True).start()
            path = self._parts.path.rstrip("/") + handler.path
            try:
                upstream.request(handler.command, path, body, self._headers(handler, len(body)))
            except OSError:
                # Not sent whole: the gateway answers nothing of it.
                with self._changed:
                    exchange.reached = False
                raise
            answer = upstream.getresponse()
            stream = (answer.getheader("Content-Type") or "").lower().startswith("text/event-stream")
            if sent is not None:
                with self._changed:
                    sent.generated.answered(answer.status, stream=stream)
            to_client = True
            handler.send_response(answer.status)
            for name, value in answer.getheaders():
                if name.lower() not in _HOP_BY_HOP:
                    handler.send_header(name, value)
            if answer.length is not None:
                # The length the gateway declared, so that the trial too sees an answer cut short.
                handler.send_header("Content-Length", str(answer.length))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.flush()
            if sent is not None:
                with self._changed:
                    sent.delivered.answered(answer.status, stream=stream)
            while True:
                to_client = False
                chunk = answer.read1(65536)
                if not chunk:
                    break
                now = self.budget.clock()
                if sent is not None:
                    with self._changed:
                        sent.generated.feed(chunk, now)
                to_client = True
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if sent is not None:
                    with self._changed:
                        sent.delivered.feed(chunk, now)
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            # `AttributeError`: a connection shut under a read is left without its socket.
            if to_client:
                exchange.closed_by_client = not exchange.cancelled
            else:
                exchange.broke = True
        finally:
            done.set()

    def _watch(self, exchange: _Exchange, done: threading.Event) -> None:
        """Notes when the trial closes the exchange before its end, and closes the gateway's side
        then, as a client that gives up does."""
        while not done.wait(0.05):
            if _gone(exchange.client):
                if not done.is_set() and not exchange.cancelled:
                    exchange.closed_by_client = True
                    exchange.shut(client=False)
                return


# The supervisor.


class Supervisor:
    """Runs one trial's process against its forwarder and holds it to the budget. A cancellation
    takes `cancel_s` seconds at most, of which a process asked to end is given `terminate_s`."""

    def __init__(
        self,
        forwarder: Forwarder,
        *,
        cancel_s: float = CANCEL_S,
        terminate_s: float = TERMINATE_S,
        poll_s: float = 0.02,
    ) -> None:
        self.forwarder = forwarder
        self._cancel_s, self._terminate_s, self._poll_s = cancel_s, terminate_s, poll_s
        self._interrupted = threading.Event()

    def interrupt(self) -> None:
        """Stops the trial from outside: it is then invalid, `interrupted`."""
        self._interrupted.set()

    def run(self, command: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> Evidence:
        """Runs `command` as the trial, in a process group of its own with `env` as its whole
        environment, until it ends, crosses a limit or is interrupted; then cancels what is left
        and gives the evidence. The forwarder must be serving."""
        budget = self.forwarder.budget
        invalid: InvalidReason | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(command),
                env=dict(env),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # The process's end is seen when it comes, not a poll later: closing the budget then
            # tells a trial that ended within its wall time from one that outlived it.
            while budget.failure is None and not self._interrupted.is_set():
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(self._poll_s)
                    break
            if self._interrupted.is_set():
                invalid = InvalidReason.INTERRUPTED
        except OSError:
            invalid = InvalidReason.DRIVER_ERROR
        except KeyboardInterrupt:
            invalid = InvalidReason.INTERRUPTED
        # The cancellation, bounded: admission closes, the exchanges are shut and settled, the
        # process is stopped.
        until = time.monotonic() + self._cancel_s
        self.forwarder.abort()
        status = None if process is None else self._stop(process, until)
        drained = self.forwarder.drained(until - time.monotonic())
        stopped = process is None or status is not None
        if invalid is None and not (drained and stopped and not budget.open_reservations):
            # Something outlived the cancellation: the accounting is not whole.
            invalid = InvalidReason.DRIVER_ERROR
        return self.forwarder.evidence(invalid=invalid, exit_status=status)

    def _stop(self, process: subprocess.Popen[bytes], until: float) -> int | None:
        """Ends the process and everything in its group, asked first and killed `terminate_s`
        later, or at `until` if that comes sooner; its exit status, `None` if it outlived both."""
        for sent, wait in ((signal.SIGTERM, self._terminate_s), (signal.SIGKILL, self._cancel_s)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, sent)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(max(0.0, min(wait, until - time.monotonic())))
        return process.poll()
