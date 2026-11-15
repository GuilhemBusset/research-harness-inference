"""The mock engine: the engine contract's routes, answered with canned streams (system design, section 4.1).

A CPU process that the gateway, the scaler and the tests can use in place of a model server. It
serves the four inference routes in their three wire formats, streamed and not, and `GET /health`.

| Route | Answer |
| --- | --- |
| `POST /v1/chat/completions` | a completion, or its chunks, the optional usage chunk and `[DONE]` |
| `POST /v1/responses` | a response, or its events from `response.created` to `response.completed` |
| `POST /v1/messages` | a message, or its events from `message_start` to `message_stop` |
| `POST /v1/messages/count_tokens` | `{"input_tokens": n}` |
| `GET /health` | 503 until the start delay has passed, then 200 |
| `POST /invocations`, `/pause`, `/tokenize` | 200, without the secret |

**The canned turn.** The coursework model reasons before it answers (section 4.2), so every turn
is reasoning, then an answer, then usage. The answer is a call of the first function the request
declares, unless the request declares none or already ends with a tool's result, and then it is
text: a harness that runs the call and sends its result back gets a turn that ends. Each piece of
`REASONING`, `ANSWER` and `ARGUMENTS` is one output token and, in a stream, one delta.

**The input count.** `count_input_tokens` is a pure function of the request: one token for every
four characters of its prompt (system text, messages or input, and tools), written as canonical
JSON. The usage of an answer and the token-counting route both report it, so the same request
always costs the same.

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

`main` serves the application on port 8000. Metrics, the limit on running requests, the handling
of SIGTERM and the image are not here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# Every engine listens here (system design, section 4.1).
PORT = 8000

SECRET_VARIABLE = "RHI_POOL_SECRET"
FIRST_BYTE_VARIABLE = "RHI_MOCK_FIRST_BYTE_SECONDS"
RATE_VARIABLE = "RHI_MOCK_TOKENS_PER_SECOND"
START_VARIABLE = "RHI_MOCK_START_SECONDS"
HANG_VARIABLE = "RHI_MOCK_HANG"

INFERENCE_ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens")
# The routes vLLM answers without its key (evidence E23).
UNGUARDED_ROUTES = ("/invocations", "/pause", "/tokenize")

# The canned turn, one output token per piece.
REASONING = ("The", " request", " is", " a", " test", ",", " so", " a", " canned", " answer", " will", " do", ".")
ANSWER = ("This", " is", " the", " mock", " engine", "'s", " canned", " answer", ".")
# The arguments of the canned tool call, as the pieces of one JSON object.
ARGUMENTS = ('{"', "canned", '":', " true", "}")

# The parts of a request that are its prompt, in any of the three formats.
_PROMPT_KEYS = ("system", "instructions", "messages", "input", "tools")
_CHARACTERS_PER_TOKEN = 4

_HANG_VALUES = {"": False, "0": False, "false": False, "1": True, "true": True}


class SettingsError(ValueError):
    """The environment does not describe a mock that may start. Its message never holds a value."""


@dataclass(frozen=True)
class Settings:
    """What the mock is started with: the pool's secret, and the four settings that shape its answers."""

    secret: str
    first_byte_seconds: float = 0.0
    tokens_per_second: float = 50.0
    start_seconds: float = 0.0
    hang: bool = False

    def __post_init__(self) -> None:
        if not self.secret:
            raise SettingsError(f"{SECRET_VARIABLE} is unset or empty: the mock does not start without the secret")
        if not (math.isfinite(self.first_byte_seconds) and self.first_byte_seconds >= 0):
            raise SettingsError(f"{FIRST_BYTE_VARIABLE} must be a number of seconds, zero or more")
        if not (math.isfinite(self.tokens_per_second) and self.tokens_per_second > 0):
            raise SettingsError(f"{RATE_VARIABLE} must be a number of tokens per second above zero")
        if not (math.isfinite(self.start_seconds) and self.start_seconds >= 0):
            raise SettingsError(f"{START_VARIABLE} must be a number of seconds, zero or more")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """The settings an environment gives. Raises `SettingsError` when it gives no secret."""

        def number(variable: str, default: float) -> float:
            try:
                return float(environ.get(variable) or default)
            except ValueError:
                raise SettingsError(f"{variable} must be a number") from None

        hang = _HANG_VALUES.get(environ.get(HANG_VARIABLE, "").strip().lower())
        if hang is None:
            raise SettingsError(f"{HANG_VARIABLE} must be 0 or 1")
        return cls(
            secret=environ.get(SECRET_VARIABLE, ""),
            first_byte_seconds=number(FIRST_BYTE_VARIABLE, cls.first_byte_seconds),
            tokens_per_second=number(RATE_VARIABLE, cls.tokens_per_second),
            start_seconds=number(START_VARIABLE, cls.start_seconds),
            hang=hang,
        )


