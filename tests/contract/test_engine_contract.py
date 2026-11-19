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
where its format puts the text, the call, and the usage. Streams are read with the gateway's own
reader of server-sent events.

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
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from rhi_common.engine_metrics import Quantity, metric_names
from rhi_gateway.sse import SseReader

REPO = Path(__file__).resolve().parents[2]

CHAT, RESPONSES, MESSAGES, COUNT = "/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"
FORMATS = (CHAT, RESPONSES, MESSAGES)
INFERENCE_ROUTES = (*FORMATS, COUNT)

MODEL = "coursework"
PROMPT = "Read the file notes.txt and say what it holds."
REQUESTS: dict[str, dict[str, Any]] = {
    CHAT: {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}]},
    RESPONSES: {"model": MODEL, "input": PROMPT},
    MESSAGES: {"model": MODEL, "max_tokens": 1024, "messages": [{"role": "user", "content": PROMPT}]},
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

# An address no engine is scraped from (a documentation range).
ELSEWHERE = "203.0.113.7"


# --- Reading an answer ---------------------------------------------------------------------------


class Answer(NamedTuple):
    """An answer, whatever its format and whether it was streamed. A count the answer lacks is `None`."""

    text: str
    calls: list[tuple[str, str]]  # each function's name, and its arguments as JSON text
    input_tokens: object
    output_tokens: object


class Calls:
    """The function calls of an answer, each put together from the pieces that carry its key."""

    def __init__(self) -> None:
        self._calls: dict[object, list[str]] = {}

    def add(self, key: object, name: object = None, arguments: object = None) -> None:
        call = self._calls.setdefault(key, ["", ""])
        call[0] += name if isinstance(name, str) else ""
        call[1] += arguments if isinstance(arguments, str) else ""

    def all(self) -> list[tuple[str, str]]:
        return [(name, arguments) for name, arguments in self._calls.values()]


def counts(usage: object, input_key: str, output_key: str) -> tuple[object, object]:
    return (usage.get(input_key), usage.get(output_key)) if isinstance(usage, Mapping) else (None, None)


def read_chat(answer: dict[str, Any]) -> Answer:
    message, calls = answer["choices"][0]["message"], Calls()
    for index, call in enumerate(message.get("tool_calls") or []):
        calls.add(index, call["function"]["name"], call["function"]["arguments"])
    return Answer(
        message.get("content") or "", calls.all(), *counts(answer.get("usage"), "prompt_tokens", "completion_tokens")
    )


def read_chat_stream(data: list[str]) -> Answer:
    assert data[-1:] == ["[DONE]"], "the stream does not end with [DONE]"
    text, calls, usage = "", Calls(), None
    for chunk in map(json.loads, data[:-1]):
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                calls.add(call.get("index"), function.get("name"), function.get("arguments"))
    return Answer(text, calls.all(), *counts(usage, "prompt_tokens", "completion_tokens"))


def read_responses(answer: dict[str, Any]) -> Answer:
    text, calls = "", Calls()
    for index, item in enumerate(answer["output"]):
        if item["type"] == "message":
            text += "".join(part["text"] for part in item["content"] if part["type"] == "output_text")
        elif item["type"] == "function_call":
            calls.add(index, item["name"], item["arguments"])
    return Answer(text, calls.all(), *counts(answer.get("usage"), "input_tokens", "output_tokens"))


def read_responses_stream(data: list[str]) -> Answer:
    events = [json.loads(one) for one in data]
    assert events and events[-1]["type"] == "response.completed", "the stream does not end with response.completed"
    text, calls = "", Calls()
    for event in events:
        if event["type"] == "response.output_text.delta":
            text += event["delta"]
        elif event["type"] == "response.output_item.added" and event["item"]["type"] == "function_call":
            calls.add(event["output_index"], name=event["item"]["name"])
        elif event["type"] == "response.function_call_arguments.delta":
            calls.add(event["output_index"], arguments=event["delta"])
    return Answer(text, calls.all(), *counts(events[-1]["response"].get("usage"), "input_tokens", "output_tokens"))


def read_messages(answer: dict[str, Any]) -> Answer:
    text, calls = "", Calls()
    for index, block in enumerate(answer["content"]):
        if block["type"] == "text":
            text += block["text"]
        elif block["type"] == "tool_use":
            calls.add(index, block["name"], json.dumps(block["input"]))
    return Answer(text, calls.all(), *counts(answer.get("usage"), "input_tokens", "output_tokens"))


def read_messages_stream(data: list[str]) -> Answer:
    events = [json.loads(one) for one in data]
    assert events and events[-1]["type"] == "message_stop", "the stream does not end with message_stop"
    text, calls, usage = "", Calls(), {}
    for event in events:
        if event["type"] == "message_start":
            usage.update(event["message"].get("usage") or {})
        elif event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            calls.add(event["index"], name=event["content_block"]["name"])
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "text_delta":
            text += event["delta"]["text"]
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
            calls.add(event["index"], arguments=event["delta"]["partial_json"])
        elif event["type"] == "message_delta":
            # The input count travels in the first event and the output count in this one.
            usage.update(event.get("usage") or {})
    return Answer(text, calls.all(), *counts(usage, "input_tokens", "output_tokens"))


READERS: dict[str, tuple[Callable[[dict[str, Any]], Answer], Callable[[list[str]], Answer]]] = {
    CHAT: (read_chat, read_chat_stream),
    RESPONSES: (read_responses, read_responses_stream),
    MESSAGES: (read_messages, read_messages_stream),
}


