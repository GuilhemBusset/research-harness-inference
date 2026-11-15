"""Independent contract checks for the mock's metrics, scheduling, shutdown and image."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from starlette.applications import Starlette

from rhi_common.engine_metrics import Quantity, metric_names
from rhi_mock_engine.app import Settings, create_app

ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"Authorization": "Bearer examiner-mock-value"}
ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


def body(stream: bool) -> dict[str, Any]:
    return {
        "model": "mock", "messages": [{"role": "user", "content": "hello"}],
        "input": "hello", "stream": stream, "stream_options": {"include_usage": True},
    }


def samples(text: str) -> dict[str, float]:
    return {
        sample.name: sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
        if not sample.labels
    }


def metric(data: dict[str, float], quantity: str, suffix: str = "") -> float:
    return data[metric_names("mock")[Quantity(quantity)] + suffix]


def client(app: Starlette, peer: str = "127.0.0.2") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url="http://mock")


def usage(response: httpx.Response, route: str, stream: bool) -> tuple[int, int]:
    assert response.status_code == 200
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        if route.endswith("responses"):
            reported = events[-1]["response"]["usage"]
        elif route.endswith("messages"):
            return events[0]["message"]["usage"]["input_tokens"], events[-2]["usage"]["output_tokens"]
        else:
            reported = events[-1]["usage"]
    else:
        reported = response.json()["usage"]
    return reported.get("input_tokens", reported.get("prompt_tokens")), reported.get(
        "output_tokens", reported.get("completion_tokens")
    )


def test_table_and_peer_restriction() -> None:
    row = metric_names("mock")
    assert set(row) == {
        "running_requests", "waiting_requests", "time_to_first_token", "time_per_output_token",
        "input_tokens", "cached_input_tokens", "output_tokens", "cache_usage",
    }
    assert len(set(row.values())) == 8

    async def scenario() -> None:
        app = create_app(Settings(secret="examiner-mock-value"))
        for peer in ("127.0.0.2", "::1", "192.0.2.1"):
            async with client(app, peer) as connection:
                response = await connection.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
            if peer == "192.0.2.1":
                assert response.status_code == 404
            else:
                assert response.status_code == 200
                families = {family.name for family in text_string_to_metric_families(response.text)}
                assert set(row.values()) <= families

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ROUTES)
def test_metrics_measure_real_work_and_time(route: str) -> None:
    now = 0.0

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    app = create_app(
        Settings(secret="examiner-mock-value", first_byte_seconds=0.2, tokens_per_second=25),
        clock=lambda: now, sleep=sleep,
    )

    async def scenario() -> None:
        async with client(app) as connection:
            answer = await connection.post(route, json=body(True), headers=HEADERS)
            incoming, outgoing = usage(answer, route, True)
            data = samples((await connection.get("/metrics")).text)
            assert metric(data, "input_tokens", "_total") == incoming
            assert metric(data, "output_tokens", "_total") == outgoing
            assert metric(data, "time_to_first_token", "_count") == 1
            assert metric(data, "time_to_first_token", "_sum") == pytest.approx(0.24)
            assert metric(data, "time_per_output_token", "_count") == outgoing - 1
            assert metric(data, "time_per_output_token", "_sum") == pytest.approx((outgoing - 1) / 25)
            second = await connection.post(route, json=body(False), headers=HEADERS)
            more_in, more_out = usage(second, route, False)
            data = samples((await connection.get("/metrics")).text)
            assert metric(data, "input_tokens", "_total") == incoming + more_in
            assert metric(data, "output_tokens", "_total") == outgoing + more_out
            assert metric(data, "time_to_first_token", "_sum") == pytest.approx(0.44)
            assert metric(data, "cached_input_tokens", "_total") == metric(data, "cache_usage") == 0
            assert metric(data, "running_requests") == metric(data, "waiting_requests") == 0

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_streams_hold_slots_and_waiters_run_after_completion() -> None:
    async def scenario() -> None:
        gates: list[asyncio.Event] = []

        async def sleep(delay: float) -> None:
            if delay == 0.25:
                gate = asyncio.Event()
                gates.append(gate)
                await gate.wait()

        app = create_app(Settings(secret="examiner-mock-value", max_running=2, tokens_per_second=4), sleep=sleep)
        async with client(app) as connection:
            sent = [asyncio.create_task(connection.post(route, json=body(True), headers=HEADERS)) for route in ROUTES]

            async def settle(count: int) -> None:
                while len(gates) < count:
                    await asyncio.sleep(0)

            await settle(2)
            for _ in range(2):
                data = samples((await connection.get("/metrics")).text)
                assert (metric(data, "running_requests"), metric(data, "waiting_requests")) == (2, 1)
                await asyncio.sleep(0)
            # Allow every token of the first stream, keeping the second blocked.
            while not sent[0].done():
                for gate in gates:
                    if gate is not gates[1]:
                        gate.set()
                await asyncio.sleep(0)
            while True:
                data = samples((await connection.get("/metrics")).text)
                if metric(data, "waiting_requests") == 0:
                    break
                await asyncio.sleep(0)
            assert (metric(data, "running_requests"), metric(data, "waiting_requests")) == (2, 0)
            while not all(task.done() for task in sent):
                for gate in gates:
                    gate.set()
                await asyncio.sleep(0)
            totals = [usage(await task, route, True) for task, route in zip(sent, ROUTES, strict=True)]
            data = samples((await connection.get("/metrics")).text)
            assert metric(data, "input_tokens", "_total") == sum(item[0] for item in totals)
            assert metric(data, "output_tokens", "_total") == sum(item[1] for item in totals)
            assert metric(data, "running_requests") == metric(data, "waiting_requests") == 0

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_cancelled_waiter_and_running_request_return_the_slot() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def sleep(delay: float) -> None:
            del delay
            await release.wait()

        app = create_app(Settings(secret="examiner-mock-value", max_running=1), sleep=sleep)
        async with client(app) as connection:
            running = asyncio.create_task(connection.post(ROUTES[0], json=body(False), headers=HEADERS))
            waiting = asyncio.create_task(connection.post(ROUTES[1], json=body(False), headers=HEADERS))
            while True:
                data = samples((await connection.get("/metrics")).text)
                if metric(data, "waiting_requests") == 1:
                    break
                await asyncio.sleep(0)
            assert metric(data, "running_requests") == 1
            for request in (waiting, running):
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            release.set()
            response = await connection.post(ROUTES[2], json=body(False), headers=HEADERS)
            assert response.status_code == 200
            data = samples((await connection.get("/metrics")).text)
            assert metric(data, "waiting_requests") == metric(data, "running_requests") == 0

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_real_sigterm_preserves_messages_stream() -> None:
    child_code = (
        "import sys; from rhi_mock_engine.app import Settings, serve; "
        "serve(Settings(secret='examiner-mock-value', tokens_per_second=30), uds=sys.argv[1])"
    )
    with tempfile.TemporaryDirectory(prefix="mock-examiner-") as folder:
        socket = Path(folder) / "server.sock"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(
            str(ROOT / path) for path in ("services/common", "services/engine/mock")
        )}
        with subprocess.Popen(
            [sys.executable, "-c", child_code, str(socket)], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not socket.exists():
                    assert process.poll() is None
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://mock", timeout=10) as c:
                    with c.stream("POST", "/v1/messages", json=body(True), headers=HEADERS) as response:
                        assert response.status_code == 200
                        lines = response.iter_lines()
                        # Send the signal after generation has started, before it finishes.
                        assert next(line for line in lines if line.startswith("event: content_block_delta"))
                        process.send_signal(signal.SIGTERM)
                        remaining = list(lines)
                    assert "event: message_stop" in remaining
                    assert "event: message_delta" in remaining
                    assert any('"thinking_delta"' in line for line in remaining)
                    events = [json.loads(line[6:]) for line in remaining if line.startswith("data: ")]
                    tokens = [event for event in events if event.get("delta", {}).get("type") in (
                        "thinking_delta", "text_delta", "input_json_delta",
                    )]
                    final_usage = next(event["usage"] for event in events if event["type"] == "message_delta")
                    assert len(tokens) == final_usage["output_tokens"] > 1
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


def test_image_uses_digest_and_hashed_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    from rhi.packaging import image_context

    dockerfile = ROOT / "services/engine/mock/Dockerfile"
    text = dockerfile.read_text()
    bases = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE | re.IGNORECASE)
    assert bases and all(re.fullmatch(r"[^@]+@sha256:[a-f0-9]{64}", base) for base in bases)
    assert "--platform" not in "\n".join(line for line in text.splitlines() if line.startswith("FROM"))
    assert "--require-hashes" in text and "--requirement requirements.txt" in text
    context = Path(image_context("mock-engine", dockerfile))
    assert (context / "rhi_common/engine_metrics.py").is_file()
    assert (context / "rhi_mock_engine/app.py").is_file()
    requirements = (context / "requirements.txt").read_text().replace("\\\n", " ")
    assert all("--hash=sha256:" in line for line in requirements.splitlines() if "==" in line)
    assert json.loads(re.findall(r"^CMD\s+(.+)$", text, re.MULTILINE)[0]) == ["python", "-m", "rhi_mock_engine.app"]
