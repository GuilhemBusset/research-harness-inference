"""Spike S2: does each client survive the hold, and how does it show a 402 (system design, sections
5.3, 6.2, 12 and 13)?

The hold is sized from the clients (section 5.3): Claude Code tolerates a stream of pings for about
12 minutes; Codex's idle timer ignores SSE comments and stops a stream after 300 seconds by default,
which is why `rhi login` writes a profile with a 15-minute stream timeout (`rhi_cli.harness`); the
SDKs time a whole call out at 10 minutes. How each client behaves through a full hold is untested
(evidence E12). This job asks it of the mock pool. **Pass:** no client aborts with the profile in
place. Codex with its default timer is run too, to show what the profile is for: its abort does not
fail the run.

**The clients** (`Client`): Claude Code and Codex at the versions `spikes/harnesses.py` pins and
installs, Codex once with its default timer and once with the profile `rhi login` writes, and a
script of each SDK of the group `spikes`, with the SDK's own timeout and retries, as a student's
program would call the endpoint. The harnesses stream every turn; each SDK script makes a stream
and a non-streaming call (`KINDS`).

**How a client is run.** Each run of a client has a home of its own, holding what `rhi login`
writes there (`rhi_cli.harness`): Claude Code's settings, Codex's profile `rhi` (for Codex with its
default timer, the same profile without `stream_idle_timeout_ms`), and the key in the CLI's key
file. `rhi token`, which both harnesses run for the key, is a command on the client's `PATH` that
runs the CLI of this repository from the job's environment, since the job installs the repository's
libraries and not the CLI's entry point. An SDK script is given its key as the SDK's own variable.

Every client reaches the gateway through a forwarder of its own (`Forwarder`), a local HTTP server
that sends each request on to the gateway and the answer back as it arrives, and records each
exchange: when it was sent, its status and cause, whether the whole answer reached the client, and
when the client closed it before its end. The forwarder writes the mock pool's name as the `model`
of every request it forwards, so that no request reaches another pool: a `claude-` name would reach
the default pool, a GPU pool (section 6.2). The clients are also told to ask for that name, so the
rewrite changes nothing they do.

A client's **request** is the first one it sends on its inference route that carries the run's
marker, the random word of the prompt, and is of the kind measured (`intended`): a model list, a
token count or a request of another kind is not it. A **repeat** of it is a later request on the
same route with the same body (`repeats`).

**The hold.** The owner sets the mock pool's `start_delay_seconds` so that a cold start of the pool,
through the gateway, lasts just under the pool's `hold_seconds` (420 seconds by default): the
gateway then holds a stream that arrives at a cold pool for just under 7 minutes. The job waits
until the gateway's status says the pool is `cold`, starts the five streams at once, one canary
student each, and reads the status every `POLL_SECONDS` until the pool is `ready`. A client's hold
is the time from its request to the pool's readiness. For the non-streaming calls, whose deadline
is `hold_seconds_non_streaming` (240 seconds by default), the job waits for a cold pool again, wakes
it (`POST /wake`) and starts the two calls when the time left before readiness is `CALL_AIM_SECONDS`
under that deadline, the readiness foreseen from the cold start the job last measured (the time
from the first request, or the wake, to readiness).

An attempt **exercised the hold** when the client's request was held for less than its deadline
and no more than `BAND_SECONDS` less (`exercised`): one that reached an already ready pool, or one
the gateway ended at its deadline (the pool not ready in time), does not answer the question; nor
does one the gateway refused with an error instead of holding it (`refused`), even when another
client's request brought the pool to readiness in the band. A
client whose attempt did not exercise the hold is run again on the next cold start, up to
`ATTEMPTS` times; after that the run is not measured. So is a client that sent no request, or one
still running `CLIENT_LIMIT_SECONDS` after it started.

**What is recorded** for each client and kind of call (`HoldOutcome`): the client's version,
whether its request **completed** (the whole answer reached the client and the client exited
without error), and for one that did not, the **seconds before giving up**, from its request to the
moment it closed the request, or to the end of the exchange or of the process when it did not. A
request a client dropped and then sent again counts as given up, at the drop, whatever the repeat
did. A completed request has 0 seconds.

**The 402.** Each client then sends one request with the key of the canary student whose daily
budget is 0 (`rhi_common.spike_keys`), whom the gateway answers 402 with the cause `budget_reached`
on the first request (`RefusalOutcome`). The forwarder confirms that the client's request was
answered so; a client whose request was not, or that sent none, leaves the run not measured. The
job records whether the client **retried** it (a repeat of the request followed), and whether it
**showed** the user that the budget was spent: whether what it printed holds the cause's sentence
or its name (`rhi_common.causes`).

**The numbers** (`Number`), one per row of the record `docs/measurements/s2.md`: for each client and
kind of call, `<client>_<kind>_completed` (1 or 0) and `<client>_<kind>_seconds_before_giving_up`;
for each client, `<client>_402_retried` and `<client>_402_shown` (1 or 0). They are posted with the
verdict through `common.run_spike`; the versions are printed with the outcomes, for the record.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s2_client_hold`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output |
| `RHI_SPIKE_POOL` | The mock pool, `mock` unless given |

The pool's deadlines and its `start_delay_seconds` come from the deployed configuration
(`rhi.config.read_ssm`): a pool whose delay is 0, or that is not a mock pool, stops the job before
it sends anything. The canary students need five keys with the default budget, and the spent one.
Run it while nothing else sends to the pool: the job needs it to go cold between its attempts.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import secrets
import select
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import anthropic
import openai
from anthropic.types import MessageParam
from openai.types.chat import ChatCompletionMessageParam

import common
import harnesses
from rhi import config
from rhi_cli import harness as login_files
from rhi_cli import store
from rhi_common.causes import Cause

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from rhi_common.spike_keys import SpikeKeys

JOB = "s2_client_hold"
MEASUREMENT = "S2"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "mock"
MOCK_ENGINE = "mock"

# What an SDK script is given, besides its SDK's own variables.
PROMPT_VARIABLE = "RHI_S2_PROMPT"
MODEL_VARIABLE = "RHI_S2_MODEL"
SDK_FLAG = "--sdk"

# Seconds. The status is read this often while a hold lasts, and while the job waits for a cold pool.
POLL_SECONDS = 5
COLD_POLL_SECONDS = 30
# Seconds. The mock pool goes cold after its idle minutes and its stop: the job waits this long.
COLD_WAIT_SECONDS = 90 * 60
# Seconds. A hold exercised the question when it lasted less than its deadline and no more than this less.
BAND_SECONDS = 60
# Seconds. The non-streaming calls are started this much under their deadline before the readiness foreseen.
CALL_AIM_SECONDS = 20
# Seconds. A client still running this long after it started is stopped, and its attempt does not count.
CLIENT_LIMIT_SECONDS = 15 * 60
# How many cold starts a client is given to exercise the hold.
ATTEMPTS = 3
# The most tokens an SDK script asks for: the mock's canned turn is short.
MAX_TOKENS = 256
# Seconds: how long one read of the status may take, and the longest a forwarded exchange may be silent.
STATUS_TIMEOUT = 30
UPSTREAM_TIMEOUT = 20 * 60

READY, COLD = "ready", "cold"


class Client(StrEnum):
    """The clients the spike runs."""

    CLAUDE_CODE = "claude_code"
    CODEX_DEFAULT = "codex_default"
    CODEX_PROFILE = "codex_profile"
    OPENAI_SDK = "openai_sdk"
    ANTHROPIC_SDK = "anthropic_sdk"


class Kind(StrEnum):
    """The kinds of call: a stream, which the gateway holds for `hold_seconds`, and a non-streaming
    call, held for `hold_seconds_non_streaming`."""

    STREAM = "stream"
    CALL = "call"


# The kinds each client makes: the harnesses stream every turn.
KINDS: dict[Client, tuple[Kind, ...]] = {
    Client.CLAUDE_CODE: (Kind.STREAM,),
    Client.CODEX_DEFAULT: (Kind.STREAM,),
    Client.CODEX_PROFILE: (Kind.STREAM,),
    Client.OPENAI_SDK: (Kind.STREAM, Kind.CALL),
    Client.ANTHROPIC_SDK: (Kind.STREAM, Kind.CALL),
}
PAIRS: tuple[tuple[Client, Kind], ...] = tuple((client, kind) for client, kinds in KINDS.items() for kind in kinds)
# The one client whose abort the pass condition allows: Codex without the profile.
WITHOUT_PROFILE = Client.CODEX_DEFAULT
# Where each client's version comes from, in what `harnesses.versions` gives.
VERSION_OF: dict[Client, str] = {
    Client.CLAUDE_CODE: harnesses.CLAUDE_CODE.name,
    Client.CODEX_DEFAULT: harnesses.CODEX.name,
    Client.CODEX_PROFILE: harnesses.CODEX.name,
    Client.OPENAI_SDK: "openai_sdk",
    Client.ANTHROPIC_SDK: "anthropic_sdk",
}
# The route each client sends its request on.
ROUTE_OF: dict[Client, str] = {
    Client.CLAUDE_CODE: "/v1/messages",
    Client.CODEX_DEFAULT: "/v1/responses",
    Client.CODEX_PROFILE: "/v1/responses",
    Client.OPENAI_SDK: "/v1/chat/completions",
    Client.ANTHROPIC_SDK: "/v1/messages",
}


class Number(StrEnum):
    """The names of the numbers the job posts, one per row of its record."""

    CLAUDE_CODE_STREAM_COMPLETED = "claude_code_stream_completed"
    CLAUDE_CODE_STREAM_SECONDS_BEFORE_GIVING_UP = "claude_code_stream_seconds_before_giving_up"
    CODEX_DEFAULT_STREAM_COMPLETED = "codex_default_stream_completed"
    CODEX_DEFAULT_STREAM_SECONDS_BEFORE_GIVING_UP = "codex_default_stream_seconds_before_giving_up"
    CODEX_PROFILE_STREAM_COMPLETED = "codex_profile_stream_completed"
    CODEX_PROFILE_STREAM_SECONDS_BEFORE_GIVING_UP = "codex_profile_stream_seconds_before_giving_up"
    OPENAI_SDK_STREAM_COMPLETED = "openai_sdk_stream_completed"
    OPENAI_SDK_STREAM_SECONDS_BEFORE_GIVING_UP = "openai_sdk_stream_seconds_before_giving_up"
    OPENAI_SDK_CALL_COMPLETED = "openai_sdk_call_completed"
    OPENAI_SDK_CALL_SECONDS_BEFORE_GIVING_UP = "openai_sdk_call_seconds_before_giving_up"
    ANTHROPIC_SDK_STREAM_COMPLETED = "anthropic_sdk_stream_completed"
    ANTHROPIC_SDK_STREAM_SECONDS_BEFORE_GIVING_UP = "anthropic_sdk_stream_seconds_before_giving_up"
    ANTHROPIC_SDK_CALL_COMPLETED = "anthropic_sdk_call_completed"
    ANTHROPIC_SDK_CALL_SECONDS_BEFORE_GIVING_UP = "anthropic_sdk_call_seconds_before_giving_up"
    CLAUDE_CODE_402_RETRIED = "claude_code_402_retried"
    CLAUDE_CODE_402_SHOWN = "claude_code_402_shown"
    CODEX_DEFAULT_402_RETRIED = "codex_default_402_retried"
    CODEX_DEFAULT_402_SHOWN = "codex_default_402_shown"
    CODEX_PROFILE_402_RETRIED = "codex_profile_402_retried"
    CODEX_PROFILE_402_SHOWN = "codex_profile_402_shown"
    OPENAI_SDK_402_RETRIED = "openai_sdk_402_retried"
    OPENAI_SDK_402_SHOWN = "openai_sdk_402_shown"
    ANTHROPIC_SDK_402_RETRIED = "anthropic_sdk_402_retried"
    ANTHROPIC_SDK_402_SHOWN = "anthropic_sdk_402_shown"


def completed(client: Client, kind: Kind) -> Number:
    """The name of whether `client`'s request of `kind` completed through the hold."""
    return Number(f"{client}_{kind}_completed")