async def events_of(response: httpx.Response) -> AsyncIterator[str]:
    """The data of each event of a stream, as it arrives. Comments, which keep a stream alive, are left out."""
    reader = SseReader()
    async for chunk in response.aiter_bytes():
        for event in reader.feed(chunk):
            if not event.comment:
                yield event.data


async def ask(client: httpx.AsyncClient, engine: Any, route: str, *, streamed: bool, calling: bool = False) -> Answer:
    """Send the route its request, with the secret, and read the answer: it is served, and it carries its usage."""
    what = f"{route}, {'streamed' if streamed else 'whole'}{', calling a function' if calling else ''}"
    body = REQUESTS[route] | (STREAMED[route] if streamed else {}) | (CALLING[route] if calling else {})
    whole, stream = READERS[route]
    async with client.stream("POST", route, json=body, headers=engine.authorized) as response:
        assert response.status_code == 200, f"{what}: answered {response.status_code}"
        if streamed:
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("text/event-stream"), f"{what}: not a stream of server-sent events"
            answer = stream([data async for data in events_of(response)])
        else:
            answer = whole(json.loads(await response.aread()))
    for count in (answer.input_tokens, answer.output_tokens):
        assert isinstance(count, int) and count > 0, f"{what}: the answer does not carry its usage"
    return answer


# --- The contract --------------------------------------------------------------------------------


def test_inference(engine: Any) -> None:
    async def scenario() -> None:
        async with engine.client() as client:
            for route in FORMATS:
                for streamed in (False, True):
                    said = await ask(client, engine, route, streamed=streamed)
                    assert said.text, (route, streamed)
                    assert said.calls == [], (route, streamed)

                    called = await ask(client, engine, route, streamed=streamed, calling=True)
                    assert [name for name, _ in called.calls] == [FUNCTION], (route, streamed)
                    assert isinstance(json.loads(called.calls[0][1]), dict), (route, streamed)

            counted = await client.post(COUNT, json=REQUESTS[COUNT], headers=engine.authorized)
            assert counted.status_code == 200
            input_tokens = counted.json().get("input_tokens")
            assert isinstance(input_tokens, int) and input_tokens > 0

    asyncio.run(scenario())


def test_health(engine: Any) -> None:
    async def scenario() -> None:
        async with engine.client() as client:
            # Without the secret: the load balancer's health check carries none.
            assert (await client.get("/health")).status_code == 200
            # 200 says the model can serve, so the next request is served.
            assert (await ask(client, engine, CHAT, streamed=False)).text

    asyncio.run(scenario())


def test_metrics(engine: Any) -> None:
    names = metric_names(engine.name)

    async def scenario() -> None:
        async with engine.client() as client:
            await ask(client, engine, CHAT, streamed=True)
            response = await client.get("/metrics")
        assert response.status_code == 200
        families = {family.name: family.type for family in text_string_to_metric_families(response.text)}
        for quantity in Quantity:
            name = names[quantity]
            assert name in families, f"{quantity.value}: the engine serves no metric named {name}"
            assert families[name] == quantity.kind.value, f"{quantity.value}: {name} is a {families[name]}"

        # On localhost only. Checked where the suite chooses the address its request comes from.
        if engine.in_process:
            async with engine.client(peer=ELSEWHERE) as client:
                assert (await client.get("/metrics")).status_code == 404, "the metrics are served to another host"

    asyncio.run(scenario())


def test_bearer_secret(engine: Any) -> None:
    wrong = {"Authorization": f"Bearer {secrets.token_urlsafe(32)}"}

    async def scenario() -> None:
        async with engine.client() as client:
            for route in INFERENCE_ROUTES:
                without = await client.post(route, json=REQUESTS[route])
                assert without.status_code == 401, f"{route} answered {without.status_code} with no secret"
                mistaken = await client.post(route, json=REQUESTS[route], headers=wrong)
                assert mistaken.status_code == 401, f"{route} answered {mistaken.status_code} to a wrong secret"
                right = await client.post(route, json=REQUESTS[route], headers=engine.authorized)
                assert right.status_code == 200, f"{route} answered {right.status_code} to the pool's secret"

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
    answer = read_chat_stream(asyncio.run(scenario()))
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


def test_the_suite_catches_a_broken_engine(mock_engine: Any) -> None:
    def any_secret(request: httpx.Request) -> None:
        request.headers["Authorization"] = mock_engine.authorized["Authorization"]

    # The mock as it is passes, behind the transport that carries the faults.
    for check in (test_bearer_secret, test_inference, test_metrics):
        check(altered(mock_engine))

    # Its secret check turned off: every request is answered, whatever it carries.
    with pytest.raises(AssertionError, match="secret"):
        test_bearer_secret(altered(mock_engine, request=any_secret))

    # Its usage removed: from the whole answers or from the streams of one format, each in turn.
    for route in FORMATS:
        for streamed in (False, True):
            with pytest.raises(AssertionError, match="usage"):
                test_inference(altered(mock_engine, answer=without_usage(route, streamed=streamed)))

    # One metric of its row missing: each in turn, and the failure names the quantity left without one.
    for quantity, name in metric_names(mock_engine.name).items():
        with pytest.raises(AssertionError, match=f"^{quantity.value}: "):
            test_metrics(altered(mock_engine, answer=without_metric(name)))


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
