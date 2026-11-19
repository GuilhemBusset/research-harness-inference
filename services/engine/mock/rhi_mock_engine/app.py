"""The mock engine: the engine contract's routes, answered with canned streams (system design, section 4.1).

A CPU process that the gateway, the scaler and the tests can use in place of a model server. It
serves the four inference routes in their three wire formats, streamed and not, `GET /health` and
`GET /metrics`.

| Route | Answer |
| --- | --- |
| `POST /v1/chat/completions` | a completion, or its chunks, the optional usage chunk and `[DONE]` |
| `POST /v1/responses` | a response, or its events from `response.created` to `response.completed` |
| `POST /v1/messages` | a message, or its events from `message_start` to `message_stop` |
| `POST /v1/messages/count_tokens` | `{"input_tokens": n}` |
| `GET /health` | 503 until the start delay has passed, then 200 |
| `GET /metrics` | the contract's metrics in Prometheus' text format, to a loopback client; 404 to any other |
| `POST /invocations`, `/pause`, `/tokenize` | 200, without the secret |

**The canned turn.** The coursework model reasons before it answers (section 4.2), so every turn
is reasoning, then an answer, then usage. The answer is a call of the first function the request
declares, unless the request declares none or already ends with a tool's result, and then it is
text: a harness that runs the call and sends its result back gets a turn that ends. Each piece of
`REASONING`, `ANSWER` and `ARGUMENTS` is one output token and, in a stream, one delta.

**The input.** `input_tokens` is a pure function of the request: its prompt as an engine reads it,
cut into tokens. What stands before the transcript comes first (system text or instructions, and
tools), then the transcript (messages or input), item by item. Each of these parts is written as
canonical JSON, and every four characters of a part are one token. So a transcript that grows at
its end keeps the tokens it had, and the same request always costs the same: `count_input_tokens`
is their number, which the usage of an answer and the token-counting route both report.

**The cache.** The mock computes nothing, so it has nothing to keep; it imitates a prefix cache by
counting (`rhi_mock_engine.cache`; section 5.5). A request to a generation route carries the cache
salt the gateway derived from its student, in `cache_salt`, the field vLLM's API reference names.
Its cached tokens are those its input shares, from its start, with the last input this engine
served under the same salt, short of its own last token, which an engine always computes. The
cache holds `cache_tokens` tokens and forgets the salt whose last request is the oldest first. A
request with no salt is not one the gateway forwarded: nothing of it is kept, and none of its
tokens is cached. Each format's usage reports the cached tokens where its reference puts them:

| Format | The whole input | Of which read from the cache |
| --- | --- | --- |
| Chat Completions | `prompt_tokens` | `prompt_tokens_details.cached_tokens` |
| Responses | `input_tokens` | `input_tokens_details.cached_tokens` |
| Messages | `input_tokens` plus `cache_read_input_tokens` | `cache_read_input_tokens` |

**The secret.** The four inference routes answer 401 unless the request carries
`Authorization: Bearer` and the pool's secret, which `Settings.from_env` reads from
`RHI_POOL_SECRET`. With the variable unset or empty there are no settings, so no application and
no listener. Like vLLM, the mock answers `/invocations`, `/pause` and `/tokenize` without the
secret (evidence E23): they are what the route allow-list of the internal load balancer exists to
block, and they do nothing here but answer.

**Settings**, each an environment variable with a default, or an argument of `Settings` in a test:

| Variable | Meaning | Default |
| --- | --- | --- |
| `RHI_MOCK_FIRST_BYTE_SECONDS` | wait before the first byte of an inference answer | 0 |
| `RHI_MOCK_TOKENS_PER_SECOND` | rate of a stream: one delta every 1/rate seconds | 50 |
| `RHI_MOCK_START_SECONDS` | time after start during which `/health` answers 503 | 0 |
| `RHI_MOCK_HANG` | `1`: an inference request is never answered, and `/health` still is | `0` |
| `RHI_MOCK_MAX_RUNNING` | requests generated at once; the others wait for a slot | 8 |
| `RHI_MOCK_CACHE_TOKENS` | capacity of the imitated prefix cache, in tokens | 1000000 |

**The limit and the metrics.** A request to one of the three generation routes waits for one of
`max_running` slots, in the order it came, and holds it until its answer or its stream ends, or its
client goes away; the token-counting route takes none. `GET /metrics` serves the quantities of the
contract's metrics row under the mock's names in `rhi_common.engine_metrics`, and each follows the
work done: the running and waiting requests; the time from a request's arrival to its first output
token; the gap between two output tokens of a stream (a whole answer is not paced, so it has no
gap); the input tokens of each request when it starts to run, cached or not, and the cached part
of them; each output token when it is sent; the cache use, which is the tokens the cache keeps
over its capacity. The route answers only a client on a loopback address, where the node's
collector scrapes it; any other gets the 404 of a route that does not exist.

**Shutdown.** `main` serves the application on port 8000 through `serve`. On SIGTERM the server
accepts nothing new, lets every request already accepted run to its end, with no deadline that
would cut a stream short, and then exits.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from rhi_common.engine_metrics import MOCK, Quantity, metric_names
from rhi_mock_engine.cache import PrefixCache

# Every engine listens here (system design, section 4.1).
PORT = 8000

SECRET_VARIABLE = "RHI_POOL_SECRET"
FIRST_BYTE_VARIABLE = "RHI_MOCK_FIRST_BYTE_SECONDS"
RATE_VARIABLE = "RHI_MOCK_TOKENS_PER_SECOND"
START_VARIABLE = "RHI_MOCK_START_SECONDS"
HANG_VARIABLE = "RHI_MOCK_HANG"
MAX_RUNNING_VARIABLE = "RHI_MOCK_MAX_RUNNING"
CACHE_TOKENS_VARIABLE = "RHI_MOCK_CACHE_TOKENS"

INFERENCE_ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens")
# The routes vLLM answers without its key (evidence E23).
UNGUARDED_ROUTES = ("/invocations", "/pause", "/tokenize")

# The canned turn, one output token per piece.
REASONING = ("The", " request", " is", " a", " test", ",", " so", " a", " canned", " answer", " will", " do", ".")
ANSWER = ("This", " is", " the", " mock", " engine", "'s", " canned", " answer", ".")
# The arguments of the canned tool call, as the pieces of one JSON object.
ARGUMENTS = ('{"', "canned", '":', " true", "}")

# The parts of a request that are its prompt, in any of the three formats: what an engine's template
# puts before the transcript, and the transcript.
_HEAD_KEYS = ("system", "instructions", "tools")
_TRANSCRIPT_KEYS = ("messages", "input")
_CHARACTERS_PER_TOKEN = 4
# The gateway writes the student's cache salt here, the field of vLLM's API reference (section 6.2).
CACHE_SALT_FIELD = "cache_salt"

_HANG_VALUES = {"": False, "0": False, "false": False, "1": True, "true": True}


class SettingsError(ValueError):
    """The environment does not describe a mock that may start. Its message never holds a value."""


@dataclass(frozen=True)
class Settings:
    """What the mock is started with: the pool's secret, and the settings that shape its answers."""

    secret: str
    first_byte_seconds: float = 0.0
    tokens_per_second: float = 50.0
    start_seconds: float = 0.0
    hang: bool = False
    # The coursework pool's engine runs at most 8 requests at once (system design, section 4.2).
    max_running: int = 8
    # Near the least a coursework instance is expected to hold (system design, section 4.7).
    cache_tokens: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.secret:
            raise SettingsError(f"{SECRET_VARIABLE} is unset or empty: the mock does not start without the secret")
        if not (math.isfinite(self.first_byte_seconds) and self.first_byte_seconds >= 0):
            raise SettingsError(f"{FIRST_BYTE_VARIABLE} must be a number of seconds, zero or more")
        if not (math.isfinite(self.tokens_per_second) and self.tokens_per_second > 0):
            raise SettingsError(f"{RATE_VARIABLE} must be a number of tokens per second above zero")
        if not (math.isfinite(self.start_seconds) and self.start_seconds >= 0):
            raise SettingsError(f"{START_VARIABLE} must be a number of seconds, zero or more")
        if isinstance(self.max_running, bool) or not isinstance(self.max_running, int) or self.max_running < 1:
            raise SettingsError(f"{MAX_RUNNING_VARIABLE} must be a whole number of requests, one or more")
        if isinstance(self.cache_tokens, bool) or not isinstance(self.cache_tokens, int) or self.cache_tokens < 1:
            raise SettingsError(f"{CACHE_TOKENS_VARIABLE} must be a whole number of tokens, one or more")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """The settings an environment gives. Raises `SettingsError` when it gives no secret."""

        def number(variable: str, default: float) -> float:
            try:
                return float(environ.get(variable) or default)
            except ValueError:
                raise SettingsError(f"{variable} must be a number") from None

        def whole(variable: str, default: int) -> int:
            try:
                return int(environ.get(variable) or default)
            except ValueError:
                raise SettingsError(f"{variable} must be a whole number") from None

        hang = _HANG_VALUES.get(environ.get(HANG_VARIABLE, "").strip().lower())
        if hang is None:
            raise SettingsError(f"{HANG_VARIABLE} must be 0 or 1")
        return cls(
            secret=environ.get(SECRET_VARIABLE, ""),
            first_byte_seconds=number(FIRST_BYTE_VARIABLE, cls.first_byte_seconds),
            tokens_per_second=number(RATE_VARIABLE, cls.tokens_per_second),
            start_seconds=number(START_VARIABLE, cls.start_seconds),
            hang=hang,
            max_running=whole(MAX_RUNNING_VARIABLE, cls.max_running),
            cache_tokens=whole(CACHE_TOKENS_VARIABLE, cls.cache_tokens),
        )