def seconds_before_giving_up(client: Client, kind: Kind) -> Number:
    """The name of the seconds `client` held its request of `kind` before giving up; 0 when it completed."""
    return Number(f"{client}_{kind}_seconds_before_giving_up")


def retried_402(client: Client) -> Number:
    """The name of whether `client` retried the 402."""
    return Number(f"{client}_402_retried")


def shown_402(client: Client) -> Number:
    """The name of whether `client` showed that the budget was spent."""
    return Number(f"{client}_402_shown")


# The verdict.


def passes(numbers: common.Numbers) -> bool:
    """Whether every client but Codex without the profile completed every request through the hold."""
    return all(numbers[completed(client, kind)] == 1 for client, kind in PAIRS if client is not WITHOUT_PROFILE)


SPIKE = common.Spike(MEASUREMENT, Number, passes)


# The outcomes.


@dataclass(frozen=True)
class HoldOutcome:
    """What one client did with one request of one kind held through a cold start."""

    client: Client
    kind: Kind
    version: str
    completed: bool
    seconds_before_giving_up: float


@dataclass(frozen=True)
class RefusalOutcome:
    """What one client did with the 402 `budget_reached`."""

    client: Client
    version: str
    retried: bool
    shown: bool


def numbers_of(holds: Sequence[HoldOutcome], refusals: Sequence[RefusalOutcome]) -> dict[str, int | float]:
    """The numbers the job posts. ValueError unless there is exactly one outcome for each client and
    kind of call, and one refusal for each client."""
    if sorted((hold.client, hold.kind) for hold in holds) != sorted(PAIRS):
        raise ValueError("the outcomes are not one for each client and kind of call")
    if sorted(refusal.client for refusal in refusals) != sorted(Client):
        raise ValueError("the refusals are not one for each client")
    numbers: dict[str, int | float] = {}
    for hold in holds:
        numbers[completed(hold.client, hold.kind)] = int(hold.completed)
        numbers[seconds_before_giving_up(hold.client, hold.kind)] = round(hold.seconds_before_giving_up, 1)
    for refusal in refusals:
        numbers[retried_402(refusal.client)] = int(refusal.retried)
        numbers[shown_402(refusal.client)] = int(refusal.shown)
    return numbers


