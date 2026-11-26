"""The coursework pool of `config.example.toml`, through the gateway as its entry point builds it.

The pools are those the example loads, written into `RHI_POOLS` as the deployment writes them
(`rhi.config.deployed_entry`), and the gateway is built from its settings (`rhi_gateway.settings`) by
`rhi_gateway.main.build`. moto stands in for the tables and the secrets, a small double for the
target groups' health, and an `httpx.MockTransport` for the engine. The student's key is drawn when
the test runs.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import replace
from typing import Any
from unittest import mock

import boto3
import httpx
import pytest
from botocore.client import BaseClient
from moto import mock_aws

from rhi import config
from rhi_common import tables
from rhi_common.keys import generate_key, key_hash
from rhi_common.records import Session
from rhi_common.students import StudentKey
from rhi_gateway import profile, routes
from rhi_gateway.body import BodyFilter
from rhi_gateway.main import Gateway, aws_clients, build
from rhi_gateway.profile import document_as_text
from rhi_gateway.settings import Settings, read_settings

REGION = "us-east-1"
ACCOUNT = "123456789012"
UPSTREAM = "http://internal.example.com:8000"
TABLES = {
    "RHI_KEYS_TABLE": (tables.KEYS, "keys-of-the-test"),
    "RHI_USAGE_TABLE": (tables.USAGE, "usage-of-the-test"),
    "RHI_POOL_STATE_TABLE": (tables.POOL_STATE, "pool-state-of-the-test"),
}

# Claude Code's request for a PDF a tool read, at its default effort, with a reminder it placed
# between the tool's result and the student's next turn. The tool call and its result are a pair.
REMINDER = {"role": "system", "content": [{"type": "text", "text": "Reminder: the task list is empty."}]}
PDF = {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQKJSVFT0YK"}
TURNS: list[dict[str, Any]] = [
    {"role": "user", "content": [{"type": "text", "text": "What does notes.pdf say?"}]},
    {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {"file_path": "notes.pdf"}}],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01",
                "content": [{"type": "text", "text": "PDF file read"}, {"type": "document", "source": PDF}],
            }
        ],
    },
    REMINDER,
    {"role": "user", "content": [{"type": "text", "text": "Summarize it in one line."}]},
]
REQUEST = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 32000,
    "system": [{"type": "text", "text": "You are a coding assistant. Answer briefly."}],
    "tools": [
        {
            "name": "Read",
            "description": "Read a file of the working directory.",
            "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}},
        }
    ],
    "messages": TURNS,
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},
}
# Claude Code's request after the model searched for a deferred MCP tool: the result of `ToolSearch`
# names the tool found with a tool reference, and stays in the history of every later request.
FOUND_TOOL = "mcp__notes__read_note"
SEARCH_TURNS: list[dict[str, Any]] = [
    {"role": "user", "content": [{"type": "text", "text": "Read the note titled plan."}]},
    {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_01", "name": "ToolSearch", "input": {"query": "notes"}}],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01",
                "content": [
                    {"type": "text", "text": "One tool was found."},
                    {"type": "tool_reference", "tool_name": FOUND_TOOL},
                ],
            }
        ],
    },
]
SEARCH_REQUEST = {
    **REQUEST,
    "tools": [
        {
            "name": "ToolSearch",
            "description": "Find the deferred tools that match a query.",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        {
            "name": FOUND_TOOL,
            "description": "Read one note by its title.",
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
            "defer_loading": True,
        },
    ],
    "messages": SEARCH_TURNS,
}
ANSWER = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "A one-page note."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 40, "output_tokens": 6},
}


def target_group(pool: str) -> str:
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{pool}/0123456789abcdef"


class Targets:
    """`describe_target_health`, in the test: every target group has one healthy target."""

    def describe_target_health(self, **arguments: str) -> dict[str, Any]:
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}]}


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    # The gateway calls the SDK from several threads, and moto's tables take one call at a time.
    one_at_a_time = threading.Lock()
    make_api_call = BaseClient._make_api_call

    def call(self: BaseClient, operation: str, arguments: Any) -> Any:
        with one_at_a_time:
            return make_api_call(self, operation, arguments)

    with mock_aws(), mock.patch.object(BaseClient, "_make_api_call", call):
        dynamodb = boto3.client("dynamodb")
        for table, name in TABLES.values():
            keys = [(table.partition_key, "HASH")] + ([(table.sort_key, "RANGE")] if table.sort_key else [])
            dynamodb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys],
                AttributeDefinitions=[{"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys],
                BillingMode="PAY_PER_REQUEST",
            )
        # The coursework pool is booked for the hour that starts now: the student of these tests is served on it (section 5.6).
        now = int(time.time())
        booked = Session("coursework", instances=1, starts_at=now, ends_at=now + 3600, set_by="owner", set_at=now)
        boto3.resource("dynamodb").Table(TABLES["RHI_POOL_STATE_TABLE"][1]).put_item(Item=booked.to_item())
        yield


def example_settings() -> Settings:
    """The gateway's settings for the pools of `config.example.toml`, as the deployment writes them."""
    example = config.load({"RHI_CONFIG": "example"})
    names = [pool.name for pool in example.pools]
    secretsmanager = boto3.client("secretsmanager")
    arns = {
        name: secretsmanager.create_secret(Name=f"pool-{name}", SecretString=secrets.token_urlsafe(24))["ARN"]
        for name in names
    }
    environ = {
        "RHI_POOLS": json.dumps({pool.name: config.deployed_entry(pool) for pool in example.pools}),
        "RHI_DEFAULT_POOL": str(example.default_pool),
        "RHI_UPSTREAM_URL": UPSTREAM,
        "RHI_TARGET_GROUPS": json.dumps({name: target_group(name) for name in names}),
        "RHI_POOL_SECRETS": json.dumps(arns),
        "RHI_COURSE_END": "2027-01-15",
        **{variable: name for variable, (_, name) in TABLES.items()},
    }
    return read_settings(environ)


