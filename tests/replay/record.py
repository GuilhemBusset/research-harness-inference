"""Record the captures the mock engine can produce by itself (`tests/captures/README.md`).

    uv run --locked python tests/replay/record.py

writes one capture for each request of `REQUESTS` into `tests/captures/`: the three wire formats,
each streamed and not. Nothing leaves the process. Each request is what a client would send to the
endpoint under an example host, with no key; it goes through the gateway's body filter of its
route, and what the filter gives is sent to the mock engine (`rhi_mock_engine`), in process. The
capture is then made of:

- the request, as written here;
- the body to forward: what the mock received, less the request ID and the cache salt, which the
  gateway writes for each request and each student;
- the answer: the mock's status, headers and bytes;
- the usage expected: what the mock itself counted, read from its `/metrics` and not from the
  answer the gateway will parse.

The mock stamps its answers with the time. It is recorded with its clock standing at `RECORDED_AT`,
so that recording again writes the same files until the mock or a filter changes. The scrubber
(`scrub.check`) reads each capture before it is written.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
# Python gives a script its own folder and nothing else: the roots pytest is given go on the path.
_PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
_ROOTS = [str(REPO / root) for root in _PYPROJECT["tool"]["pytest"]["ini_options"]["pythonpath"]]
sys.path[:0] = [root for root in _ROOTS if root not in sys.path]

import httpx
from prometheus_client.parser import text_string_to_metric_families
from starlette.types import Message

import harness
import scrub
from rhi_common.engine_metrics import MOCK, Quantity, metric_names
from rhi_gateway import routes
from rhi_gateway.app import RequestBody
from rhi_mock_engine import app as engine

# The moment the mock's clock stands at while it is recorded: 2027-01-15, an invented one.
RECORDED_AT = 1_800_000_000
# The gateway's ID of each recorded request. It is left out of what is written.
REQUEST_ID = "0" * 32
# The mock's bearer secret for the time of a recording, in this process only: it guards nothing.
SECRET = "the-recorder-and-its-mock"

_HEADERS = {"host": "gateway.example.com", "content-type": "application/json", "user-agent": "rhi-capture-recorder"}
_MESSAGES_HEADERS = {**_HEADERS, "anthropic-version": "2023-06-01"}
_SYSTEM = "You are a coding assistant. Answer briefly."
_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
_READ_FILE = "Read a file of the working directory."

# The capture's name: the request's path, its headers and its body, as a client sends them. Some
# declare a tool, and the mock then answers with a call of it; `store` is a field of OpenAI's
# service, which the filters drop.
REQUESTS: dict[str, tuple[str, dict[str, str], dict[str, Any]]] = {
    "mock-messages": (
        "/v1/messages",
        _MESSAGES_HEADERS,
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": "Say hello."}],
        },
    ),
    "mock-messages-stream": (
        "/v1/messages?beta=true",
        _MESSAGES_HEADERS,
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 32000,
            "stream": True,
            "system": [{"type": "text", "text": _SYSTEM}],
            "tools": [{"name": "read_file", "description": _READ_FILE, "input_schema": _SCHEMA}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "What does the README say?"}]}],
        },
    ),
    "mock-chat-completions": (
        "/v1/chat/completions",
        _HEADERS,
        {
            "model": "mock",
            "store": False,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "What does the README say?"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": _READ_FILE, "parameters": _SCHEMA},
                }
            ],
        },
    ),
    "mock-chat-completions-stream": (
        "/v1/chat/completions",
        _HEADERS,
        {
            "model": "mock",
            "stream": True,
            # Asked for: the gateway hides the usage chunk from a client that did not ask, and the
            # client would then not receive the upstream's answer byte for byte.
            "stream_options": {"include_usage": True},
            "max_completion_tokens": 512,
            "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": "Say hello."}],
        },
    ),
    "mock-responses": (
        "/v1/responses",
        _HEADERS,
        {"model": "mock", "instructions": _SYSTEM, "input": "Say hello.", "max_output_tokens": 512},
    ),
    "mock-responses-stream": (
        "/v1/responses",
        _HEADERS,
        {
            "model": "mock",
            "stream": True,
            "store": False,
            "instructions": _SYSTEM,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "What does the README say?"}]}],
            "tools": [{"type": "function", "name": "read_file", "description": _READ_FILE, "parameters": _SCHEMA}],
        },
    ),
}


async def _at_once(seconds: float) -> None:
    """The mock's wait between two tokens: none, for a recording."""


def _counted(exposition: str, quantity: Quantity) -> int:
    """What the mock's counter of a quantity stands at, in the text of its `/metrics`."""
    sample = metric_names(MOCK)[quantity] + "_total"
    values = [
        found.value
        for family in text_string_to_metric_families(exposition)
        for found in family.samples
        if found.name == sample
    ]
    return int(values[0])


async def _record(name: str) -> dict[str, Any]:
    path, headers, body = REQUESTS[name]
    sent = json.dumps(body, separators=(",", ":")).encode("utf-8")
    route = routes.match("POST", path.encode("ascii"))
    if route is None or route.upstream_path is None:
        raise ValueError(f"{path} is no forwarded route of the gateway")

    async def receive() -> Message:
        return {"type": "http.request", "body": sent, "more_body": False}

    forwarded = await harness.filters()[route].read(
        RequestBody(receive), student=str(harness.STUDENT), request_id=REQUEST_ID
    )
    # A mock of its own for each capture, so that its counters hold this request and no other.
    mock_engine = engine.create_app(engine.Settings(secret=SECRET), sleep=_at_once)
    transport = httpx.ASGITransport(app=mock_engine)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine.example.com") as client:
        answer = await client.post(
            route.upstream_path,
            content=forwarded.body,
            headers={"authorization": f"Bearer {SECRET}", "content-type": "application/json"},
        )
        answer.raise_for_status()
        metrics = (await client.get("/metrics")).text
    lines = answer.text.splitlines(keepends=True)
    capture = {
        "request": {"method": "POST", "path": path, "headers": headers, "body": sent.decode("utf-8")},
        "forward": {
            field: value for field, value in json.loads(forwarded.body).items() if field not in harness.LEFT_OUT
        },
        "answer": {
            "status": answer.status_code,
            "headers": dict(answer.headers),
            # One text for each line of a stream, so that the file reads and compares line by line.
            "body": lines if len(lines) > 1 else answer.text,
        },
        "usage": {
            "input_tokens": _counted(metrics, Quantity.INPUT_TOKENS),
            "output_tokens": _counted(metrics, Quantity.OUTPUT_TOKENS),
        },
    }
    scrub.check(capture)
    return capture


def record() -> dict[str, dict[str, Any]]:
    """Every capture of `REQUESTS`, by its name, as the JSON value its file holds."""

    async def every() -> dict[str, dict[str, Any]]:
        return {name: await _record(name) for name in REQUESTS}

    # Only the name `time` of the mock's module is replaced, by a clock that stands still.
    with mock.patch.object(engine, "time", SimpleNamespace(time=lambda: float(RECORDED_AT))):
        return asyncio.run(every())


def main() -> None:
    for name, capture in record().items():
        path = harness.CAPTURES / f"{name}.json"
        path.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
        print(path.relative_to(REPO))


if __name__ == "__main__":
    main()