def _canonical(value: object) -> str:
    """A value as canonical JSON: the same text for the same value, whatever the key order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def input_tokens(body: Mapping[str, Any]) -> tuple[str, ...]:
    """The input of a request, in any of the three formats, as its tokens: a pure function of its prompt.

    The parts of the prompt in the order an engine reads them: what stands before the transcript,
    then each item of the transcript. A part is cut by itself, so the tokens of a transcript do not
    change when an item is added at its end.
    """
    parts = [_canonical({key: body[key] for key in _HEAD_KEYS if key in body})]
    for key in _TRANSCRIPT_KEYS:
        if key in body:
            items = body[key]
            parts += [_canonical(item) for item in (items if isinstance(items, list) else [items])]
    return tuple(
        part[at : at + _CHARACTERS_PER_TOKEN] for part in parts for at in range(0, len(part), _CHARACTERS_PER_TOKEN)
    )


def count_input_tokens(body: Mapping[str, Any]) -> int:
    """The number of input tokens of a request."""
    return len(input_tokens(body))


@dataclass(frozen=True)
class Turn:
    """One canned turn, before it is written in a wire format."""

    # Tells the identifiers of one turn from another's; the same for the same prompt.
    key: str
    model: str
    created: int
    # The whole input, and the part of it the cache held.
    input_tokens: int
    cached_tokens: int
    reasoning: tuple[str, ...]
    # The pieces of the text, or of the call's arguments when `tool` names a function.
    answer: tuple[str, ...]
    tool: str | None

    @property
    def output_tokens(self) -> int:
        return len(self.reasoning) + len(self.answer)


class Frame(NamedTuple):
    """One server-sent event of a stream. A token is paced at the stream's rate; the rest is not."""

    data: bytes
    token: bool = False