def _prompt(body: Mapping[str, Any]) -> str:
    """The request's prompt as canonical JSON: the same text for the same prompt, whatever the key order."""
    prompt = {key: body[key] for key in _PROMPT_KEYS if key in body}
    return json.dumps(prompt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def count_input_tokens(body: Mapping[str, Any]) -> int:
    """The input tokens of a request, in any of the three formats: a pure function of its prompt."""
    return math.ceil(len(_prompt(body)) / _CHARACTERS_PER_TOKEN)


@dataclass(frozen=True)
class Turn:
    """One canned turn, before it is written in a wire format."""

    # Tells the identifiers of one turn from another's; the same for the same prompt.
    key: str
    model: str
    created: int
    input_tokens: int
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


def _chat_usage(turn: Turn) -> dict[str, int]:
    return {
        "prompt_tokens": turn.input_tokens,
        "completion_tokens": turn.output_tokens,
        "total_tokens": turn.input_tokens + turn.output_tokens,
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
    return {
        "id": f"msg_{turn.key}",
        "type": "message",
        "role": "assistant",
        "model": turn.model,
        "content": content if finished else [],
        "stop_reason": stop_reason if finished else None,
        "stop_sequence": None,
        "usage": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens if finished else 0},
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


def _turn(body: Mapping[str, Any], tool: str | None) -> Turn:
    model = body.get("model")
    return Turn(
        key=hashlib.sha256(_prompt(body).encode()).hexdigest()[:24],
        model=model if isinstance(model, str) else "mock",
        created=int(time.time()),
        input_tokens=count_input_tokens(body),
        reasoning=REASONING,
        answer=ANSWER if tool is None else ARGUMENTS,
        tool=tool,
    )


def _error(status: int, kind: str, message: str) -> JSONResponse:
    # One shape for every route: Anthropic's error object, which holds OpenAI's `error` member.
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


def create_app(
    settings: Settings,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> Starlette:
    """The mock engine as an ASGI application. `clock` and `sleep` are arguments so that a test waits for nothing."""
    started = clock()
    secret = settings.secret.encode()

    def authorized(request: Request) -> bool:
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(credential.strip().encode(), secret)

    async def paced(frames: Iterator[Frame]) -> AsyncIterator[bytes]:
        for frame in frames:
            if frame.token:
                await sleep(1 / settings.tokens_per_second)
            yield frame.data

    def inference(respond: Callable[[Mapping[str, Any]], Response]) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
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
            await sleep(settings.first_byte_seconds)
            return respond(body)

        return endpoint

    def answer_in(wire: _Wire) -> Callable[[Mapping[str, Any]], Response]:
        def respond(body: Mapping[str, Any]) -> Response:
            turn = _turn(body, wire.tool(body))
            if body.get("stream") is True:
                return StreamingResponse(paced(wire.events(turn, body)), media_type="text/event-stream")
            return JSONResponse(wire.answer(turn))

        return respond

    def count_tokens(body: Mapping[str, Any]) -> Response:
        return JSONResponse({"input_tokens": count_input_tokens(body)})

    async def health(request: Request) -> Response:
        return Response(status_code=200 if clock() - started >= settings.start_seconds else 503)

    async def unguarded(request: Request) -> Response:
        return JSONResponse({"route": request.url.path, "secret_required": False})

    return Starlette(
        routes=[
            *(Route(path, inference(answer_in(wire)), methods=["POST"]) for path, wire in _WIRES.items()),
            Route("/v1/messages/count_tokens", inference(count_tokens), methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            *(Route(path, unguarded, methods=["POST"]) for path in UNGUARDED_ROUTES),
        ]
    )


def main() -> None:
    """Serve the mock on port 8000. With no secret in the environment it raises before anything listens."""
    settings = Settings.from_env(os.environ)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
