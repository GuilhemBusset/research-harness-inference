"""The mock engine's metrics, its limit on running requests, its shutdown and its image (system design, section 4.1).

The metrics and the limit are exercised in process, with a `sleep` the test controls, so nothing
waits. The shutdown is exercised on a real process and a real signal, over a Unix socket in a
temporary folder: no port, no network. The Dockerfile is read, not built: this host has no
container runtime, and CI's container job builds and starts the image.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
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
from starlette.types import Message

from rhi_common.engine_metrics import MOCK, Kind, Quantity, metric_names
from rhi_mock_engine.app import Settings, SettingsError, create_app

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "services/engine/mock/Dockerfile"

# A made-up value, not a credential: the mock only compares it with what a request carries.
SECRET = "-".join(["a", "pool", "secret", "for", "the", "metrics"])
AUTHORIZED = {"Authorization": f"Bearer {SECRET}"}
CHAT = "/v1/chat/completions"
NAMES = metric_names(MOCK)


def chat(prompt: str, **fields: Any) -> dict[str, Any]:
    return {"model": "coursework", "messages": [{"role": "user", "content": prompt}], **fields}


def http(app: Starlette, client: tuple[str, int] = ("127.0.0.1", 40000)) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://engine")


async def scrape(client: httpx.AsyncClient) -> dict[str, float]:
    """The metrics as a loopback client reads them: each sample's value by the quantity's name and suffix."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    return {
        sample.name: sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
        if not sample.name.endswith(("_bucket", "_created"))
    }


def value(samples: dict[str, float], quantity: Quantity) -> float:
    suffix = {Kind.GAUGE: "", Kind.COUNTER: "_total", Kind.HISTOGRAM: "_count"}[quantity.kind]
    return samples[NAMES[quantity] + suffix]


# --- The metrics route ---------------------------------------------------------------------------


async def no_sleep(seconds: float) -> None:
    del seconds


async def asgi_get(app: Starlette, path: str, client: tuple[str, int] | None) -> int:
    """The status of a GET called straight over ASGI, with the client the scope names, or none."""
    statuses: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("127.0.0.1", 8000),
    }
    if client is not None:
        scope["client"] = client
    await app(scope, receive, send)
    return statuses[0]


def test_the_metrics_route_serves_every_name_to_a_loopback_client_only() -> None:
    app = create_app(Settings(secret=SECRET), sleep=no_sleep)

    async def scenario() -> None:
        for loopback in ("127.0.0.1", "::1"):
            async with http(app, (loopback, 40000)) as client:
                response = await client.get("/metrics")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain")
            families = {family.name: family.type for family in text_string_to_metric_families(response.text)}
            assert {NAMES[quantity]: quantity.kind.value for quantity in Quantity}.items() <= families.items()
        for other in (("203.0.113.7", 40000), ("10.0.0.5", 40000), ("testclient", 50000), None):
            assert await asgi_get(app, "/metrics", other) == 404, other
        # The same client is served the rest: only the metrics are kept to the loopback address.
        assert await asgi_get(app, "/health", ("203.0.113.7", 40000)) == 200

    asyncio.run(scenario())


def test_a_stream_counts_its_tokens_and_the_gaps_between_them() -> None:
    app = create_app(Settings(secret=SECRET), sleep=no_sleep)

    async def scenario() -> None:
        async with http(app) as client:
            body = chat("Read the file notes.txt", stream=True, stream_options={"include_usage": True})
            response = await client.post(CHAT, json=body, headers=AUTHORIZED)
            chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
            usage = chunks[-1]["usage"]
            samples = await scrape(client)

        assert value(samples, Quantity.INPUT_TOKENS) == usage["prompt_tokens"]
        assert value(samples, Quantity.OUTPUT_TOKENS) == usage["completion_tokens"]
        assert value(samples, Quantity.TIME_TO_FIRST_TOKEN) == 1
        assert value(samples, Quantity.TIME_PER_OUTPUT_TOKEN) == usage["completion_tokens"] - 1
        assert value(samples, Quantity.RUNNING_REQUESTS) == value(samples, Quantity.WAITING_REQUESTS) == 0

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


