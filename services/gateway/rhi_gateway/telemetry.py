"""One log record per request, metadata only (system design, sections 6.2, 6.6 and 7.3).

Prompts and responses are not stored. Each request the gateway answers gives one record, written
to standard output as one JSON object on one line when the request ends; the task's log
configuration sends that output to the gateway's log group. A record holds exactly these fields:

| Field | Value |
| --- | --- |
| `alias` | the student's roster alias, once the key is accepted; `null` before |
| `time` | when the request arrived, ISO 8601 in UTC |
| `pool` | the pool the request was for, once known; `null` otherwise |
| `wire_format` | the wire format of the route; `null` for a local route or a path outside the table |
| `input_tokens`, `output_tokens` | what the request's settlement observed; 0 when nothing was forwarded |
| `latency_ms` | from the request's arrival to its end, in milliseconds |
| `status` | the HTTP status sent; `null` if none was |
| `cause` | the cause the gateway gave (`rhi_common.causes`), `none` for its own fault, else `null` |

Nothing else reaches a record: no body, no header's value, no key or its hash, no GitHub ID (the
alias is the only name of a student in a log, section 6.6), and no text of an exception. A record
is never in CloudWatch's embedded metric format, which would make CloudWatch metrics of it, and
those cannot be deleted for 15 months.

**The gateway's own faults** (section 7.3). An exception that escapes the application is a fault
of the gateway: the client is answered 500 with no body, so none of the exception's text reaches
it, and the record carries the cause `none`. So does any answer of 500 or above that names no
cause. The exception is not raised further: the server would log its text.

`Telemetry` is the middleware that `rhi_gateway.app.build_app` installs. The parts of the gateway
that learn something of a request note it on the request's record through the `note_*`
functions, which do nothing outside a request.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rhi_common.causes import Cause
from rhi_gateway.routes import match

# The cause of an answer that names none of `rhi_common.causes`: a fault of the gateway itself.
NO_CAUSE = "none"
# The status of an answer to a request whose handler failed.
FAULT_STATUS = 500


@dataclass
class RequestRecord:
    """What is logged of one request. Every field is metadata: none comes from a body or a header's value."""

    time: str
    wire_format: str | None
    alias: str | None = None
    pool: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    status: int | None = None
    cause: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "time": self.time,
            "pool": self.pool,
            "wire_format": self.wire_format,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "cause": self.cause,
        }


_CURRENT: ContextVar[RequestRecord | None] = ContextVar("rhi_gateway_request_record", default=None)


def note_student(alias: str) -> None:
    """The request's student, by alias: never the GitHub ID."""
    record = _CURRENT.get()
    if record is not None:
        record.alias = alias


def note_pool(name: str) -> None:
    record = _CURRENT.get()
    if record is not None:
        record.pool = name


def note_tokens(input_tokens: int, output_tokens: int) -> None:
    """The tokens the request's settlement observed."""
    record = _CURRENT.get()
    if record is not None:
        record.input_tokens = input_tokens
        record.output_tokens = output_tokens


def note_cause(cause: Cause) -> None:
    """The cause the gateway gives the request, in a JSON answer or in the event that ends a stream."""
    record = _CURRENT.get()
    if record is not None:
        record.cause = cause.value


def write_line(line: str) -> None:
    """One record to standard output, in one write, flushed at once."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _arrival(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Telemetry:
    """The ASGI middleware that writes one record per HTTP request, and answers the gateway's own faults."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        write: Callable[[str], None] = write_line,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._write = write
        self._clock = clock
        self._monotonic = monotonic

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        entry = match(scope["method"], scope.get("raw_path"))
        wire_format = None if entry is None or entry.wire_format is None else entry.wire_format.value
        record = RequestRecord(time=_arrival(self._clock()), wire_format=wire_format)
        started = self._monotonic()

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                record.status = message["status"]
            await send(message)

        token = _CURRENT.set(record)
        try:
            await self._app(scope, receive, sending)
        except Exception:  # noqa: BLE001 - any exception is the gateway's own fault: answered 500, logged as cause none, never its text.
            record.cause = NO_CAUSE
            if record.status is None:
                await _answer_fault(sending)
        finally:
            _CURRENT.reset(token)
            record.latency_ms = max(0, round((self._monotonic() - started) * 1000))
            if record.status is not None and record.status >= FAULT_STATUS and record.cause is None:
                record.cause = NO_CAUSE
            self._write(json.dumps(record.to_json(), separators=(",", ":")))


async def _answer_fault(send: Send) -> None:
    """500 with no body: nothing of the exception reaches the client."""
    await send({"type": "http.response.start", "status": FAULT_STATUS, "headers": [(b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b"", "more_body": False})