# The exchanges, as the forwarder records them.


@dataclass
class Exchange:
    """One request a client sent through its forwarder, and what became of it. Times are on the
    forwarder's clock: `sent_at` when it was sent on, `closed_at` when the client closed it before
    its end, `ended_at` when the exchange ended."""

    method: str
    path: str
    body: bytes
    sent_at: float
    status: int | None = None
    cause: str | None = None
    completed: bool = False
    closed_at: float | None = None
    ended_at: float | None = None

    @property
    def route(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def json(self) -> dict[str, Any] | None:
        try:
            held = json.loads(self.body)
        except ValueError:
            return None
        return held if isinstance(held, dict) else None

    def streams(self) -> bool:
        return bool((self.json() or {}).get("stream"))


def intended(exchanges: Sequence[Exchange], client: Client, kind: Kind, marker: str) -> Exchange | None:
    """The client's request: the first it sent on its route that carries the marker and is of the kind."""
    for exchange in exchanges:
        if (
            exchange.method == "POST"
            and exchange.route == ROUTE_OF[client]
            and marker.encode() in exchange.body
            and exchange.streams() == (kind is Kind.STREAM)
        ):
            return exchange
    return None


def _same_request(body: dict[str, Any] | None) -> dict[str, Any] | None:
    # What differs between two sends of one request is not the request: an SDK's metadata.
    return None if body is None else {key: value for key, value in body.items() if key != "metadata"}


def repeats(exchanges: Sequence[Exchange], first: Exchange) -> int:
    """How many times the client sent `first` again: later requests on its route with its body."""
    later = exchanges[exchanges.index(first) + 1 :]
    same = _same_request(first.json())
    return sum(
        1
        for exchange in later
        if exchange.route == first.route
        and (exchange.body == first.body or (same is not None and _same_request(exchange.json()) == same))
    )


def cause_of(body: bytes) -> str | None:
    """The cause an error's body names: its `type` on Messages, its `code` on the OpenAI formats."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict):
        return None
    cause = error.get("code") or error.get("type")
    return cause if isinstance(cause, str) else None


def shows_budget(output: str) -> bool:
    """Whether a client's output tells the user that the budget is spent: the cause's sentence or name."""
    return Cause.BUDGET_REACHED.sentence in output or Cause.BUDGET_REACHED.value in output


# The forwarder.

_HOP_BY_HOP = {
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
# The last event of a stream that ended well, in each format: Messages, Responses, Chat Completions.
STREAM_ENDS = (b"event: message_stop", b"event: response.completed", b"data: [DONE]")
# What is kept of the stream so far, so that a last event split between two reads is still seen.
_TAIL = max(len(event) for event in STREAM_ENDS) - 1


def directed(body: bytes, model: str) -> bytes:
    """The body with `model` as its model, when it is a JSON object that names one; otherwise as it is."""
    try:
        held = json.loads(body)
    except ValueError:
        return body
    if not isinstance(held, dict) or "model" not in held or held["model"] == model:
        return body
    return json.dumps({**held, "model": model}, separators=(",", ":")).encode()


class Forwarder:
    """A local HTTP server that sends each request on to the gateway at `upstream`, the model
    written as `model`, and the answer back as it arrives, recording each exchange. Used as a
    context manager: it serves inside the block."""

    def __init__(self, upstream: str, model: str, clock: Callable[[], float] = time.monotonic) -> None:
        parts = urllib.parse.urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the gateway's address is not an http or https address")
        self._parts = parts
        self.model = model
        self.clock = clock
        self.exchanges: list[Exchange] = []
        self._lock = threading.Lock()
        forwarder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                forwarder._forward(self)

            def do_POST(self) -> None:
                forwarder._forward(self)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """The forwarder's address, which the client is given as the endpoint's."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def recorded(self) -> list[Exchange]:
        with self._lock:
            return list(self.exchanges)

    def _connection(self) -> http.client.HTTPConnection:
        host, port = self._parts.hostname or "", self._parts.port
        if self._parts.scheme == "https":
            return http.client.HTTPSConnection(
                host, port, timeout=UPSTREAM_TIMEOUT, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT)

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        handler.close_connection = True
        length = int(handler.headers.get("Content-Length") or 0)
        body = directed(handler.rfile.read(length), self.model) if length else b""
        exchange = Exchange(handler.command, handler.path, body, self.clock())
        with self._lock:
            self.exchanges.append(exchange)
        headers = {name: value for name, value in handler.headers.items() if name.lower() not in _HOP_BY_HOP}
        headers["Content-Length"] = str(len(body))
        headers["Accept-Encoding"] = "identity"
        upstream = self._connection()
        done = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(handler.connection, upstream, exchange, done), daemon=True)
        try:
            upstream.request(handler.command, self._parts.path.rstrip("/") + handler.path, body, headers)
            watcher.start()
            answer = upstream.getresponse()
            exchange.status = answer.status
            payload = answer.read() if answer.status >= 400 else b""
            if answer.status >= 400:
                exchange.cause = cause_of(payload)
            handler.send_response(answer.status)
            for name, value in answer.getheaders():
                if name.lower() not in _HOP_BY_HOP:
                    handler.send_header(name, value)
            if answer.status >= 400:
                handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            handler.wfile.flush()
            # A stream is whole once its last event has passed, which an error event ending it is not.
            streaming = "text/event-stream" in (answer.getheader("Content-Type") or "")
            tail, ended = b"", False
            while chunk := answer.read1(65536):
                handler.wfile.write(chunk)
                handler.wfile.flush()
                # The read with what was kept before it: an event begun in the last read ends in this one.
                seen = tail + chunk
                ended = ended or any(event in seen for event in STREAM_ENDS)
                tail = seen[-_TAIL:]
                # A client may close as soon as it has the last event: it then has the whole answer.
                exchange.completed = answer.status == 200 and streaming and ended
            exchange.completed = answer.status == 200 and (ended or not streaming)
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            # The client closed the exchange, or the gateway did: what ended it is in the record.
            if exchange.closed_at is None and exchange.status is not None and not exchange.completed:
                exchange.closed_at = self.clock()
        finally:
            done.set()
            exchange.ended_at = self.clock()
            upstream.close()

    def _watch(
        self, client: socket.socket, upstream: http.client.HTTPConnection, exchange: Exchange, done: threading.Event
    ) -> None:
        """Notes when the client closes the exchange before its end, and closes the gateway's side
        then, as a client that gives up does."""
        while not done.is_set():
            try:
                readable, _, _ = select.select([client], [], [], 0.2)
                if readable and not client.recv(1, socket.MSG_PEEK):
                    raise ConnectionResetError
            except (OSError, ValueError):
                if not done.is_set():
                    exchange.closed_at = self.clock()
                    with contextlib.suppress(OSError):
                        if upstream.sock is not None:
                            upstream.sock.shutdown(socket.SHUT_RDWR)
                return


