"""The engine contract, as a suite every engine adapter must pass (system design, section 4.1).

One test for each row of the contract that an engine answers by itself:

| Row | Test |
| --- | --- |
| Inference | `test_inference`: the three generation routes, whole and streamed, each with a call of a declared function, and the token-counting route |
| Health | `test_health` |
| Metrics | `test_metrics`: every name of the engine's row of `rhi_common.engine_metrics` |
| The bearer secret of the reachability row | `test_bearer_secret`, on the four inference routes |
| Shutdown | `test_shutdown`, collected only where the suite controls the engine |

The rest of the reachability row is the internal load balancer's allow-list, and the identity row
is the lifecycle agent's: neither is a property an engine can be asked for, and both are tested
elsewhere. So are the engine settings the contract fixes for vLLM.

`conftest.py` says which engine the suite runs against: the mock in this process, or the engine at
the address the environment gives. The suite asks only what any engine answers. A request forces
the call of its function, so a model has no choice to make, and nothing is read from an answer but
where its format puts the text, the call, the usage, and how it ended. Streams are read with the
gateway's own reader of server-sent events. Every generation request asks for the effort `low` and
2,048 tokens, which leave a model that reasons first room to answer (`ROOM`). Every check fails
with a message whose first line is its reason, `<request> <check> <finish>` (`Exchange`): the one
line of a failure that leaves the node (system design, section 4.1).

The last tests are about the suite itself: that it fails on a broken engine, that it runs against
an engine at an address, and what the environment asks for.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple, cast

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from rhi_common.engine_metrics import Quantity, metric_names
from rhi_gateway.sse import SseReader

REPO = Path(__file__).resolve().parents[2]

CHAT, RESPONSES, MESSAGES, COUNT = "/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"
FORMATS = (CHAT, RESPONSES, MESSAGES)
INFERENCE_ROUTES = (*FORMATS, COUNT)
# Each route's word in a reason.
WORDS = {CHAT: "chat", RESPONSES: "responses", MESSAGES: "messages", COUNT: "count"}

MODEL = "coursework"
PROMPT = "Read the file notes.txt and say what it holds."
# What every generation request asks for, as every harness does: an effort, and a budget the
# reasoning fits in. The coursework model reasons first, at `xhigh` unless asked, and a smaller
# budget can end inside the reasoning, with no text and no call. `low` is the lowest effort its
# template accepts; 2,048 tokens take about 36 seconds at one stream's rate, within the client's
# read time (system design, section 4.1). Each in its format's own field.
EFFORT, BUDGET = "low", 2048
ROOM: dict[str, dict[str, Any]] = {
    CHAT: {"reasoning_effort": EFFORT, "max_completion_tokens": BUDGET},
    RESPONSES: {"reasoning": {"effort": EFFORT}, "max_output_tokens": BUDGET},
    MESSAGES: {"output_config": {"effort": EFFORT}, "max_tokens": BUDGET},
}
REQUESTS: dict[str, dict[str, Any]] = {
    CHAT: {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}], **ROOM[CHAT]},
    RESPONSES: {"model": MODEL, "input": PROMPT, **ROOM[RESPONSES]},
    MESSAGES: {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}], **ROOM[MESSAGES]},
    COUNT: {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}]},
}
# What asks each format for a stream that carries its usage.
STREAMED: dict[str, dict[str, Any]] = {
    CHAT: {"stream": True, "stream_options": {"include_usage": True}},
    RESPONSES: {"stream": True},
    MESSAGES: {"stream": True},
}
# How each format declares a function, and requires the answer to call it.
FUNCTION = "read_file"
SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
CALLING: dict[str, dict[str, Any]] = {
    CHAT: {
        "tools": [{"type": "function", "function": {"name": FUNCTION, "parameters": SCHEMA}}],
        "tool_choice": {"type": "function", "function": {"name": FUNCTION}},
    },
    RESPONSES: {
        "tools": [{"type": "function", "name": FUNCTION, "parameters": SCHEMA}],
        "tool_choice": {"type": "function", "name": FUNCTION},
    },
    MESSAGES: {
        "tools": [{"name": FUNCTION, "input_schema": SCHEMA}],
        "tool_choice": {"type": "tool", "name": FUNCTION},
    },
}
# How each format says its answer ended, in the words it uses; any other value is `other`.
FINISHES: dict[str, frozenset[str]] = {
    CHAT: frozenset({"stop", "length", "tool_calls"}),
    RESPONSES: frozenset({"completed", "incomplete"}),
    MESSAGES: frozenset({"end_turn", "max_tokens", "tool_use", "stop_sequence"}),
}
# The events that end a Responses stream, each carrying the response and its status; of them, those
# that end it with an answer.
RESPONSES_LAST = frozenset({"response.completed", "response.incomplete", "response.failed"})
RESPONSES_ENDS = frozenset({"response.completed", "response.incomplete"})

# An address no engine is scraped from (a documentation range).
ELSEWHERE = "203.0.113.7"


# --- Saying why a check failed -------------------------------------------------------------------


class Exchange:
    """One request of a test, and how far its answer was read.

    A failed check raises an assertion whose message begins with the line `<request> <check>
    <finish>`: the request's name, the check's, and how the answer said it ended, all words of the
    suite's own (system design, section 4.1), so that the line holds nothing of the request or the
    answer. The plugin of `services/engine/contract/` passes that line on, and nothing else.
    """

    def __init__(self, request: str, route: str | None = None) -> None:
        self.request, self.route, self.finish = request, route, "none"

    def ended(self, value: object) -> None:
        """Note how the answer said it ended. A value of `None` says nothing."""
        if value is not None and self.route in FINISHES:
            self.finish = value if isinstance(value, str) and value in FINISHES[self.route] else "other"

    def reason(self, check: str) -> str:
        return f"{self.request} {check} {self.finish}"

    def check(self, holds: bool, check: str, detail: str = "") -> None:
        if not holds:
            raise AssertionError(self.reason(check) + (f"\n{detail}" if detail else ""))


def status_word(response: httpx.Response) -> str:
    return f"status-{response.status_code:03d}"


@contextmanager
def exchanging(request: str, route: str | None = None) -> Generator[Exchange]:
    """The exchange of one request, whose every failure begins with its reason: one the suite
    raised as it is, a timeout, a transport error, an answer not of its format's shape, or else
    `unexplained`."""
    said = Exchange(request, route)
    try:
        yield said
    except AssertionError:
        raise
    except httpx.TimeoutException as error:
        raise AssertionError(said.reason("timeout")) from error
    except httpx.TransportError as error:
        raise AssertionError(said.reason("no-connection")) from error
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, httpx.DecodingError) as error:
        raise AssertionError(said.reason("unreadable")) from error
    except Exception as error:
        raise AssertionError(said.reason("unexplained")) from error


# --- Reading an answer ---------------------------------------------------------------------------


class Answer(NamedTuple):
    """An answer, whatever its format and whether it was streamed. A count the answer lacks is `None`."""

    text: str
    calls: list[tuple[str, str]]  # each function's name, and its arguments as JSON text
    input_tokens: object
    output_tokens: object


def text_of(value: object) -> str:
    """The text a format's field holds: a string, or nothing. Any other value is not of the format's shape."""
    if value is None or isinstance(value, str):
        return value or ""
    raise TypeError("the answer's text is not a string")


class Calls:
    """The function calls of an answer, each put together from the pieces that carry its key."""

    def __init__(self) -> None:
        self._calls: dict[object, list[str]] = {}

    def add(self, key: object, name: object = None, arguments: object = None) -> None:
        call = self._calls.setdefault(key, ["", ""])
        call[0] += text_of(name)
        call[1] += text_of(arguments)

    def all(self) -> list[tuple[str, str]]:
        return [(name, arguments) for name, arguments in self._calls.values()]


def counts(usage: object, input_key: str, output_key: str) -> tuple[object, object]:
    return (usage.get(input_key), usage.get(output_key)) if isinstance(usage, Mapping) else (None, None)


def read_chat(answer: dict[str, Any], said: Exchange) -> Answer:
    choice, calls = answer["choices"][0], Calls()
    said.ended(choice.get("finish_reason"))
    message = choice["message"]
    for index, call in enumerate(message.get("tool_calls") or []):
        calls.add(index, call["function"]["name"], call["function"]["arguments"])
    return Answer(
        text_of(message.get("content")),
        calls.all(),
        *counts(answer.get("usage"), "prompt_tokens", "completion_tokens"),
    )


class Stream:
    """A stream's reader, fed each event's data as it arrives, so that a stream cut short still
    says how far its answer was read: how it said it ended, among the rest."""

    def __init__(self, said: Exchange) -> None:
        self.said, self.text, self.calls = said, "", Calls()

    def feed(self, data: str) -> None:
        raise NotImplementedError

    def answer(self) -> Answer:
        raise NotImplementedError


class ChatStream(Stream):
    def __init__(self, said: Exchange) -> None:
        super().__init__(said)
        self.done, self.usage = False, cast(object, None)

    def feed(self, data: str) -> None:
        self.done = data == "[DONE]"
        if self.done:
            return
        chunk = json.loads(data)
        self.usage = chunk.get("usage") or self.usage
        for choice in chunk.get("choices") or []:
            self.said.ended(choice.get("finish_reason"))
            delta = choice.get("delta") or {}
            self.text += text_of(delta.get("content"))
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                self.calls.add(call.get("index"), function.get("name"), function.get("arguments"))

    def answer(self) -> Answer:
        self.said.check(self.done, "no-end-event", "the stream does not end with [DONE]")
        return Answer(self.text, self.calls.all(), *counts(self.usage, "prompt_tokens", "completion_tokens"))


def read_responses(answer: dict[str, Any], said: Exchange) -> Answer:
    said.ended(answer.get("status"))
    text, calls = "", Calls()
    for index, item in enumerate(answer["output"]):
        if item["type"] == "message":
            text += "".join(text_of(part["text"]) for part in item["content"] if part["type"] == "output_text")
        elif item["type"] == "function_call":
            calls.add(index, item["name"], item["arguments"])
    return Answer(text, calls.all(), *counts(answer.get("usage"), "input_tokens", "output_tokens"))


class ResponsesStream(Stream):
    def __init__(self, said: Exchange) -> None:
        super().__init__(said)
        self.last: dict[str, Any] = {}

    def feed(self, data: str) -> None:
        event = json.loads(data)
        self.last = event
        if event["type"] in RESPONSES_LAST:
            self.said.ended(event["response"].get("status"))
        elif event["type"] == "response.output_text.delta":
            self.text += text_of(event["delta"])
        elif event["type"] == "response.output_item.added" and event["item"]["type"] == "function_call":
            self.calls.add(event["output_index"], name=event["item"]["name"])
        elif event["type"] == "response.function_call_arguments.delta":
            self.calls.add(event["output_index"], arguments=event["delta"])

    def answer(self) -> Answer:
        # A stream that says the response failed has ended, but not with an answer.
        ended = self.last.get("type") in RESPONSES_ENDS
        self.said.check(ended, "no-end-event", "the stream does not end with a response that completed")
        usage = self.last["response"].get("usage")
        return Answer(self.text, self.calls.all(), *counts(usage, "input_tokens", "output_tokens"))


def read_messages(answer: dict[str, Any], said: Exchange) -> Answer:
    said.ended(answer.get("stop_reason"))
    text, calls = "", Calls()
    for index, block in enumerate(answer["content"]):
        if block["type"] == "text":
            text += text_of(block["text"])
        elif block["type"] == "tool_use":
            calls.add(index, block["name"], json.dumps(block["input"]))
    return Answer(text, calls.all(), *counts(answer.get("usage"), "input_tokens", "output_tokens"))


class MessagesStream(Stream):
    def __init__(self, said: Exchange) -> None:
        super().__init__(said)
        self.usage: dict[str, Any] = {}
        self.last: dict[str, Any] = {}

    def feed(self, data: str) -> None:
        event = json.loads(data)
        self.last = event
        if event["type"] == "message_start":
            self.usage.update(event["message"].get("usage") or {})
        elif event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            self.calls.add(event["index"], name=event["content_block"]["name"])
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "text_delta":
            self.text += text_of(event["delta"]["text"])
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
            self.calls.add(event["index"], arguments=event["delta"]["partial_json"])
        elif event["type"] == "message_delta":
            self.said.ended((event.get("delta") or {}).get("stop_reason"))
            # The input count travels in the first event and the output count in this one.
            self.usage.update(event.get("usage") or {})

    def answer(self) -> Answer:
        ended = self.last.get("type") == "message_stop"
        self.said.check(ended, "no-end-event", "the stream does not end with message_stop")
        return Answer(self.text, self.calls.all(), *counts(self.usage, "input_tokens", "output_tokens"))


READERS: dict[str, tuple[Callable[[dict[str, Any], Exchange], Answer], type[Stream]]] = {
    CHAT: (read_chat, ChatStream),
    RESPONSES: (read_responses, ResponsesStream),
    MESSAGES: (read_messages, MessagesStream),
}


async def events_of(response: httpx.Response) -> AsyncIterator[str]:
    """The data of each event of a stream, as it arrives. Comments, which keep a stream alive, are left out."""
    reader = SseReader()
    async for chunk in response.aiter_bytes():
        for event in reader.feed(chunk):
            if not event.comment:
                yield event.data


async def ask(
    client: httpx.AsyncClient, engine: Any, route: str, *, streamed: bool, calling: bool = False
) -> tuple[Exchange, Answer]:
    """Send the route its request, with the secret, and read the answer: it is served, and it carries its usage."""
    name = f"{WORDS[route]}-{'streamed' if streamed else 'whole'}{'-calling' if calling else ''}"
    body = REQUESTS[route] | (STREAMED[route] if streamed else {}) | (CALLING[route] if calling else {})
    whole, stream = READERS[route]
    with exchanging(name, route) as said:
        async with client.stream("POST", route, json=body, headers=engine.authorized) as response:
            said.check(response.status_code == 200, status_word(response))
            if streamed:
                content_type = response.headers.get("content-type", "")
                said.check(content_type.startswith("text/event-stream"), "not-a-stream")
                reader = stream(said)
                async for data in events_of(response):
                    reader.feed(data)
                answer = reader.answer()
            else:
                answer = whole(json.loads(await response.aread()), said)
        carried = all(isinstance(count, int) and count > 0 for count in (answer.input_tokens, answer.output_tokens))
        said.check(carried, "no-usage", "the answer does not carry its usage")
    return said, answer


def is_an_object(arguments: str) -> bool:
    try:
        return isinstance(json.loads(arguments), dict)
    except ValueError:
        return False


# --- The contract --------------------------------------------------------------------------------


def test_inference(engine: Any) -> None:
    async def scenario() -> None:
        async with engine.client() as client:
            for route in FORMATS:
                for streamed in (False, True):
                    said, answer = await ask(client, engine, route, streamed=streamed)
                    said.check(bool(answer.text), "no-text")
                    said.check(answer.calls == [], "a-call")

                    said, answer = await ask(client, engine, route, streamed=streamed, calling=True)
                    said.check([name for name, _ in answer.calls] == [FUNCTION], "no-call")
                    said.check(is_an_object(answer.calls[0][1]), "arguments-not-an-object")

            with exchanging("count") as said:
                counted = await client.post(COUNT, json=REQUESTS[COUNT], headers=engine.authorized)
                said.check(counted.status_code == 200, status_word(counted))
                body = counted.json()
                said.check(isinstance(body, dict), "unreadable")
                input_tokens = body.get("input_tokens")
                said.check(isinstance(input_tokens, int) and input_tokens > 0, "no-input-tokens")

    asyncio.run(scenario())


def test_health(engine: Any) -> None:
    async def scenario() -> None:
        async with engine.client() as client:
            # Without the secret: the load balancer's health check carries none.
            with exchanging("health") as said:
                health = await client.get("/health")
                said.check(health.status_code == 200, status_word(health))
            # 200 says the model can serve, so the next request is served.
            said, answer = await ask(client, engine, CHAT, streamed=False)
            said.check(bool(answer.text), "no-text")

    asyncio.run(scenario())


def test_metrics(engine: Any) -> None:
    names = metric_names(engine.name)

    async def scenario() -> None:
        async with engine.client() as client:
            await ask(client, engine, CHAT, streamed=True)
            with exchanging("metrics") as said:
                response = await client.get("/metrics")
                said.check(response.status_code == 200, status_word(response))
                families = {family.name: family.type for family in text_string_to_metric_families(response.text)}
        for quantity in Quantity:
            name = names[quantity]
            said.check(name in families, f"metric-missing-{quantity.value}", f"the engine serves no metric {name}")
            said.check(
                families[name] == quantity.kind.value, f"metric-kind-{quantity.value}", f"{name} is a {families[name]}"
            )

        # On localhost only. Checked where the suite chooses the address its request comes from.
        if engine.in_process:
            async with engine.client(peer=ELSEWHERE) as client:
                with exchanging("metrics") as said:
                    elsewhere = await client.get("/metrics")
                    said.check(
                        elsewhere.status_code == 404, "metrics-elsewhere", "the metrics are served to another host"
                    )

    asyncio.run(scenario())


def test_bearer_secret(engine: Any) -> None:
    wrong = {"Authorization": f"Bearer {secrets.token_urlsafe(32)}"}
    # Each way of asking, and what it must be answered.
    ways = (("without", {}, 401), ("wrong", wrong, 401), ("with", engine.authorized, 200))

    async def scenario() -> None:
        async with engine.client() as client:
            for route in INFERENCE_ROUTES:
                for way, headers, expected in ways:
                    with exchanging(f"{WORDS[route]}-{way}-secret") as said:
                        answered = await client.post(route, json=REQUESTS[route], headers=headers)
                        said.check(answered.status_code == expected, status_word(answered))

    asyncio.run(scenario())


def test_shutdown(engine_process: Any) -> None:
    engine, child = engine_process.engine, engine_process.child

    async def scenario() -> list[str]:
        body = REQUESTS[CHAT] | STREAMED[CHAT]
        async with (
            engine.client() as client,
            client.stream("POST", CHAT, json=body, headers=engine.authorized) as response,
        ):
            assert response.status_code == 200
            events = events_of(response)
            # The stream has started and has about a second to run: the signal finds it in flight.
            first = await anext(events)
            child.send_signal(signal.SIGTERM)
            return [first] + [data async for data in events]

    # The request runs to its end: the terminal event, the text and the usage, as with no signal.
    reader = ChatStream(Exchange("chat-streamed", CHAT))
    for data in asyncio.run(scenario()):
        reader.feed(data)
    answer = reader.answer()
    assert answer.text
    assert isinstance(answer.output_tokens, int) and answer.output_tokens > 0
    # Then the engine stops.
    child.wait(timeout=30)


# --- The suite itself ----------------------------------------------------------------------------


class Altered(httpx.AsyncBaseTransport):
    """An engine's transport, with each request and each answer passed through a function on its way."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        request: Callable[[httpx.Request], None] | None,
        answer: Callable[[httpx.Request, str], str] | None,
    ) -> None:
        self._inner, self._request, self._answer = inner, request, answer

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._request is not None:
            self._request(request)
        response = await self._inner.handle_async_request(request)
        if self._answer is None:
            # Nothing to change: the answer as it comes, a stream still in flight.
            return response
        content = (await response.aread()).decode()
        await response.aclose()
        if self._answer is not None:
            content = self._answer(request, content)
        headers = [(name, value) for name, value in response.headers.multi_items() if name.lower() != "content-length"]
        return httpx.Response(response.status_code, headers=headers, content=content.encode())