def _sse(data: Mapping[str, Any], event: str | None = None) -> bytes:
    name = f"event: {event}\n" if event else ""
    return f"{name}data: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _items(value: object) -> list[Mapping[str, Any]]:
    """The objects of a list in a request, and nothing for anything else: the mock does not validate."""
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


# --- Chat Completions -------------------------------------------------------------------------


def _chat_tool(body: Mapping[str, Any]) -> str | None:
    messages = _items(body.get("messages"))
    if messages and messages[-1].get("role") == "tool":
        return None
    for tool in _items(body.get("tools")):
        function = tool.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return function["name"]
    return None


def _chat_usage(turn: Turn) -> dict[str, Any]:
    return {
        "prompt_tokens": turn.input_tokens,
        "completion_tokens": turn.output_tokens,
        "total_tokens": turn.input_tokens + turn.output_tokens,
        "prompt_tokens_details": {"cached_tokens": turn.cached_tokens},
    }


def _chat_answer(turn: Turn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "reasoning": "".join(turn.reasoning)}
    if turn.tool is None:
        message["content"] = "".join(turn.answer)
    else:
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": f"call_{turn.key}",
                "type": "function",
                "function": {"name": turn.tool, "arguments": "".join(turn.answer)},
            }
        ]
    return {
        "id": f"chatcmpl-{turn.key}",
        "object": "chat.completion",
        "created": turn.created,
        "model": turn.model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop" if turn.tool is None else "tool_calls"}],
        "usage": _chat_usage(turn),
    }