# One attempt of a client.


@dataclass
class Attempt:
    """What one run of a client left: its exit status, what it printed, when it ended, and its exchanges."""

    exit_code: int | None
    output: str
    ended_at: float
    exchanges: list[Exchange] = field(default_factory=list)
    stopped: bool = False


def hold_outcome(client: Client, kind: Kind, version: str, attempt: Attempt, request: Exchange) -> HoldOutcome:
    """What the client did with its request: completed, or the seconds before it gave up."""
    if request.completed and attempt.exit_code == 0:
        return HoldOutcome(client, kind, version, completed=True, seconds_before_giving_up=0.0)
    end = next(at for at in (request.closed_at, request.ended_at, attempt.ended_at) if at is not None)
    return HoldOutcome(client, kind, version, completed=False, seconds_before_giving_up=max(0.0, end - request.sent_at))


def refused(request: Exchange) -> bool:
    """Whether the gateway answered the request with an error rather than holding it: a held stream
    is answered 200 at once, a held call only when it ends or when the client gives up."""
    return request.status is not None and request.status != HTTPStatus.OK


def exercised(request: Exchange, ready_at: float | None, deadline: float) -> bool:
    """Whether the request was held through the cold start meant: the gateway did not refuse it,
    and the pool became ready less than `deadline` seconds after it, and no more than
    `BAND_SECONDS` less. Another client's request may have started the pool: a refused request
    was not held, however the pool's readiness falls."""
    if ready_at is None or refused(request):
        return False
    return deadline - BAND_SECONDS <= ready_at - request.sent_at < deadline