# --- The limit -----------------------------------------------------------------------------------


def test_the_limit_is_a_setting() -> None:
    assert Settings.from_env({"RHI_POOL_SECRET": SECRET}).max_running == 8
    assert Settings.from_env({"RHI_POOL_SECRET": SECRET, "RHI_MOCK_MAX_RUNNING": "2"}).max_running == 2
    for refused in ("0", "-1", "1.5", "many"):
        with pytest.raises(SettingsError):
            Settings.from_env({"RHI_POOL_SECRET": SECRET, "RHI_MOCK_MAX_RUNNING": refused})


def test_running_and_waiting() -> None:
    first_byte = 1.0
    # One gate for each request that reaches its first-byte wait, which it holds a slot for.
    gates: list[asyncio.Event] = []

    async def sleep(seconds: float) -> None:
        if seconds == first_byte:
            gate = asyncio.Event()
            gates.append(gate)
            await gate.wait()

    app = create_app(Settings(secret=SECRET, max_running=2, first_byte_seconds=first_byte), sleep=sleep)

    async def scenario() -> None:
        async with http(app) as client:

            async def settled(condition: Any) -> dict[str, float]:
                for _ in range(1000):
                    samples = await scrape(client)
                    if condition(samples):
                        return samples
                    await asyncio.sleep(0)
                raise AssertionError("the metrics never reached the expected state")

            prompts = ("Read notes.txt", "List the folder, then read every file in it", "Hi")
            sent = [asyncio.ensure_future(client.post(CHAT, json=chat(p), headers=AUTHORIZED)) for p in prompts]

            samples = await settled(lambda s: value(s, Quantity.WAITING_REQUESTS) == 1 and len(gates) == 2)
            assert value(samples, Quantity.RUNNING_REQUESTS) == 2
            # Still so while nothing has ended.
            for _ in range(50):
                await asyncio.sleep(0)
            samples = await scrape(client)
            assert (value(samples, Quantity.RUNNING_REQUESTS), value(samples, Quantity.WAITING_REQUESTS)) == (2, 1)
            assert len(gates) == 2
            assert not any(request.done() for request in sent)

            gates[0].set()
            done, _ = await asyncio.wait(sent, return_when=asyncio.FIRST_COMPLETED)
            assert len(done) == 1
            samples = await settled(lambda s: len(gates) == 3)
            assert (value(samples, Quantity.RUNNING_REQUESTS), value(samples, Quantity.WAITING_REQUESTS)) == (2, 0)

            for gate in gates[1:]:
                gate.set()
            answers = [(await request).json()["usage"] for request in sent]
            samples = await scrape(client)

        assert value(samples, Quantity.INPUT_TOKENS) == sum(usage["prompt_tokens"] for usage in answers)
        assert value(samples, Quantity.OUTPUT_TOKENS) == sum(usage["completion_tokens"] for usage in answers)
        assert value(samples, Quantity.TIME_TO_FIRST_TOKEN) == 3
        assert value(samples, Quantity.RUNNING_REQUESTS) == value(samples, Quantity.WAITING_REQUESTS) == 0
        # The mock has no cache.
        assert value(samples, Quantity.CACHED_INPUT_TOKENS) == 0
        assert value(samples, Quantity.CACHE_USAGE) == 0

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


# --- The shutdown --------------------------------------------------------------------------------

# The child serves the mock as `main` does, through `serve`, on the Unix socket it is given.
CHILD = "import os, sys; from rhi_mock_engine.app import Settings, serve; serve(Settings.from_env(os.environ), uds=sys.argv[1])"


