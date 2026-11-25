"""A client's forwarder and its home: what the spikes that run a harness share (system design,
sections 6.2 and 12).

This module is not a job. A spike that runs a client imports it as `forwarder`.

**The clients** (`Client`): Claude Code and Codex at the versions `spikes/harnesses.py` pins and
installs, Codex once with its default timer and once with the profile `rhi login` writes, and a
script of each SDK of the group `spikes`.

**The home.** Each run of a client has a home of its own, holding what `rhi login` writes there
(`rhi_cli.harness`): Claude Code's settings, Codex's profile `rhi` (for Codex with its default
timer, the same profile without `stream_idle_timeout_ms`), and the key in the CLI's key file
(`write_home`). `rhi token`, which both harnesses run for the key, is a command of the home's `bin`
folder that runs the CLI of this repository from the job's environment, since the job installs the
repository's libraries and not the CLI's entry point.

**The forwarder.** A client reaches the gateway through a forwarder of its own (`Forwarder`), a
local HTTP server that sends each request on to the gateway and the answer back as it arrives, and
records each exchange (`Exchange`): when it was sent, its status and cause, whether the whole
answer reached the client (the last event of a stream, whole, or a body to the length the gateway
declared), and when the client closed it before its end. The forwarder writes the pool's name as
the `model` of every request it forwards, so that no request reaches another pool: a `claude-` name
would reach the default pool, a GPU pool (section 6.2).

**A refusal** (`refused`): the gateway answered the request with an error rather than its answer,
an error status or an error event that ends a stream.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import select
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Self

from rhi_cli import harness as login_files
from rhi_cli import store

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Mapping
    from pathlib import Path

# Seconds: the longest a forwarded exchange may be silent.
UPSTREAM_TIMEOUT = 20 * 60


class Client(StrEnum):
    """The clients the spike runs."""

    CLAUDE_CODE = "claude_code"
    CODEX_DEFAULT = "codex_default"
    CODEX_PROFILE = "codex_profile"
    OPENAI_SDK = "openai_sdk"
    ANTHROPIC_SDK = "anthropic_sdk"


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
    # The request's headers as the client sent them, without its key and its cookies (`replayable`).
    headers: dict[str, str] = field(default_factory=dict)

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


def _cause_in(held: object) -> str | None:
    error = held.get("error") if isinstance(held, dict) else None
    if not isinstance(error, dict):
        return None
    cause = error.get("code") or error.get("type")
    return cause if isinstance(cause, str) else None


def cause_of(body: bytes) -> str | None:
    """The cause an error's body names: its `type` on Messages, its `code` on the OpenAI formats."""
    try:
        return _cause_in(json.loads(body))
    except ValueError:
        return None


def stream_error(event: bytes) -> str | None:
    """The cause an event of a stream names when it is an error ending the stream: `error` on
    Messages, `response.failed` on Responses, a chunk that holds `error` on Chat Completions."""
    data = b"\n".join(line[5:].strip() for line in event.splitlines() if line.startswith(b"data:"))
    try:
        held = json.loads(data)
    except ValueError:
        return None
    if isinstance(held, dict) and held.get("type") == "response.failed":
        held = held.get("response")
    return _cause_in(held)


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


def stream_end(event: bytes) -> bool:
    """Whether a whole event, the blank line that ends it already passed, is the last event of a
    stream that ended well: its name, or `[DONE]`, and data that is whole JSON."""
    lines = event.strip(b"\n").split(b"\n")
    if b"data: [DONE]" in lines:
        return True
    if not any(line in STREAM_ENDS for line in lines):
        return False
    data = b"\n".join(line[5:].strip() for line in lines if line.startswith(b"data:"))
    try:
        json.loads(data)
    except ValueError:
        return False
    return True


def whole_json(body: bytes) -> bool:
    """Whether a non-streaming answer's body is whole: JSON that reads to its end."""
    try:
        json.loads(body)
    except ValueError:
        return False
    return True


# The headers an exchange does not keep: the key and the cookies, which a recorded session never holds.
_NOT_KEPT = frozenset({"authorization", "x-api-key", "cookie", "proxy-authorization"})