def call_delay(cold_start: float, deadline: float) -> float:
    """Seconds from the wake to the start of the calls, so that the readiness foreseen comes
    `CALL_AIM_SECONDS` under their deadline after them."""
    return max(0.0, cold_start - (deadline - CALL_AIM_SECONDS))


def refusal_outcome(client: Client, version: str, attempt: Attempt, marker: str) -> RefusalOutcome:
    """What the client did with the 402. NotExercised when its request was not answered 402 `budget_reached`."""
    kind = KINDS[client][0]
    request = intended(attempt.exchanges, client, kind, marker)
    if request is None or request.status != Cause.BUDGET_REACHED.status or request.cause != Cause.BUDGET_REACHED:
        raise NotExercised(f"{client}'s request was not answered 402 {Cause.BUDGET_REACHED}")
    return RefusalOutcome(
        client, version, retried=repeats(attempt.exchanges, request) > 0, shown=shows_budget(attempt.output)
    )


class NotExercised(RuntimeError):
    """The run did not ask the question: it is not measured."""


# The clients, as processes.


class Started:
    """A client that runs: `done` once it has ended, then `result`."""

    def done(self) -> bool:
        raise NotImplementedError

    def result(self) -> Attempt:
        raise NotImplementedError


type Starter = Callable[[Client, Kind, str, str], Started]


