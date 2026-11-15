"""Independent contract probes for the mock's routes and timing controls."""

from __future__ import annotations

import asyncio
import json
from itertools import pairwise
from typing import Any

import httpx
import pytest
from starlette.types import Message

from rhi_mock_engine import app as mock

ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
COUNT = "/v1/messages/count_tokens"
SECRET = "-".join(("examiner", "mock", "value"))
HEADERS = {"Authorization": f"Bearer {SECRET}"}


async def immediate(seconds: float) -> None:
    del seconds


def body_for(route: str, *, tools: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "mock", "max_tokens": 100}
    if route == ROUTES[1]:
        body["input"] = "Give a short answer."
        tool = {"type": "function", "name": "inspect", "parameters": {"type": "object"}}
    else:
        body["messages"] = [{"role": "user", "content": "Give a short answer."}]
        tool = (
            {"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}
            if route == ROUTES[0]
            else {"name": "inspect", "input_schema": {"type": "object"}}
        )
    if tools:
        body["tools"] = [tool]
    return body


def events(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.headers["content-type"].startswith("text/event-stream")
    records = []
    for frame in response.text.split("\n\n"):
        data = [line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")]
        if data and data[0] != "[DONE]":
            records.append(json.loads("\n".join(data)))
    return records


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tools", [False, True])
def test_wire_order_and_usage(route: str, stream: bool, tools: bool) -> None:
    async def scenario() -> None:
        app = mock.create_app(mock.Settings(secret=SECRET), sleep=immediate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as http:
            body = body_for(route, tools=tools)
            count = await http.post(COUNT, json=body, headers=HEADERS)
            assert count.status_code == 200
            expected_input = count.json()["input_tokens"]
            assert isinstance(expected_input, int) and expected_input > 0
            response = await http.post(
                route, json={**body, "stream": stream, "stream_options": {"include_usage": True}}, headers=HEADERS
            )
            assert response.status_code == 200
            if not stream:
                result = response.json()
                if route == ROUTES[0]:
                    message = result["choices"][0]["message"]
                    assert message["reasoning"]
                    assert message["tool_calls"][0]["function"]["name"] == "inspect" if tools else message["content"]
                    usage, key = result["usage"], "prompt_tokens"
                elif route == ROUTES[1]:
                    first, second = result["output"]
                    assert first["type"] == "reasoning" and first["content"][0]["text"]
                    assert second["type"] == ("function_call" if tools else "message")
                    usage, key = result["usage"], "input_tokens"
                else:
                    first, second = result["content"]
                    assert first["type"] == "thinking" and first["thinking"]
                    assert second["type"] == ("tool_use" if tools else "text")
                    usage, key = result["usage"], "input_tokens"
                assert usage[key] == expected_input
                return
            records = events(response)
            if route == ROUTES[0]:
                assert response.text.endswith("data: [DONE]\n\n")
                deltas = [record["choices"][0]["delta"] for record in records if record["choices"]]
                reasoning = [i for i, delta in enumerate(deltas) if delta.get("reasoning")]
                answer = [i for i, delta in enumerate(deltas) if delta.get("tool_calls" if tools else "content")]
                assert records[-1]["choices"] == []
                assert records[-1]["usage"]["prompt_tokens"] == expected_input
            elif route == ROUTES[1]:
                names = [record["type"] for record in records]
                assert names[0] == "response.created" and names[-1] == "response.completed"
                reasoning = [i for i, name in enumerate(names) if name == "response.reasoning_text.delta"]
                answer_name = "response.function_call_arguments.delta" if tools else "response.output_text.delta"
                answer = [i for i, name in enumerate(names) if name == answer_name]
                assert records[-1]["response"]["usage"]["input_tokens"] == expected_input
            else:
                assert records[0]["type"] == "message_start" and records[-1]["type"] == "message_stop"
                reasoning = [i for i, event in enumerate(records) if event.get("delta", {}).get("type") == "thinking_delta"]
                kind = "input_json_delta" if tools else "text_delta"
                answer = [i for i, event in enumerate(records) if event.get("delta", {}).get("type") == kind]
                assert records[0]["message"]["usage"]["input_tokens"] == expected_input
                assert records[-2]["usage"]["output_tokens"] > 0
            assert reasoning and answer and max(reasoning) < min(answer)

    asyncio.run(scenario())


def test_startup_secret_port_and_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(mock.uvicorn, "run", lambda app, **kwargs: served.append((app, kwargs)))
    monkeypatch.setenv("RHI_MOCK_FIRST_BYTE_SECONDS", "0")
    monkeypatch.setenv("RHI_MOCK_TOKENS_PER_SECOND", "50")
    monkeypatch.setenv("RHI_MOCK_HANG", "0")
    for value in (None, ""):
        monkeypatch.delenv("RHI_POOL_SECRET", raising=False)
        if value is not None:
            monkeypatch.setenv("RHI_POOL_SECRET", value)
        with pytest.raises(mock.SettingsError):
            mock.main()
        assert not served
    monkeypatch.setenv("RHI_POOL_SECRET", SECRET)
    monkeypatch.setenv("RHI_MOCK_START_SECONDS", "10")
    mock.main()
    [(app, options)] = served
    assert options["port"] == 8000

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as http:
            assert (await http.get("/health")).status_code == 503
            for route in (*ROUTES, COUNT):
                for headers in ({}, {"Authorization": "Bearer incorrect"}):
                    assert (await http.post(route, json=body_for(route), headers=headers)).status_code == 401
                assert (await http.post(route, json=body_for(route), headers=HEADERS)).status_code == 200
        now = 10.0
        delayed = mock.create_app(mock.Settings.from_env({"RHI_POOL_SECRET": SECRET, "RHI_MOCK_START_SECONDS": "2"}),
                                  clock=lambda: now)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delayed), base_url="http://mock") as http:
            assert (await http.get("/health")).status_code == 503
            now = 12.0
            assert (await http.get("/health")).status_code == 200

    asyncio.run(scenario())


def test_unguarded_routes_and_deterministic_count() -> None:
    async def scenario() -> None:
        app = mock.create_app(mock.Settings(secret=SECRET), sleep=immediate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as http:
            for route in ("/invocations", "/pause", "/tokenize"):
                assert (await http.post(route)).status_code == 200
            body = body_for(ROUTES[2])
            original = (await http.post(COUNT, json=body, headers=HEADERS)).json()["input_tokens"]
            assert (await http.post(COUNT, json=dict(reversed(list(body.items()))), headers=HEADERS)).json() == {
                "input_tokens": original
            }
            body["messages"][0]["content"] += " more" * 100
            assert (await http.post(COUNT, json=body, headers=HEADERS)).json()["input_tokens"] > original

    asyncio.run(scenario())


def test_pacing_and_hang_leave_health_available() -> None:
    async def scenario() -> None:
        elapsed = 0.0
        sent: list[tuple[float, Message]] = []
        pending: asyncio.Queue[Message] = asyncio.Queue()
        await pending.put({"type": "http.request", "body": json.dumps({**body_for(ROUTES[0]), "stream": True}).encode()})

        async def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        async def send(message: Message) -> None:
            sent.append((elapsed, message))

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": ROUTES[0], "query_string": b"",
            "headers": [(b"authorization", f"Bearer {SECRET}".encode())],
        }
        settings = mock.Settings.from_env({
            "RHI_POOL_SECRET": SECRET, "RHI_MOCK_FIRST_BYTE_SECONDS": "3", "RHI_MOCK_TOKENS_PER_SECOND": "4"
        })
        app = mock.create_app(settings, sleep=sleep)
        await app(scope, pending.get, send)
        assert sent[0][0] == 3
        token_times = []
        for timestamp, message in sent:
            raw = message.get("body", b"")
            if raw.startswith(b"data: {"):
                delta = json.loads(raw.removeprefix(b"data: "))["choices"][0]["delta"]
                if delta.get("reasoning") or delta.get("content"):
                    token_times.append(timestamp)
        assert len(token_times) > 1
        assert token_times[0] == 3.25
        assert all(later - earlier == 0.25 for earlier, later in pairwise(token_times))

        sent.clear()
        hung = mock.create_app(mock.Settings.from_env({"RHI_POOL_SECRET": SECRET, "RHI_MOCK_HANG": "1"}))
        await pending.put({"type": "http.request", "body": json.dumps(body_for(ROUTES[0])).encode()})
        task = asyncio.create_task(hung(scope, pending.get, send))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hung), base_url="http://mock") as http:
                assert (await http.get("/health")).status_code == 200
                done, _ = await asyncio.wait({task}, timeout=0.03)
                assert not done and sent == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))
