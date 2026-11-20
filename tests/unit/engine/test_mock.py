"""The mock engine, called in process (`rhi_mock_engine.app`; system design, section 4.1).

Each wire format has one reader here, for its JSON answer and for its stream, which reduces an
answer to what the issue names: the order of its parts, the reasoning, the text or the tool call,
and the usage. A reader takes the fields from where its format puts them, so an answer in another
format's shape does not read. The streams are read with the gateway's own reader of server-sent
events. No test waits: the clock and the sleep of the application are arguments.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.types import Message

from rhi_gateway.sse import SseReader
from rhi_mock_engine import app as mock
from rhi_mock_engine.app import Settings, SettingsError, count_input_tokens, create_app

# A made-up value, not a credential: the mock only compares it with what a request carries.
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])
AUTHORIZED = {"Authorization": f"Bearer {SECRET}"}

CHAT, RESPONSES, MESSAGES, COUNT = mock.INFERENCE_ROUTES
FORMATS = (CHAT, RESPONSES, MESSAGES)

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}}
REQUESTS: dict[str, dict[str, Any]] = {
    CHAT: {"model": "coursework", "messages": [{"role": "user", "content": "Read the file notes.txt"}]},
    RESPONSES: {"model": "coursework", "instructions": "Be brief.", "input": "Read the file notes.txt"},
    MESSAGES: {
        "model": "coursework",
        "max_tokens": 64,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "Read the file notes.txt"}],
    },
}
# How each format declares a function, and how its history ends with that function's result.
TOOLS: dict[str, list[dict[str, Any]]] = {
    CHAT: [{"type": "function", "function": {"name": "read_file", "parameters": SCHEMA}}],
    RESPONSES: [{"type": "function", "name": "read_file", "parameters": SCHEMA}],
    MESSAGES: [{"name": "read_file", "input_schema": SCHEMA}],
}
TOOL_RESULTS: dict[str, tuple[str, dict[str, Any]]] = {
    CHAT: ("messages", {"role": "tool", "tool_call_id": "call_1", "content": "the notes"}),
    RESPONSES: ("input", {"type": "function_call_output", "call_id": "call_1", "output": "the notes"}),
    MESSAGES: (
        "messages",
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "the notes"}]},
    ),
}


class Seen(NamedTuple):
    """An answer, whatever its format and whether it was streamed."""

    parts: list[str]  # "reasoning", "text" and "tool", in the order they first appear
    reasoning: str
    text: str
    tool: tuple[str, str] | None  # the function's name, and its arguments as JSON text
    input_tokens: int
    output_tokens: int


class Parts:
    """Collects the pieces of an answer in the order they arrive."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.pieces: dict[str, list[str]] = {"reasoning": [], "text": [], "tool": []}
        self.name: str | None = None

    def add(self, part: str, piece: str) -> None:
        if part not in self.order:
            self.order.append(part)
        self.pieces[part].append(piece)

    def seen(self, input_tokens: int, output_tokens: int) -> Seen:
        joined = {part: "".join(pieces) for part, pieces in self.pieces.items()}
        tool = (self.name, joined["tool"]) if self.name is not None else None
        return Seen(self.order, joined["reasoning"], joined["text"], tool, input_tokens, output_tokens)


def read_chat(answer: dict[str, Any]) -> Seen:
    assert answer["object"] == "chat.completion"
    [choice] = answer["choices"]
    message, parts = choice["message"], Parts()
    parts.add("reasoning", message["reasoning"])
    if choice["finish_reason"] == "tool_calls":
        [call] = message["tool_calls"]
        parts.name = call["function"]["name"]
        parts.add("tool", call["function"]["arguments"])
    else:
        assert choice["finish_reason"] == "stop"
        parts.add("text", message["content"])
    return parts.seen(answer["usage"]["prompt_tokens"], answer["usage"]["completion_tokens"])