@contextlib.contextmanager
def _environment(values: Mapping[str, str]) -> Generator[None]:
    """The process's environment with `values` set, for the block: `rhi_cli.harness` finds its files there."""
    before = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def without_timer(profile: str) -> str:
    """Codex's profile with its stream timeout left out: Codex with its default timer."""
    return "".join(
        line for line in profile.splitlines(keepends=True) if not line.strip().startswith("stream_idle_timeout_ms")
    )


def write_home(home: Path, client: Client, endpoint: str, model: str, key: str) -> None:
    """What `rhi login` writes into a home for `client`, the endpoint being `endpoint`, and the
    `rhi` command that answers `rhi token` with `key`."""
    folders = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex")}
    with _environment(folders):
        if client is Client.CLAUDE_CODE:
            login_files.configure_claude_code(endpoint)
        elif client in (Client.CODEX_DEFAULT, Client.CODEX_PROFILE):
            login_files.configure_codex(endpoint, model)
            if client is Client.CODEX_DEFAULT:
                profile = login_files.codex_profile()
                profile.write_text(without_timer(profile.read_text(encoding="utf-8")), encoding="utf-8")
    keys = home / ".config" / "rhi"
    keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_file = keys / store.KEY_FILE
    key_file.touch(mode=0o600)
    key_file.write_text(key, encoding="utf-8")
    command = home / "bin" / "rhi"
    command.parent.mkdir(parents=True, exist_ok=True)
    script = "import sys; from rhi_cli.main import main; sys.exit(main())"
    command.write_text(f"#!/bin/sh\nexec '{sys.executable}' -c '{script}' \"$@\"\n", encoding="utf-8")
    command.chmod(command.stat().st_mode | stat.S_IXUSR)


def client_command(
    client: Client,
    kind: Kind,
    *,
    home: Path,
    endpoint: str,
    model: str,
    key: str,
    prompt: str,
    binaries: Mapping[str, Path],
) -> tuple[list[str], dict[str, str]]:
    """The command that runs `client` once, and its environment."""
    environment = {
        "HOME": str(home),
        "PATH": f"{home / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "XDG_CONFIG_HOME": str(home / ".config"),
        # The job's import path, so that `rhi token` and an SDK script import what the job imports.
        "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
        "LANG": "C.UTF-8",
        # `rhi token` reads the key file the home holds, not a keychain of the job's machine.
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
    }
    if client is Client.CLAUDE_CODE:
        environment |= {
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Every model Claude Code asks for is the mock pool's.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
        }
        argv = [str(binaries[harnesses.CLAUDE_CODE.name]), "-p", prompt, "--model", model, "--output-format", "text"]
        return argv, environment
    if client in (Client.CODEX_DEFAULT, Client.CODEX_PROFILE):
        environment["CODEX_HOME"] = str(home / ".codex")
        argv = [
            str(binaries[harnesses.CODEX.name]),
            "exec",
            "--profile",
            login_files.NAME,
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            prompt,
        ]
        return argv, environment
    environment |= {PROMPT_VARIABLE: prompt, MODEL_VARIABLE: model}
    if client is Client.OPENAI_SDK:
        environment |= {"OPENAI_API_KEY": key, "OPENAI_BASE_URL": f"{endpoint}/v1"}
    else:
        environment |= {"ANTHROPIC_API_KEY": key, "ANTHROPIC_BASE_URL": endpoint}
    return [sys.executable, str(Path(__file__).resolve()), SDK_FLAG, client, kind], environment


class Process(Started):
    """A client run as a process, through a forwarder of its own, in a home of its own."""

    def __init__(
        self,
        client: Client,
        kind: Kind,
        key: str,
        prompt: str,
        *,
        folder: Path,
        gateway: str,
        model: str,
        binaries: Mapping[str, Path],
        clock: Callable[[], float],
    ) -> None:
        self.clock = clock
        home = Path(tempfile.mkdtemp(prefix=f"{client}-", dir=folder))
        self.forwarder = Forwarder(gateway, model, clock).__enter__()
        write_home(home, client, self.forwarder.url, model, key)
        argv, environment = client_command(
            client, kind, home=home, endpoint=self.forwarder.url, model=model, key=key, prompt=prompt, binaries=binaries
        )
        self.output_path = home / "output.txt"
        self._output = self.output_path.open("wb")
        self.started_at = clock()
        self.process = subprocess.Popen(
            argv, env=environment, cwd=home, stdin=subprocess.DEVNULL, stdout=self._output, stderr=subprocess.STDOUT
        )
        self.ended_at: float | None = None
        self.stopped = False

    def done(self) -> bool:
        if self.ended_at is None and self.process.poll() is not None:
            self.ended_at = self.clock()
        if self.ended_at is None and self.clock() - self.started_at > CLIENT_LIMIT_SECONDS:
            self.process.kill()
            self.process.wait()
            self.stopped = True
            self.ended_at = self.clock()
        return self.ended_at is not None

    def result(self) -> Attempt:
        self._output.close()
        self.forwarder.__exit__()
        output = self.output_path.read_text(encoding="utf-8", errors="replace")
        return Attempt(
            self.process.returncode, output, self.ended_at or self.clock(), self.forwarder.recorded(), self.stopped
        )