def issue_key() -> str:
    key = generate_key()
    now = int(time.time())
    item = StudentKey(
        key_hash=key_hash(key),
        github_id=7,
        alias="ada",
        installation=None,
        issued_at=now - 60,
        expires_at=now + 86_400,
        revoked_at=None,
        concurrency_limit=2,
    ).to_item()
    boto3.resource("dynamodb").Table(TABLES["RHI_KEYS_TABLE"][1]).put_item(Item=item)
    return key


def started(gateway: Gateway, scenario: Callable[[httpx.AsyncClient], Awaitable[None]]) -> None:
    """Start the application (its lifespan), run `scenario` with a client of it, and stop it."""

    async def run() -> None:
        app = gateway.app
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.example.com") as client,
        ):
            await scenario(client)

    asyncio.run(asyncio.wait_for(run(), timeout=30))


@pytest.mark.usefixtures("aws")
def test_the_gateway_starts() -> None:
    settings = example_settings()
    key = issue_key()
    gateway = build(settings, aws_clients())

    async def scenario(client: httpx.AsyncClient) -> None:
        assert (await client.head("/api/hello")).status_code == 200
        listed = (await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})).json()
        assert ([model["id"] for model in listed["data"]], listed["default"]) == (["coursework", "mock"], "coursework")

    started(gateway, scenario)

    # The coursework pool's profile was built, its four entries in order, and the Messages filter runs it.
    built = profile.build(settings.pools)["coursework"]
    assert [item.name for item in built.entries] == [
        "reasoning_effort",
        "system_in_place",
        "document_as_text",
        "tool_reference_as_text",
    ]
    messages_filter = gateway.forwarding.filters[routes.MESSAGES]
    assert isinstance(messages_filter, BodyFilter)
    assert "coursework" in messages_filter.profiles


def received(request: dict[str, Any]) -> dict[str, Any]:
    """The body the coursework pool's engine receives for a request Claude Code sends, which it answers."""
    key = issue_key()
    sent: list[dict[str, Any]] = []

    def engine(asked: httpx.Request) -> httpx.Response:
        sent.append(json.loads(asked.content))
        return httpx.Response(200, json=ANSWER)

    clients = replace(aws_clients(), elbv2=Targets())
    gateway = build(example_settings(), clients, transport=httpx.MockTransport(engine))

    async def scenario(client: httpx.AsyncClient) -> None:
        while not gateway.readiness.pool("coursework").ready:
            await asyncio.sleep(0.01)
        answer = await client.post("/v1/messages?beta=true", headers={"Authorization": f"Bearer {key}"}, json=request)
        assert (answer.status_code, answer.json()) == (200, ANSWER)

    started(gateway, scenario)

    [body] = sent
    return body


@pytest.mark.usefixtures("aws")
def test_a_claude_code_request_reaches_the_coursework_engine_in_the_form_the_model_accepts() -> None:
    body = received(REQUEST)
    assert body["model"] == "coursework"
    # Effort high becomes xhigh, the one the model's template accepts above medium.
    assert body["output_config"]["effort"] == "xhigh"
    # The reminder keeps its place between the two user turns, as user content.
    sent_turns, asked_turns = body["messages"], TURNS
    assert len(sent_turns) == len(asked_turns)
    assert sent_turns[3] == {**REMINDER, "role": "user"}
    assert [turn["role"] for turn in sent_turns] == ["user", "assistant", "user", "user", "user"]
    # The tool call and its result stay a pair; the document in the result is the profile's sentence.
    assert sent_turns[1] == asked_turns[1]
    [result] = sent_turns[2]["content"]
    assert result["tool_use_id"] == "toolu_01"
    assert result["content"] == [
        {"type": "text", "text": "PDF file read"},
        {"type": "text", "text": document_as_text.SENTENCE},
    ]


@pytest.mark.usefixtures("aws")
def test_a_tool_reference_reaches_the_coursework_engine_as_a_line_of_text_in_its_place() -> None:
    body = received(SEARCH_REQUEST)
    assert body["model"] == "coursework"
    # The search's call and its result stay a pair, and the deferred tool stays declared.
    sent_turns = body["messages"]
    assert sent_turns[:2] == SEARCH_TURNS[:2]
    assert body["tools"] == SEARCH_REQUEST["tools"]
    [result] = sent_turns[2]["content"]
    assert result["tool_use_id"] == "toolu_01"
    assert result["content"] == [
        {"type": "text", "text": "One tool was found."},
        {"type": "text", "text": f"[tool reference: {FOUND_TOOL}]"},
    ]