def _chat_events(turn: Turn, body: Mapping[str, Any]) -> Iterator[Frame]:
    head = {
        "id": f"chatcmpl-{turn.key}",
        "object": "chat.completion.chunk",
        "created": turn.created,
        "model": turn.model,
    }

    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        return _sse({**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})

    yield Frame(chunk({"role": "assistant", "content": ""}))
    for piece in turn.reasoning:
        yield Frame(chunk({"reasoning": piece}), token=True)
    if turn.tool is None:
        for piece in turn.answer:
            yield Frame(chunk({"content": piece}), token=True)
    else:
        call = {"index": 0, "id": f"call_{turn.key}", "type": "function"}
        yield Frame(chunk({"tool_calls": [{**call, "function": {"name": turn.tool, "arguments": ""}}]}))
        for piece in turn.answer:
            yield Frame(chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}), token=True)
    yield Frame(chunk({}, "stop" if turn.tool is None else "tool_calls"))
    # The usage chunk is optional in this format: only a request that asks for it gets it.
    options = body.get("stream_options")
    if isinstance(options, Mapping) and options.get("include_usage") is True:
        yield Frame(_sse({**head, "choices": [], "usage": _chat_usage(turn)}))
    yield Frame(b"data: [DONE]\n\n")


# --- Responses --------------------------------------------------------------------------------


def _responses_tool(body: Mapping[str, Any]) -> str | None:
    items = _items(body.get("input"))
    if items and str(items[-1].get("type", "")).endswith("_call_output"):
        return None
    for tool in _items(body.get("tools")):
        if tool.get("type") == "function" and isinstance(tool.get("name"), str):
            return tool["name"]
    return None