def replayable(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """A request's headers without its key, its cookies and the hop's own: what a replay of it may send."""
    return {name: value for name, value in headers if name.lower() not in _NOT_KEPT | _HOP_BY_HOP}


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
        exchange = Exchange(handler.command, handler.path, body, self.clock(), headers=replayable(handler.headers.items()))
        with self._lock:
            self.exchanges.append(exchange)
        headers = {name: value for name, value in handler.headers.items() if name.lower() not in _HOP_BY_HOP}
        headers["Content-Length"] = str(len(body))
        headers["Accept-Encoding"] = "identity"
        upstream = self._connection()
        done = threading.Event()
        # Set once the client has been sent, or is being sent, all the gateway declared: its close is
        # then the end of what it was given, not a client giving up.
        delivered = threading.Event()
        watcher = threading.Thread(
            target=self._watch, args=(handler.connection, upstream, exchange, done, delivered), daemon=True
        )
        # Which side the exchange is at: an error while writing to the client is the client closing
        # it; one while reading the gateway is the gateway ending it, which no client did.
        to_client = False
        try:
            upstream.request(handler.command, self._parts.path.rstrip("/") + handler.path, body, headers)
            watcher.start()
            answer = upstream.getresponse()
            exchange.status = answer.status
            payload = answer.read() if answer.status >= 400 else b""
            if answer.status >= 400:
                exchange.cause = cause_of(payload)
            to_client = True
            handler.send_response(answer.status)
            for name, value in answer.getheaders():
                if name.lower() not in _HOP_BY_HOP:
                    handler.send_header(name, value)
            if answer.status >= 400:
                handler.send_header("Content-Length", str(len(payload)))
            elif answer.length is not None:
                # The length the gateway declared, so that a client too sees an answer cut short.
                handler.send_header("Content-Length", str(answer.length))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            handler.wfile.flush()
            # A stream is whole once its last event has passed, whole, which an error event ending
            # it is not; a non-streaming answer once its body has, to the length the gateway declared.
            streaming = "text/event-stream" in (answer.getheader("Content-Type") or "")
            ended, pending, kept = False, b"", b""
            while True:
                to_client = False
                chunk = answer.read1(65536)
                if not chunk:
                    break
                to_client = True
                if answer.length == 0:
                    delivered.set()
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if streaming:
                    # Each whole event: an event begun in the last read ends in this one.
                    *events, pending = (pending + chunk).replace(b"\r\n", b"\n").split(b"\n\n")
                    # An error that ends the stream is the gateway ending it.
                    exchange.cause = exchange.cause or next(filter(None, map(stream_error, events)), None)
                    ended = ended or any(map(stream_end, events))
                    # A client may close as soon as it has the last event: it then has the whole answer.
                    exchange.completed = answer.status == 200 and ended
                elif answer.status == 200:
                    kept += chunk
            # Whatever the gateway declared and did not send: the answer was cut short.
            short = bool(answer.length)
            whole = ended if streaming else whole_json(kept)
            exchange.completed = exchange.completed or (answer.status == 200 and whole and not short)
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            # Only a failed write is the client closing the exchange; a failed read is the gateway's doing.
            if to_client and exchange.closed_at is None and not exchange.completed:
                exchange.closed_at = self.clock()
        finally:
            done.set()
            exchange.ended_at = self.clock()
            upstream.close()

    def _watch(
        self,
        client: socket.socket,
        upstream: http.client.HTTPConnection,
        exchange: Exchange,
        done: threading.Event,
        delivered: threading.Event,
    ) -> None:
        """Notes when the client closes the exchange before its end, and closes the gateway's side
        then, as a client that gives up does. A client that closes once it has all the gateway
        declared has not given up: what became of the exchange is the gateway's doing."""
        while not done.is_set():
            try:
                readable, _, _ = select.select([client], [], [], 0.2)
                if readable and not client.recv(1, socket.MSG_PEEK):
                    raise ConnectionResetError
            except (OSError, ValueError):
                if not done.is_set() and not delivered.is_set():
                    exchange.closed_at = self.clock()
                    with contextlib.suppress(OSError):
                        if upstream.sock is not None:
                            upstream.sock.shutdown(socket.SHUT_RDWR)
                return


def refused(request: Exchange) -> bool:
    """Whether the gateway answered the request with an error rather than holding it to its answer:
    an error status (a held stream is answered 200 at once, a held call only when it ends or when
    the client gives up), or an error event that ended a stream, such as the end of the hold."""
    return request.cause is not None or (request.status is not None and request.status != HTTPStatus.OK)


# The home.


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