def altered(
    engine: Any,
    *,
    request: Callable[[httpx.Request], None] | None = None,
    answer: Callable[[httpx.Request, str], str] | None = None,
) -> Any:
    """The engine, with a fault between it and the suite; with no fault, the engine as it is."""
    return replace(engine, connect=lambda peer: Altered(engine.connect(peer), request, answer))


def without_usage(route: str, *, streamed: bool) -> Callable[[httpx.Request, str], str]:
    """The answers of one route, the whole ones or the streamed ones, with every `usage` member taken out."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip(member) for key, member in value.items() if key != "usage"}
        return [strip(member) for member in value] if isinstance(value, list) else value

    def answer(request: httpx.Request, content: str) -> str:
        if request.url.path != route or (json.loads(request.content).get("stream") is True) != streamed:
            return content
        lines = []
        # A whole answer is one line of JSON; an event of a stream holds one after `data: `.
        for line in content.split("\n"):
            prefix = "data: " if line.startswith("data: ") else ""
            try:
                lines.append(prefix + json.dumps(strip(json.loads(line.removeprefix(prefix)))))
            except ValueError:
                lines.append(line)
        return "\n".join(lines)

    return answer


def without_metric(name: str) -> Callable[[httpx.Request, str], str]:
    """The scrape with one metric family taken out: its `# HELP` and `# TYPE` lines and its samples."""
    family = {name + suffix for suffix in ("", "_total", "_created", "_bucket", "_sum", "_count")}

    def answer(request: httpx.Request, content: str) -> str:
        if request.url.path != "/metrics":
            return content
        kept = []
        for line in content.splitlines(keepends=True):
            subject = line.split()[2] if line.startswith("#") else re.split(r"[{ ]", line, maxsplit=1)[0]
            if subject not in family:
                kept.append(line)
        return "".join(kept)

    return answer


def reasoning_alone(*, streamed: bool) -> Callable[[httpx.Request, str], str]:
    """The Messages answers, the whole ones or the streamed ones, cut inside the reasoning: one
    thinking block and no other, and `stop_reason` `max_tokens`."""

    def answer(request: httpx.Request, content: str) -> str:
        if request.url.path != MESSAGES or (json.loads(request.content).get("stream") is True) != streamed:
            return content
        if not streamed:
            message = json.loads(content)
            message["content"] = [block for block in message["content"] if block["type"] == "thinking"]
            return json.dumps(message | {"stop_reason": "max_tokens"})
        frames, thinking = [], set()
        for frame in content.split("\n\n"):
            lines = frame.split("\n")
            data = [line for line in lines if line.startswith("data: ")]
            if not data:
                frames.append(frame)
                continue
            event = json.loads(data[0].removeprefix("data: "))
            if event["type"] == "content_block_start" and event["content_block"]["type"] == "thinking":
                thinking.add(event["index"])
            if event["type"].startswith("content_block_") and event["index"] not in thinking:
                continue
            if event["type"] == "message_delta":
                event["delta"]["stop_reason"] = "max_tokens"
            frames.append("\n".join(f"data: {json.dumps(event)}" if line in data else line for line in lines))
        return "\n\n".join(frames)

    return answer


def failed_responses_stream(request: httpx.Request, content: str) -> str:
    """The Responses streams, ended by the event that says the response failed, and not completed."""
    if request.url.path != RESPONSES or json.loads(request.content).get("stream") is not True:
        return content
    lines = []
    for line in content.split("\n"):
        if line == "event: response.completed":
            line = "event: response.failed"
        elif line.startswith("data: ") and '"response.completed"' in line:
            event = json.loads(line.removeprefix("data: "))
            event["type"], event["response"]["status"] = "response.failed", "failed"
            line = f"data: {json.dumps(event)}"
        lines.append(line)
    return "\n".join(lines)


def chat_text_not_a_string(request: httpx.Request, content: str) -> str:
    """The whole Chat answers, with an object where their text belongs."""
    if request.url.path != CHAT or json.loads(request.content).get("stream") is True:
        return content
    answer = json.loads(content)
    answer["choices"][0]["message"]["content"] = {"text": "not a string"}
    return json.dumps(answer)


class StallsAfterItsFinish(httpx.AsyncByteStream):
    """A Chat stream that says it ended at its budget, then stalls until the client gives up."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}\n\n'
        raise httpx.ReadTimeout("no more of the stream")