def _responses_items(turn: Turn) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two output items of a finished turn: the reasoning, then the message or the function call."""
    reasoning = {
        "id": f"rs_{turn.key}",
        "type": "reasoning",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "".join(turn.reasoning)}],
    }
    if turn.tool is None:
        text = {"type": "output_text", "text": "".join(turn.answer), "annotations": []}
        return reasoning, {
            "id": f"msg_{turn.key}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [text],
        }
    return reasoning, {
        "id": f"fc_{turn.key}",
        "type": "function_call",
        "status": "completed",
        "call_id": f"call_{turn.key}",
        "name": turn.tool,
        "arguments": "".join(turn.answer),
    }


def _responses_answer(turn: Turn, *, completed: bool = True) -> dict[str, Any]:
    usage = {
        "input_tokens": turn.input_tokens,
        "input_tokens_details": {"cached_tokens": turn.cached_tokens},
        "output_tokens": turn.output_tokens,
        "total_tokens": turn.input_tokens + turn.output_tokens,
    }
    return {
        "id": f"resp_{turn.key}",
        "object": "response",
        "created_at": turn.created,
        "status": "completed" if completed else "in_progress",
        "model": turn.model,
        "output": list(_responses_items(turn)) if completed else [],
        "usage": usage if completed else None,
    }


def _responses_events(turn: Turn, body: Mapping[str, Any]) -> Iterator[Frame]:
    sequence = 0

    def event(name: str, token: bool = False, /, **fields: Any) -> Frame:
        nonlocal sequence
        frame = Frame(_sse({"type": name, "sequence_number": sequence, **fields}, name), token)
        sequence += 1
        return frame

    reasoning, answer = _responses_items(turn)
    yield event("response.created", response=_responses_answer(turn, completed=False))
    yield event("response.in_progress", response=_responses_answer(turn, completed=False))

    at = {"item_id": reasoning["id"], "output_index": 0, "content_index": 0}
    yield event("response.output_item.added", output_index=0, item={**reasoning, "content": []})
    yield event("response.content_part.added", **at, part={"type": "reasoning_text", "text": ""})
    for piece in turn.reasoning:
        yield event("response.reasoning_text.delta", True, **at, delta=piece)
    yield event("response.reasoning_text.done", **at, text="".join(turn.reasoning))
    yield event("response.content_part.done", **at, part=reasoning["content"][0])
    yield event("response.output_item.done", output_index=0, item=reasoning)

    if turn.tool is None:
        at = {"item_id": answer["id"], "output_index": 1, "content_index": 0}
        started = {**answer, "status": "in_progress", "content": []}
        yield event("response.output_item.added", output_index=1, item=started)
        yield event("response.content_part.added", **at, part={**answer["content"][0], "text": ""})
        for piece in turn.answer:
            yield event("response.output_text.delta", True, **at, delta=piece)
        yield event("response.output_text.done", **at, text="".join(turn.answer))
        yield event("response.content_part.done", **at, part=answer["content"][0])
    else:
        at = {"item_id": answer["id"], "output_index": 1}
        started = {**answer, "status": "in_progress", "arguments": ""}
        yield event("response.output_item.added", output_index=1, item=started)
        for piece in turn.answer:
            yield event("response.function_call_arguments.delta", True, **at, delta=piece)
        yield event("response.function_call_arguments.done", **at, arguments="".join(turn.answer))
    yield event("response.output_item.done", output_index=1, item=answer)
    # Usage travels in the terminal event, and nowhere else.
    yield event("response.completed", response=_responses_answer(turn))


# --- Messages ---------------------------------------------------------------------------------


def _messages_tool(body: Mapping[str, Any]) -> str | None:
    messages = _items(body.get("messages"))
    if messages and any(block.get("type") == "tool_result" for block in _items(messages[-1].get("content"))):
        return None
    for tool in _items(body.get("tools")):
        if isinstance(tool.get("name"), str):
            return tool["name"]
    return None


def _messages_answer(turn: Turn, *, finished: bool = True) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "".join(turn.reasoning), "signature": turn.key}]
    if turn.tool is None:
        content.append({"type": "text", "text": "".join(turn.answer)})
    else:
        tool_input = json.loads("".join(turn.answer))
        content.append({"type": "tool_use", "id": f"toolu_{turn.key}", "name": turn.tool, "input": tool_input})
    stop_reason = "end_turn" if turn.tool is None else "tool_use"
    # In this format `input_tokens` is the part of the input that was not read from the cache.
    usage = {
        "input_tokens": turn.input_tokens - turn.cached_tokens,
        "cache_read_input_tokens": turn.cached_tokens,
        "output_tokens": turn.output_tokens if finished else 0,
    }
    return {
        "id": f"msg_{turn.key}",
        "type": "message",
        "role": "assistant",
        "model": turn.model,
        "content": content if finished else [],
        "stop_reason": stop_reason if finished else None,
        "stop_sequence": None,
        "usage": usage,
    }


def _messages_events(turn: Turn, body: Mapping[str, Any]) -> Iterator[Frame]:
    def event(name: str, token: bool = False, /, **fields: Any) -> Frame:
        return Frame(_sse({"type": name, **fields}, name), token)

    def delta(index: int, kind: str, field: str, piece: str, token: bool = True) -> Frame:
        return event("content_block_delta", token, index=index, delta={"type": kind, field: piece})

    finished = _messages_answer(turn)
    # Usage travels in two events: the input here, the output in `message_delta`.
    yield event("message_start", message=_messages_answer(turn, finished=False))

    yield event("content_block_start", index=0, content_block={"type": "thinking", "thinking": "", "signature": ""})
    for piece in turn.reasoning:
        yield delta(0, "thinking_delta", "thinking", piece)
    yield delta(0, "signature_delta", "signature", turn.key, token=False)
    yield event("content_block_stop", index=0)

    if turn.tool is None:
        yield event("content_block_start", index=1, content_block={"type": "text", "text": ""})
        for piece in turn.answer:
            yield delta(1, "text_delta", "text", piece)
    else:
        yield event("content_block_start", index=1, content_block={**finished["content"][1], "input": {}})
        for piece in turn.answer:
            yield delta(1, "input_json_delta", "partial_json", piece)
    yield event("content_block_stop", index=1)

    stop = {"stop_reason": finished["stop_reason"], "stop_sequence": None}
    yield event("message_delta", delta=stop, usage={"output_tokens": turn.output_tokens})
    yield event("message_stop")


# --- The application --------------------------------------------------------------------------


class _Wire(NamedTuple):
    """How one wire format chooses and writes the canned turn."""

    tool: Callable[[Mapping[str, Any]], str | None]
    answer: Callable[[Turn], dict[str, Any]]
    events: Callable[[Turn, Mapping[str, Any]], Iterator[Frame]]


_WIRES = {
    "/v1/chat/completions": _Wire(_chat_tool, _chat_answer, _chat_events),
    "/v1/responses": _Wire(_responses_tool, _responses_answer, _responses_events),
    "/v1/messages": _Wire(_messages_tool, _messages_answer, _messages_events),
}


def _turn(body: Mapping[str, Any], tool: str | None, tokens: Sequence[str], cached: int) -> Turn:
    model = body.get("model")
    return Turn(
        key=hashlib.sha256("".join(tokens).encode()).hexdigest()[:24],
        model=model if isinstance(model, str) else "mock",
        created=int(time.time()),
        input_tokens=len(tokens),
        cached_tokens=cached,
        reasoning=REASONING,
        answer=ANSWER if tool is None else ARGUMENTS,
        tool=tool,
    )


def _error(status: int, kind: str, message: str) -> JSONResponse:
    # One shape for every route: Anthropic's error object, which holds OpenAI's `error` member.
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


class _Slots:
    """`size` slots, handed out in the order they are asked for.

    Bound to no event loop, unlike `asyncio.Semaphore`, so one application serves under any loop.
    It never calls the application's `sleep`, which a test replaces.
    """

    def __init__(self, size: int) -> None:
        self._free = size
        self._waiters: deque[asyncio.Future[None]] = deque()

    async def take(self) -> None:
        if self._free and not self._waiters:
            self._free -= 1
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            # A slot handed over as the request went away goes to the next one; a waiter that was
            # cancelled stays in the queue, done, and `give_back` passes over it.
            if waiter.done() and not waiter.cancelled():
                self.give_back()
            waiter.cancel()
            raise

    def give_back(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._free += 1


# Seconds. Finer than Prometheus' defaults where a stream's pace lives (system design, section 7.2).
_FIRST_TOKEN_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0)
_TOKEN_GAP_BUCKETS = (0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0)


class _Metrics:
    """The quantities of the contract's metrics row, under the mock's names, in a registry of one application."""

    def __init__(self) -> None:
        names = metric_names(MOCK)
        self.registry = CollectorRegistry(auto_describe=True)
        registry = self.registry
        self.running = Gauge(names[Quantity.RUNNING_REQUESTS], "Requests being generated.", registry=registry)
        self.waiting = Gauge(names[Quantity.WAITING_REQUESTS], "Requests waiting for a slot.", registry=registry)
        self.first_token = Histogram(
            names[Quantity.TIME_TO_FIRST_TOKEN],
            "Seconds from a request's arrival to its first output token.",
            buckets=_FIRST_TOKEN_BUCKETS,
            registry=registry,
        )
        self.token_gap = Histogram(
            names[Quantity.TIME_PER_OUTPUT_TOKEN],
            "Seconds between two output tokens of a stream.",
            buckets=_TOKEN_GAP_BUCKETS,
            registry=registry,
        )
        self.input_tokens = Counter(names[Quantity.INPUT_TOKENS], "Input tokens of the requests run.", registry=registry)
        self.cached_input_tokens = Counter(
            names[Quantity.CACHED_INPUT_TOKENS], "Input tokens the cache held, of those.", registry=registry
        )
        self.output_tokens = Counter(names[Quantity.OUTPUT_TOKENS], "Output tokens sent.", registry=registry)
        self.cache_usage = Gauge(names[Quantity.CACHE_USAGE], "Share of the cache in use.", registry=registry)


class _Holding:
    """An answer that holds a slot until it has been sent, or its client has gone away."""

    def __init__(self, response: Response, release: Callable[[], None]) -> None:
        self._response = response
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._response(scope, receive, send)
        finally:
            self._release()


def _loopback(request: Request) -> bool:
    """Whether the request comes from a loopback address. A client with no address does not."""
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> Starlette:
    """The mock engine as an ASGI application. `clock` and `sleep` are arguments so that a test waits for nothing."""
    started = clock()
    secret = settings.secret.encode()
    slots = _Slots(settings.max_running)
    metrics = _Metrics()
    cache = PrefixCache(settings.cache_tokens)

    def authorized(request: Request) -> bool:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(credential.strip().encode(), secret)

    async def paced(frames: Iterator[Frame], arrived: float) -> AsyncIterator[bytes]:
        last: float | None = None
        for frame in frames:
            if frame.token:
                await sleep(1 / settings.tokens_per_second)
                now = clock()
                if last is None:
                    metrics.first_token.observe(now - arrived)
                else:
                    metrics.token_gap.observe(now - last)
                last = now
                metrics.output_tokens.inc()
            yield frame.data

    def inference(
        respond: Callable[[Mapping[str, Any]], Awaitable[Response | _Holding]],
    ) -> Callable[[Request], Awaitable[Response | _Holding]]:
        async def endpoint(request: Request) -> Response | _Holding:
            if not authorized(request):
                return _error(401, "authentication_error", "This route requires the pool's bearer secret.")
            if settings.hang:
                # Nothing is ever sent. The wait ends with the connection, so that a client that
                # gives up does not leave a task behind.
                while (await request.receive())["type"] != "http.disconnect":
                    pass
                return Response()
            try:
                body = json.loads(await request.body())
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return _error(400, "invalid_request_error", "The body must be a JSON object.")
            return await respond(body)

        return endpoint

    def answer_in(wire: _Wire) -> Callable[[Mapping[str, Any]], Awaitable[_Holding]]:
        async def respond(body: Mapping[str, Any]) -> _Holding:
            arrived = clock()
            metrics.waiting.inc()
            try:
                await slots.take()
            finally:
                metrics.waiting.dec()
            metrics.running.inc()
            held = True

            def release() -> None:
                nonlocal held
                if held:
                    held = False
                    metrics.running.dec()
                    slots.give_back()

            try:
                tokens = input_tokens(body)
                salt = body.get(CACHE_SALT_FIELD)
                # Counted when the request starts to run: that is when an engine reads its prompt.
                cached = cache.serve(salt, tokens) if isinstance(salt, str) and salt else 0
                turn = _turn(body, wire.tool(body), tokens, cached)
                metrics.input_tokens.inc(turn.input_tokens)
                metrics.cached_input_tokens.inc(cached)
                metrics.cache_usage.set(cache.use)
                await sleep(settings.first_byte_seconds)
                if body.get("stream") is True:
                    response: Response = StreamingResponse(
                        paced(wire.events(turn, body), arrived), media_type="text/event-stream"
                    )
                else:
                    metrics.first_token.observe(clock() - arrived)
                    metrics.output_tokens.inc(turn.output_tokens)
                    response = JSONResponse(wire.answer(turn))
            except BaseException:
                release()
                raise
            return _Holding(response, release)

        return respond

    async def count_tokens(body: Mapping[str, Any]) -> Response:
        await sleep(settings.first_byte_seconds)
        return JSONResponse({"input_tokens": count_input_tokens(body)})

    async def health(request: Request) -> Response:
        return Response(status_code=200 if clock() - started >= settings.start_seconds else 503)

    async def exposition(request: Request) -> Response:
        if not _loopback(request):
            # The answer of a route that does not exist.
            return PlainTextResponse("Not Found", status_code=404)
        return Response(generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def unguarded(request: Request) -> Response:
        return JSONResponse({"route": request.url.path, "secret_required": False})

    return Starlette(
        routes=[
            *(Route(path, inference(answer_in(wire)), methods=["POST"]) for path, wire in _WIRES.items()),
            Route("/v1/messages/count_tokens", inference(count_tokens), methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/metrics", exposition, methods=["GET"]),
            *(Route(path, unguarded, methods=["POST"]) for path in UNGUARDED_ROUTES),
        ]
    )


def serve(settings: Settings, **where: Any) -> None:
    """Serve the mock where `where` says: uvicorn's `host` and `port`, or `uds`.

    On SIGTERM uvicorn stops accepting, waits for every request it has accepted to end, and exits.
    `timeout_graceful_shutdown=None` says so: no deadline cuts a stream in flight.
    """
    uvicorn.run(create_app(settings), **where, timeout_graceful_shutdown=None)


def main() -> None:
    """Serve the mock on port 8000. With no secret in the environment it raises before anything listens."""
    serve(Settings.from_env(os.environ), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