def read_chat_stream(events: list[tuple[str | None, str]]) -> Seen:
    assert all(name is None for name, _ in events)
    assert events[-1][1] == "[DONE]"
    chunks = [json.loads(data) for _, data in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    # The usage chunk is the last one, and the only one with no choice.
    usage = chunks.pop()
    assert usage["choices"] == []
    parts, finish_reasons = Parts(), []
    for chunk in chunks:
        [choice] = chunk["choices"]
        finish_reasons.append(choice["finish_reason"])
        delta = choice["delta"]
        if delta.get("reasoning"):
            parts.add("reasoning", delta["reasoning"])
        if delta.get("content"):
            parts.add("text", delta["content"])
        for call in delta.get("tool_calls", []):
            function = call["function"]
            parts.name = function.get("name", parts.name)
            parts.add("tool", function["arguments"])
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert finish_reasons[:-1] == [None] * (len(chunks) - 1)
    assert finish_reasons[-1] == ("stop" if parts.name is None else "tool_calls")
    return parts.seen(usage["usage"]["prompt_tokens"], usage["usage"]["completion_tokens"])


def read_responses(answer: dict[str, Any]) -> Seen:
    assert answer["object"] == "response"
    assert answer["status"] == "completed"
    parts = Parts()
    for item in answer["output"]:
        if item["type"] == "reasoning":
            parts.add("reasoning", "".join(content["text"] for content in item["content"]))
        elif item["type"] == "function_call":
            parts.name = item["name"]
            parts.add("tool", item["arguments"])
        else:
            assert item["type"] == "message"
            parts.add("text", "".join(content["text"] for content in item["content"]))
    return parts.seen(answer["usage"]["input_tokens"], answer["usage"]["output_tokens"])


def read_responses_stream(events: list[tuple[str | None, str]]) -> Seen:
    loaded = [json.loads(data) for _, data in events]
    names = [name for name, _ in events]
    assert names == [event["type"] for event in loaded]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    assert [event["sequence_number"] for event in loaded] == list(range(len(loaded)))
    parts = Parts()
    for event in loaded:
        if event["type"] == "response.reasoning_text.delta":
            parts.add("reasoning", event["delta"])
        elif event["type"] == "response.output_text.delta":
            parts.add("text", event["delta"])
        elif event["type"] == "response.function_call_arguments.delta":
            parts.add("tool", event["delta"])
        elif event["type"] == "response.output_item.added" and event["item"]["type"] == "function_call":
            parts.name = event["item"]["name"]
    # The terminal event carries the whole response: what the deltas built, and the usage.
    whole = read_responses(loaded[-1]["response"])
    streamed = parts.seen(whole.input_tokens, whole.output_tokens)
    assert streamed == whole
    return streamed


def read_messages(answer: dict[str, Any]) -> Seen:
    assert answer["type"] == "message"
    assert answer["role"] == "assistant"
    parts = Parts()
    for block in answer["content"]:
        if block["type"] == "thinking":
            parts.add("reasoning", block["thinking"])
        elif block["type"] == "tool_use":
            parts.name = block["name"]
            parts.add("tool", json.dumps(block["input"]))
        else:
            assert block["type"] == "text"
            parts.add("text", block["text"])
    assert answer["stop_reason"] == ("end_turn" if parts.name is None else "tool_use")
    return parts.seen(answer["usage"]["input_tokens"], answer["usage"]["output_tokens"])


def read_messages_stream(events: list[tuple[str | None, str]]) -> Seen:
    loaded = [json.loads(data) for _, data in events]
    names = [name for name, _ in events]
    assert names == [event["type"] for event in loaded]
    assert names[0] == "message_start"
    assert names[-2:] == ["message_delta", "message_stop"]
    assert names.count("content_block_start") == names.count("content_block_stop") == 2
    parts = Parts()
    for event in loaded:
        if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            parts.name = event["content_block"]["name"]
        elif event["type"] == "content_block_delta":
            delta = event["delta"]
            if delta["type"] == "thinking_delta":
                parts.add("reasoning", delta["thinking"])
            elif delta["type"] == "text_delta":
                parts.add("text", delta["text"])
            elif delta["type"] == "input_json_delta":
                parts.add("tool", delta["partial_json"])
    assert loaded[-2]["delta"]["stop_reason"] == ("end_turn" if parts.name is None else "tool_use")
    # The usage is in two events: the input at the start, the output at the end.
    return parts.seen(loaded[0]["message"]["usage"]["input_tokens"], loaded[-2]["usage"]["output_tokens"])


READERS = {
    CHAT: (read_chat, read_chat_stream),
    RESPONSES: (read_responses, read_responses_stream),
    MESSAGES: (read_messages, read_messages_stream),
}


async def no_sleep(seconds: float) -> None:
    del seconds


class Engine:
    """The application, called over ASGI in this process: no socket, and one whole answer for each call."""

    def __init__(self, app: Starlette) -> None:
        self.app = app

    def request(self, method: str, route: str, **arguments: Any) -> httpx.Response:
        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://engine") as http:
                return await http.request(method, route, **arguments)

        return asyncio.run(call())

    def post(self, route: str, *, json: object, headers: dict[str, str] | None = None) -> httpx.Response:
        return self.request("POST", route, json=json, headers=headers)

    def get(self, route: str) -> httpx.Response:
        return self.request("GET", route)


def client(**settings: Any) -> Engine:
    return Engine(create_app(Settings(secret=SECRET, **settings), sleep=no_sleep))


def ask(route: str, request: dict[str, Any], *, stream: bool, engine: Engine | None = None) -> Seen:
    """Sends an authorized request and reads the answer with its format's reader."""
    read, read_stream = READERS[route]
    body = {**request, "stream": stream}
    if stream and route == CHAT:
        body["stream_options"] = {"include_usage": True}
    response = (engine or client()).post(route, json=body, headers=AUTHORIZED)
    assert response.status_code == 200
    if not stream:
        assert response.headers["content-type"] == "application/json"
        return read(response.json())
    assert response.headers["content-type"].startswith("text/event-stream")
    return read_stream([(event.name, event.data) for event in SseReader().feed(response.content)])


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
@pytest.mark.parametrize("route", FORMATS)
def test_each_format_answers_with_reasoning_then_text_then_usage(route: str, stream: bool) -> None:
    seen = ask(route, REQUESTS[route], stream=stream)

    assert seen.parts == ["reasoning", "text"]
    assert seen.reasoning
    assert seen.text
    assert seen.tool is None
    assert seen.input_tokens == count_input_tokens(REQUESTS[route])
    assert seen.output_tokens > 0
    # The stream and the whole answer are the same turn.
    assert seen == ask(route, REQUESTS[route], stream=not stream)


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
@pytest.mark.parametrize("route", FORMATS)
def test_a_request_that_declares_tools_gets_reasoning_then_a_tool_call(route: str, stream: bool) -> None:
    request = {**REQUESTS[route], "tools": TOOLS[route]}

    seen = ask(route, request, stream=stream)

    assert seen.parts == ["reasoning", "tool"]
    assert seen.reasoning
    assert seen.tool is not None
    name, arguments = seen.tool
    assert name == "read_file"
    assert isinstance(json.loads(arguments), dict)
    assert seen.input_tokens == count_input_tokens(request)
    assert seen == ask(route, request, stream=not stream)


@pytest.mark.parametrize("route", FORMATS)
def test_after_a_tool_result_the_turn_is_text(route: str) -> None:
    key, result = TOOL_RESULTS[route]
    history = REQUESTS[route][key]
    earlier = history if isinstance(history, list) else [{"role": "user", "content": history}]
    request = {**REQUESTS[route], "tools": TOOLS[route], key: [*earlier, result]}

    assert ask(route, request, stream=True).parts == ["reasoning", "text"]


def test_chat_completions_sends_the_usage_chunk_only_when_asked() -> None:
    response = client().post(CHAT, json={**REQUESTS[CHAT], "stream": True}, headers=AUTHORIZED)

    events = SseReader().feed(response.content)
    assert events[-1].data == "[DONE]"
    assert all("usage" not in json.loads(event.data) for event in events[:-1])


def test_the_input_count_is_derived_from_the_request() -> None:
    engine = client()
    request = REQUESTS[MESSAGES]

    def counted(body: dict[str, Any]) -> int:
        response = engine.post(COUNT, json=body, headers=AUTHORIZED)
        assert response.status_code == 200
        assert response.json().keys() == {"input_tokens"}
        return response.json()["input_tokens"]

    count = counted(request)
    assert count > 0
    # The token-counting route and the usage of an answer agree.
    assert count == ask(MESSAGES, request, stream=False, engine=engine).input_tokens
    # The same request counts the same, whatever the order of its keys and whatever is not its prompt.
    assert counted(request) == count
    assert counted(dict(reversed(request.items()))) == count
    assert counted({**request, "max_tokens": 4096, "stream": True}) == count
    # A longer prompt counts more.
    longer = {**request, "messages": [*request["messages"], {"role": "user", "content": "And summarise it."}]}
    assert counted(longer) > count
    assert counted({**request, "tools": TOOLS[MESSAGES]}) > count


@pytest.mark.parametrize("route", mock.INFERENCE_ROUTES)
def test_an_inference_route_refuses_a_request_without_the_secret(route: str) -> None:
    engine = client()
    request = REQUESTS.get(route, REQUESTS[MESSAGES])

    assert engine.post(route, json=request).status_code == 401
    assert engine.post(route, json=request, headers={"Authorization": f"Bearer not-{SECRET}"}).status_code == 401
    assert engine.post(route, json=request, headers={"Authorization": SECRET}).status_code == 401
    assert engine.post(route, json=request, headers=AUTHORIZED).status_code == 200


def test_unguarded_routes_like_vllm() -> None:
    engine = client()

    for route in ("/invocations", "/pause", "/tokenize"):
        assert engine.post(route, json={"prompt": "anything"}).status_code == 200, route
    # The same call of an inference route is refused: the secret guards those four only.
    assert engine.post(CHAT, json=REQUESTS[CHAT]).status_code == 401


@pytest.mark.parametrize("environ", [{}, {"RHI_POOL_SECRET": ""}], ids=["unset", "empty"])
def test_the_mock_refuses_to_start_without_the_secret(environ: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: served.append((args, kwargs)))
    monkeypatch.delenv("RHI_POOL_SECRET", raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SettingsError):
        Settings.from_env(environ)
    with pytest.raises(SettingsError):
        mock.main()
    assert served == []


def test_the_mock_serves_on_port_8000_with_the_secret_of_its_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append((app, kwargs)))
    monkeypatch.setenv("RHI_POOL_SECRET", SECRET)

    mock.main()

    [(app, kwargs)] = served
    assert kwargs["port"] == 8000
    engine = Engine(app)
    assert engine.post(CHAT, json=REQUESTS[CHAT], headers=AUTHORIZED).status_code == 200
    assert engine.post(CHAT, json=REQUESTS[CHAT], headers={"Authorization": "Bearer another"}).status_code == 401