def stalled(engine: Any) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=StallsAfterItsFinish())

    return replace(engine, connect=lambda peer: httpx.MockTransport(answer))


def reason_of(check: Callable[[Any], None], engine: Any) -> str:
    """The first line of the failure a test of the contract ends in, against the engine."""
    with pytest.raises(AssertionError) as failed:
        check(engine)
    return str(failed.value).splitlines()[0]


def test_the_suite_catches_a_broken_engine(mock_engine: Any) -> None:
    def any_secret(request: httpx.Request) -> None:
        request.headers["Authorization"] = mock_engine.authorized["Authorization"]

    # The mock as it is passes, behind the transport that carries the faults.
    for check in (test_bearer_secret, test_inference, test_metrics):
        check(altered(mock_engine))

    # Its secret check turned off: every request is answered, whatever it carries.
    assert (
        reason_of(test_bearer_secret, altered(mock_engine, request=any_secret)) == "chat-without-secret status-200 none"
    )

    # Its usage removed: from the whole answers or from the streams of one format, each in turn.
    # The request without a function is the first of each, and the mock's answers end as they should.
    ends = {CHAT: "stop", RESPONSES: "completed", MESSAGES: "end_turn"}
    for route in FORMATS:
        for streamed in (False, True):
            broken = altered(mock_engine, answer=without_usage(route, streamed=streamed))
            way = "streamed" if streamed else "whole"
            assert reason_of(test_inference, broken) == f"{WORDS[route]}-{way} no-usage {ends[route]}"

    # One metric of its row missing: each in turn, and the failure names the quantity left without one.
    for quantity, name in metric_names(mock_engine.name).items():
        broken = altered(mock_engine, answer=without_metric(name))
        assert reason_of(test_metrics, broken) == f"metrics metric-missing-{quantity.value} none"

    # Its Messages answers cut inside the reasoning, as a budget too small for a model that reasons
    # first cuts them: the whole ones and the streamed ones, then the streamed ones alone.
    cut = [reasoning_alone(streamed=False), reasoning_alone(streamed=True)]

    def both(request: httpx.Request, content: str) -> str:
        return cut[1](request, cut[0](request, content))

    assert reason_of(test_inference, altered(mock_engine, answer=both)) == "messages-whole no-text max_tokens"
    assert reason_of(test_inference, altered(mock_engine, answer=cut[1])) == "messages-streamed no-text max_tokens"

    # A Responses stream that says its response failed, though it carried text and usage.
    broken = altered(mock_engine, answer=failed_responses_stream)
    assert reason_of(test_inference, broken) == "responses-streamed no-end-event other"

    # An answer not of its format's shape, read as far as it says how it ended.
    broken = altered(mock_engine, answer=chat_text_not_a_string)
    assert reason_of(test_health, broken) == "chat-whole unreadable stop"

    # A stream that times out after it said how it ended keeps that word.
    assert reason_of(test_metrics, stalled(mock_engine)) == "chat-streamed timeout length"