# The gateway's status and wake.


@dataclass(frozen=True)
class Gateway:
    """The gateway at `endpoint`, asked about the pool `pool` as the canary student of `key`."""

    endpoint: str
    pool: str
    key: str = field(repr=False)

    def _ask(self, method: str, path: str, body: bytes | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        request = urllib.request.Request(self.endpoint.rstrip("/") + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=STATUS_TIMEOUT) as answer:
            return json.loads(answer.read())

    def state(self) -> str:
        """The pool's state as the status gives it; empty when the status could not be read."""
        try:
            pools = self._ask("GET", "/status")["pools"]
            return next(str(record["state"]) for record in pools if record["model"] == self.pool)
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return ""

    def wake(self) -> None:
        """Wakes the pool, as a student's `rhi wait` does."""
        self._ask("POST", "/wake", json.dumps({"model": self.pool}).encode())


# One run.


@dataclass(frozen=True)
class Deadlines:
    """The pool's holds, in seconds: for a stream, and for a non-streaming call."""

    stream: float
    call: float

    def of(self, kind: Kind) -> float:
        return self.stream if kind is Kind.STREAM else self.call


class Run:
    """One run of the spike: the 402 of each client, then the holds of the streams and the calls."""

    def __init__(
        self,
        gateway: Any,
        deadlines: Deadlines,
        start: Starter,
        keys: SpikeKeys,
        versions: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        if len(keys.students) < len(Client):
            raise ValueError("spike S2 needs a canary student with the default budget for each client")
        self.gateway, self.deadlines, self.start, self.keys = gateway, deadlines, start, keys
        self.versions, self.clock, self.sleep, self.log = versions, clock, sleep, log
        # The last cold start measured: from the first request, or the wake, to readiness.
        self.cold_start: float | None = None

    def version(self, client: Client) -> str:
        return self.versions[VERSION_OF[client]]

    def refusal(self, client: Client) -> RefusalOutcome:
        marker = f"rhi-s2-{secrets.token_hex(4)}"
        running = self.start(client, KINDS[client][0], self.keys.spent, f"Reply with one word. {marker}")
        while not running.done():
            self.sleep(POLL_SECONDS)
        return refusal_outcome(client, self.version(client), running.result(), marker)

    def _wait_cold(self) -> None:
        waited = 0.0
        while (state := self.gateway.state()) != COLD:
            if waited >= COLD_WAIT_SECONDS:
                raise NotExercised(f"the pool did not go cold: it is {state or 'unknown'}")
            self.sleep(COLD_POLL_SECONDS)
            waited += COLD_POLL_SECONDS

    def _attempt(self, kind: Kind, clients: Sequence[Client]) -> list[HoldOutcome]:
        """One cold start: the clients' requests held through it; the outcomes of those that exercised the hold."""
        deadline = self.deadlines.of(kind)
        self._wait_cold()
        woke_at = None
        if kind is Kind.CALL:
            woke_at = self.clock()
            self.gateway.wake()
            foreseen = self.cold_start if self.cold_start is not None else deadline
            self.sleep(call_delay(foreseen, deadline))
        markers = {client: f"rhi-s2-{secrets.token_hex(4)}" for client in clients}
        running = {
            client: self.start(client, kind, self.keys.students[index], f"Reply with one word. {markers[client]}")
            for index, client in enumerate(clients)
        }
        launched_at, ready_at = self.clock(), None
        # Until every client has ended, and the pool is ready or can no longer be ready in time.
        while True:
            if ready_at is None and self.gateway.state() == READY:
                ready_at = self.clock()
            ended = all(started.done() for started in running.values())
            if ended and (ready_at is not None or self.clock() - launched_at > deadline):
                break
            self.sleep(POLL_SECONDS)
        attempts = {client: started.result() for client, started in running.items()}
        requests = {client: intended(attempts[client].exchanges, client, kind, markers[client]) for client in clients}
        sent = [request.sent_at for request in requests.values() if request is not None]
        if ready_at is not None and (woke_at is not None or sent):
            self.cold_start = ready_at - (woke_at if woke_at is not None else min(sent))
        outcomes = []
        for client in clients:
            request, attempt = requests[client], attempts[client]
            if request is None or attempt.stopped or not exercised(request, ready_at, deadline):
                if request is None:
                    held = "no request"
                elif refused(request):
                    held = f"the gateway answered {request.status} {request.cause or ''}".rstrip()
                elif ready_at is None:
                    held = "the pool was not ready in time"
                else:
                    held = f"{ready_at - request.sent_at:.0f} s"
                self.log(f"{client} {kind}: the hold was not the one meant ({held}, stopped: {attempt.stopped})")
                continue
            outcomes.append(hold_outcome(client, kind, self.version(client), attempt, request))
        return outcomes

    def holds(self, kind: Kind) -> list[HoldOutcome]:
        """The outcome of each client that makes requests of `kind`, each through a hold that exercised it."""
        remaining = [client for client, client_kind in PAIRS if client_kind is kind]
        outcomes: list[HoldOutcome] = []
        for _ in range(ATTEMPTS):
            for outcome in self._attempt(kind, remaining):
                outcomes.append(outcome)
                remaining.remove(outcome.client)
            if not remaining:
                return outcomes
        raise NotExercised(f"no hold of a {kind} exercised the question for {', '.join(remaining)}")

    def measure(self) -> dict[str, int | float]:
        refusals = [self.refusal(client) for client in Client]
        holds = self.holds(Kind.STREAM) + self.holds(Kind.CALL)
        for hold in holds:
            given_up = "completed" if hold.completed else f"gave up after {hold.seconds_before_giving_up:.0f} s"
            self.log(f"{hold.client} {hold.version} {hold.kind}: {given_up}")
        for refusal in refusals:
            self.log(f"{refusal.client} {refusal.version} 402: retried {refusal.retried}, shown {refusal.shown}")
        return numbers_of(holds, refusals)


# The SDK scripts.


def sdk_script(client: Client, kind: Kind, environ: Mapping[str, str]) -> int:
    """One request through an SDK, with its own timeout and retries, as a student's program sends it:
    prints the answer, or what the SDK raised. The SDK reads its key and address from its own variables."""
    prompt, model = environ[PROMPT_VARIABLE], environ[MODEL_VARIABLE]
    try:
        if client is Client.OPENAI_SDK:
            chat = openai.OpenAI().chat.completions
            asked: list[ChatCompletionMessageParam] = [{"role": "user", "content": prompt}]
            if kind is Kind.STREAM:
                for chunk in chat.create(model=model, messages=asked, stream=True):
                    print("".join(choice.delta.content or "" for choice in chunk.choices), end="", flush=True)
            else:
                print(chat.create(model=model, messages=asked).choices[0].message.content)
        else:
            messages = anthropic.Anthropic().messages
            given: list[MessageParam] = [{"role": "user", "content": prompt}]
            if kind is Kind.STREAM:
                with messages.stream(model=model, max_tokens=MAX_TOKENS, messages=given) as stream:
                    for text in stream.text_stream:
                        print(text, end="", flush=True)
            else:
                print(messages.create(model=model, max_tokens=MAX_TOKENS, messages=given).content)
    except Exception as error:  # noqa: BLE001 - what the SDK raised is what a student's program shows
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print()
    return 0


def main(environ: Mapping[str, str] = os.environ) -> common.Verdict:
    """Runs the spike with the job's environment and credentials, and posts its numbers."""

    def measure() -> dict[str, int | float]:
        name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
        (pool,) = [pool for pool in config.read_ssm().pools if pool.name == name]
        if pool.engine != MOCK_ENGINE or pool.start_delay_seconds <= 0:
            raise ValueError("spike S2 runs on a mock pool whose start_delay_seconds is set")
        keys = common.canary_keys()
        endpoint = environ[ENDPOINT_VARIABLE]
        with tempfile.TemporaryDirectory(prefix="rhi-s2-") as work:
            folder = Path(work)
            binaries = harnesses.install(folder / "harnesses")
            versions = harnesses.versions(binaries)
            print(harnesses.report(versions))

            def start(client: Client, kind: Kind, key: str, prompt: str) -> Started:
                return Process(
                    client,
                    kind,
                    key,
                    prompt,
                    folder=folder,
                    gateway=endpoint,
                    model=name,
                    binaries=binaries,
                    clock=time.monotonic,
                )

            gateway = Gateway(endpoint, name, keys.students[0])
            deadlines = Deadlines(pool.hold_seconds, pool.hold_seconds_non_streaming)
            return Run(gateway, deadlines, start, keys, versions).measure()

    return common.run_spike(SPIKE, JOB, measure, environ=environ)


if __name__ == "__main__":
    if sys.argv[1:2] == [SDK_FLAG]:
        sys.exit(sdk_script(Client(sys.argv[2]), Kind(sys.argv[3]), os.environ))
    main()