def test_health_answers_503_before_the_start_delay_and_200_after() -> None:
    now = 100.0
    engine = Engine(create_app(Settings(secret=SECRET, start_seconds=30), clock=lambda: now))

    assert engine.get("/health").status_code == 503
    now = 129.9
    assert engine.get("/health").status_code == 503
    now = 130.0
    # No secret: the load balancer's health check carries none.
    assert engine.get("/health").status_code == 200


def test_the_start_delay_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(app))
    # The application's clock, moved by the test: nothing waits.
    now = 100.0
    monkeypatch.setattr(mock, "create_app", lambda settings: create_app(settings, clock=lambda: now))
    monkeypatch.setenv("RHI_POOL_SECRET", SECRET)
    # The argument wins over the environment's start delay.
    monkeypatch.setenv("RHI_MOCK_START_SECONDS", "30")

    mock.main(["--start-delay-seconds", "2"])

    [app] = served
    engine = Engine(app)
    assert engine.get("/health").status_code == 503
    now = 101.9
    assert engine.get("/health").status_code == 503
    now = 102.0
    assert engine.get("/health").status_code == 200


def test_the_settings_come_from_the_environment() -> None:
    assert Settings.from_env({"RHI_POOL_SECRET": SECRET}) == Settings(secret=SECRET)
    environ = {
        "RHI_POOL_SECRET": SECRET,
        "RHI_MOCK_FIRST_BYTE_SECONDS": "1.5",
        "RHI_MOCK_TOKENS_PER_SECOND": "20",
        "RHI_MOCK_START_SECONDS": "45",
        "RHI_MOCK_HANG": "1",
    }

    assert Settings.from_env(environ) == Settings(
        secret=SECRET, first_byte_seconds=1.5, tokens_per_second=20, start_seconds=45, hang=True
    )
    with pytest.raises(SettingsError):
        Settings.from_env({**environ, "RHI_MOCK_TOKENS_PER_SECOND": "0"})
    with pytest.raises(SettingsError):
        Settings.from_env({**environ, "RHI_MOCK_HANG": "perhaps"})