# Where each generation format asks for its effort and its output budget: the object that holds the
# effort, then its key in that object, and the key of the budget.
ROOM_FIELDS = {
    CHAT: (None, "reasoning_effort", "max_completion_tokens"),
    RESPONSES: ("reasoning", "effort", "max_output_tokens"),
    MESSAGES: ("output_config", "effort", "max_tokens"),
}


def leaves_room(path: str, body: dict[str, Any]) -> bool:
    """Whether a generation request asks for the effort `low` and 2,048 tokens, in its format's fields."""
    parent, effort, budget = ROOM_FIELDS[path]
    holder = body if parent is None else body.get(parent)
    return isinstance(holder, dict) and holder.get(effort) == "low" and body.get(budget) == 2048


def test_every_generation_request_leaves_a_reasoning_model_room_to_answer(
    mock_engine: Any, engine_process: Any
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def record(request: httpx.Request) -> None:
        if request.url.path in FORMATS:
            sent.append((request.url.path, json.loads(request.content)))

    for check in (test_inference, test_health, test_metrics, test_bearer_secret):
        check(altered(mock_engine, request=record))
    before = len(sent)
    test_shutdown(replace(engine_process, engine=altered(engine_process.engine, request=record)))
    # The shutdown's one request, a stream, is among them.
    assert [(path, body.get("stream")) for path, body in sent[before:]] == [(CHAT, True)]

    assert {path for path, _ in sent} == set(FORMATS)
    assert [(path, body) for path, body in sent if not leaves_room(path, body)] == []

    # A request without either field, or with another value, is one the check refuses.
    for path, (parent, effort, budget) in ROOM_FIELDS.items():
        body = REQUESTS[path]
        effortless = {key: value for key, value in body.items() if key != (parent or effort)}
        for lacking in (effortless, body | {budget: 1024}, {k: v for k, v in body.items() if k != budget}):
            assert not leaves_room(path, lacking)


def contract_tests(*arguments: str, environ: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Run pytest on the tests of the contract's rows, in a process of its own: the environment is read there.

    The tests are named one by one, so the run can hold no test about the suite itself, and the
    test that starts this run never starts another from inside it.
    """
    rows = (test_inference, test_health, test_metrics, test_bearer_secret, test_shutdown)
    tests = [f"{Path(__file__).resolve()}::{test.__name__}" for test in rows]
    command = [sys.executable, "-m", "pytest", *tests, "-p", "no:cacheprovider", *arguments]
    return subprocess.run(
        command, cwd=REPO, env=dict(environ), capture_output=True, text=True, timeout=300, check=False
    )


def test_the_suite_runs_against_an_engine_at_an_address(engine_at_an_address: dict[str, str], tmp_path: Path) -> None:
    answered_anywhere = {test.__name__ for test in (test_inference, test_health, test_metrics, test_bearer_secret)}
    in_process = {name: value for name, value in os.environ.items() if name not in engine_at_an_address}

    # At the address the environment gives, every row passes but the shutdown, which is not collected.
    report = tmp_path / "report.xml"
    run = contract_tests(f"--junitxml={report}", environ=in_process | engine_at_an_address)
    assert run.returncode == 0, run.stdout + run.stderr
    cases = list(ET.parse(report).getroot().iter("testcase"))
    assert {case.get("name") for case in cases} == answered_anywhere
    assert not [case.get("name") for case in cases if len(case)], "a test failed or was skipped"

    # In process, the shutdown test is collected with the others.
    collected = contract_tests("--collect-only", "-q", environ=in_process)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    names = {line.rpartition("::")[2] for line in collected.stdout.splitlines() if "::" in line}
    assert names == answered_anywhere | {test_shutdown.__name__}


def test_the_environment_names_the_engine(read_engine_target: Callable[[Mapping[str, str]], Any]) -> None:
    secret = secrets.token_urlsafe(32)
    # No address: the mock in process, whatever else is set.
    assert read_engine_target({}) is None
    assert read_engine_target({"RHI_ENGINE_NAME": "mock", "RHI_POOL_SECRET": secret}) is None

    given = {"RHI_ENGINE_URL": "http://127.0.0.1:8000/", "RHI_ENGINE_NAME": "mock", "RHI_POOL_SECRET": secret}
    target = read_engine_target(given)
    assert (target.url, target.name, target.secret) == ("http://127.0.0.1:8000", "mock", secret)

    # An address with no row, an unknown row or no secret is refused by the variable's name: an
    # engine that was to be checked is never replaced by the mock, or read through another's row.
    for variable, value in (("RHI_ENGINE_NAME", ""), ("RHI_ENGINE_NAME", "another-engine"), ("RHI_POOL_SECRET", "")):
        with pytest.raises(ValueError, match=variable) as refused:
            read_engine_target(given | {variable: value})
        assert secret not in str(refused.value)
    with pytest.raises(ValueError, match="RHI_ENGINE_NAME"):
        read_engine_target({"RHI_ENGINE_URL": given["RHI_ENGINE_URL"]})
