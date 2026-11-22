"""An error of the gateway, in the wire format of the request (system design, section 6.2, step 8).

A client understands the error shape of its own format and no other, so every error is rendered
three ways. Each has two forms: a JSON answer, with the cause's status, and the event that ends a
stream. A stream was answered 200 before its error was known, so its cause travels in the event
and nowhere else.

| Format | JSON answer | Event that ends a stream |
| --- | --- | --- |
| Anthropic Messages | `{"type": "error", "error": {"type", "message"}}` | `event: error`, the same object |
| OpenAI Responses | `{"error": {"message", "type", "param", "code"}}` | `event: response.failed`, the error as the response's `error` |
| OpenAI Chat Completions | the same as Responses | a `data:` line holding the JSON answer's object |

The cause's name is the error's `type`, and its `code` where the format has one; the message is
the cause's sentence. Every string of a rendered error is therefore a constant of this code or of
`rhi_common.causes`. From an exception, the renderer takes the cause that a `GatewayError` carries
and nothing else: never its message, its arguments or the exception it came from, which may hold
text from AWS or from the engine (section 5.3). An exception that carries no cause is rendered as
`engine_error`.

Nothing here reads configuration or the network: these are pure functions of their arguments.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from rhi_common.causes import Cause


class WireFormat(StrEnum):
    """The three formats the gateway forwards (system design, section 6.1)."""

    MESSAGES = "messages"
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class GatewayError(Exception):
    """A failure whose cause the gateway knows.

    The message is for whoever raises and catches it inside the gateway: no renderer reads it.
    """

    def __init__(self, cause: Cause, message: str = "") -> None:
        super().__init__(message)
        self.cause = cause


class KeyGone(GatewayError):
    """The refusal of a key that the store no longer holds, as the admin's erase leaves it: `invalid_key`."""

    def __init__(self) -> None:
        super().__init__(Cause.INVALID_KEY)


# The causes that ask the client to come back: the only ones whose seconds a caller may give.
_CARRIES_RETRY_AFTER = frozenset(cause for cause in Cause if cause.retry_after is not None)


@dataclass(frozen=True)
class JsonAnswer:
    """An error as a whole HTTP answer: the status, the headers and the JSON body."""

    status: int
    headers: Mapping[str, str]
    body: bytes


def cause_of(error: Cause | BaseException) -> Cause:
    """The cause to render: the one given, the one a `GatewayError` carries, or `engine_error`."""
    if isinstance(error, Cause):
        return error
    if isinstance(error, GatewayError):
        return error.cause
    return Cause.ENGINE_ERROR


def render_json(
    error: Cause | BaseException,
    wire_format: WireFormat,
    retry_after: Mapping[Cause, int] | None = None,
) -> JsonAnswer:
    """The JSON answer for an error, in one wire format.

    `retry_after` replaces the default seconds of the `Retry-After` header, for the causes it
    names. It may name only the causes that carry the header: any other is refused, so that no
    caller can add the header to a cause the design gives none.
    """
    cause = cause_of(error)
    headers = {"Content-Type": "application/json"}
    seconds = _retry_after_seconds(cause, retry_after or {})
    if seconds is not None:
        headers["Retry-After"] = str(seconds)
    body = _messages_error(cause) if wire_format is WireFormat.MESSAGES else _openai_error(cause)
    return JsonAnswer(status=cause.status, headers=headers, body=_json(body))


def render_stream_event(error: Cause | BaseException, wire_format: WireFormat) -> bytes:
    """The server-sent event that ends a stream of one wire format with an error.

    The gateway writes nothing after it. On Chat Completions that means no `[DONE]` either: it
    says a stream ended well.
    """
    cause = cause_of(error)
    if wire_format is WireFormat.MESSAGES:
        return _event("error", _messages_error(cause))
    if wire_format is WireFormat.RESPONSES:
        failed = {"object": "response", "status": "failed", "error": _response_error(cause)}
        return _event("response.failed", {"type": "response.failed", "response": failed})
    # Chat Completions names no event: a chunk that holds `error` is what its clients read.
    return _event(None, _openai_error(cause))


def _retry_after_seconds(cause: Cause, given: Mapping[Cause, int]) -> int | None:
    for named, seconds in given.items():
        if named not in _CARRIES_RETRY_AFTER:
            raise ValueError("Retry-After was given for a cause that carries none")
        if seconds < 0:
            raise ValueError("Retry-After was given a negative number of seconds")
    return given.get(cause, cause.retry_after)


def _messages_error(cause: Cause) -> dict[str, object]:
    return {"type": "error", "error": {"type": cause.value, "message": cause.sentence}}


def _openai_error(cause: Cause) -> dict[str, object]:
    return {"error": {"message": cause.sentence, "type": cause.value, "param": None, "code": cause.value}}


def _response_error(cause: Cause) -> dict[str, object]:
    return {"code": cause.value, "message": cause.sentence}


def _json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("ascii")


def _event(name: str | None, data: Mapping[str, object]) -> bytes:
    event = b"" if name is None else b"event: " + name.encode("ascii") + b"\n"
    return event + b"data: " + _json(data) + b"\n\n"