@pytest.mark.parametrize("route", FORMATS)
def test_the_first_byte_waits_and_the_stream_keeps_its_rate(route: str) -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    settings = Settings(secret=SECRET, first_byte_seconds=2.5, tokens_per_second=4)
    engine = Engine(create_app(settings, sleep=sleep))

    whole = ask(route, REQUESTS[route], stream=False, engine=engine)
    assert waits == [2.5]

    waits.clear()
    ask(route, REQUESTS[route], stream=True, engine=engine)
    # The wait before the first byte, then one wait of a quarter of a second for each token.
    assert waits == [2.5] + [0.25] * whole.output_tokens


def test_the_hang_mode() -> None:
    app = create_app(Settings(secret=SECRET, hang=True))

    async def scenario() -> None:
        hung_up = asyncio.Event()
        sent: list[Message] = []
        body = [json.dumps(REQUESTS[CHAT]).encode()]

        async def receive() -> Message:
            if body:
                return {"type": "http.request", "body": body.pop(), "more_body": False}
            await hung_up.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": CHAT,
            "raw_path": CHAT.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"authorization", f"Bearer {SECRET}".encode()), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
        inference = asyncio.ensure_future(app(scope, receive, send))

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as other:
            for _ in range(3):
                assert (await other.get("/health")).status_code == 200
                # Long enough for an answer that needs no model; nothing arrives, not even a status line.
                done, _ = await asyncio.wait({inference}, timeout=0.05)
                assert not done
                assert sent == []
            # The secret still guards a hung engine.
            assert (await other.post(CHAT, json=REQUESTS[CHAT])).status_code == 401

        # The wait lasts as long as the client does: it ends when the client hangs up.
        hung_up.set()
        await asyncio.wait_for(inference, timeout=5)

    # Bounded, so that a mock that hangs where it should answer fails this test and does not stall it.
    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