def test_sigterm_finishes_requests_in_flight() -> None:
    with tempfile.TemporaryDirectory(prefix="rhi-mock-") as folder:
        socket_path = Path(folder) / "mock.sock"
        environ = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(REPO / "services/engine/mock"), str(REPO / "services/common")]),
            "RHI_POOL_SECRET": SECRET,
            "RHI_MOCK_FIRST_BYTE_SECONDS": "0",
            # 21 tokens at 20 a second: about a second of stream after the signal.
            "RHI_MOCK_TOKENS_PER_SECOND": "20",
            "RHI_MOCK_START_SECONDS": "0",
            "RHI_MOCK_HANG": "0",
        }
        with (Path(folder) / "mock.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, "-c", CHILD, str(socket_path)], env=environ, stderr=log)
        try:
            transport = httpx.HTTPTransport(uds=str(socket_path))
            with httpx.Client(transport=transport, base_url="http://engine", timeout=10) as client:
                deadline = time.monotonic() + 20
                while not (socket_path.exists() and client.get("/health").status_code == 200):
                    assert child.poll() is None, (Path(folder) / "mock.log").read_text()
                    assert time.monotonic() < deadline, "the mock did not start"
                    time.sleep(0.05)

                body = chat("Read the file notes.txt", stream=True, stream_options={"include_usage": True})
                with client.stream("POST", CHAT, json=body, headers=AUTHORIZED) as response:
                    lines = (line for line in response.iter_lines() if line.startswith("data: "))
                    first = next(lines)
                    child.send_signal(signal.SIGTERM)
                    after = list(lines)

            child.wait(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    assert json.loads(first[6:])["choices"][0]["delta"]["role"] == "assistant"
    assert after[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in after[:-1]]
    tokens = [chunk for chunk in chunks if chunk["choices"] and set(chunk["choices"][0]["delta"]) & {"reasoning", "content"}]
    # Every token of the turn came after the signal, and the turn is whole.
    assert len(tokens) == chunks[-1]["usage"]["completion_tokens"] > 0
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"


# --- The image -----------------------------------------------------------------------------------

_PINNED = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


def arguments(text: str, keyword: str) -> list[str]:
    """The arguments of each instruction of a Dockerfile with that keyword, as written, continuation lines joined."""
    lines = (line.strip() for line in re.sub(r"\\\n", " ", text).splitlines())
    return [line[len(keyword) :].strip() for line in lines if line.upper().startswith(keyword + " ")]


def instructions(text: str, keyword: str) -> list[list[str]]:
    """The same, each split into words."""
    return [shlex.split(written) for written in arguments(text, keyword)]


def base_images(text: str) -> list[str]:
    return [next(word for word in words if not word.startswith("--")) for words in instructions(text, "FROM")]


def unpinned(text: str) -> list[str]:
    """The base images that are not pinned by a digest."""
    return [image for image in base_images(text) if not _PINNED.fullmatch(image)]


def test_the_base_image_is_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert base_images(text)
    assert unpinned(text) == []
    # The same check refuses the same file with its digest taken away.
    stripped = re.sub(r"@sha256:[0-9a-f]{64}", "", text)
    assert unpinned(stripped) == base_images(stripped) != []


def test_the_image_is_built_from_its_build_context_with_hashes_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    # No platform is fixed: the builder's --platform, or its own, picks the image from the index.
    assert not any(word.startswith("--platform") for words in instructions(text, "FROM") for word in words)
    [install] = [words for words in instructions(text, "RUN") if "install" in words]
    assert {"--require-hashes", "--no-deps"} <= set(install)
    assert install[install.index("--requirement") + 1] == "requirements.txt"
    # Python is process 1, so SIGTERM reaches it.
    [command] = arguments(text, "CMD")
    assert json.loads(command)[:3] == ["python", "-m", "rhi_mock_engine.app"]

    # The packaging module imports the CDK, whose jsii refuses a package cache below a directory
    # another user owns, as in the implement loop's sandbox; without the cache it needs none.
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    from rhi import packaging

    context = Path(packaging.image_context("mock-engine", DOCKERFILE))
    for words in instructions(text, "COPY"):
        sources = [word for word in words[:-1] if not word.startswith("--")]
        assert sources
        assert all((context / source).exists() for source in sources), sources
    exported = re.sub(r"\\\n", " ", (context / "requirements.txt").read_text(encoding="utf-8"))
    requirements = [line for line in exported.splitlines() if "==" in line]
    assert requirements
    assert all("--hash=sha256:" in line for line in requirements)
