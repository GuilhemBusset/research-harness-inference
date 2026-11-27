"""A Messages request that declares one of Anthropic's own tools is refused (`rhi_gateway.formats.messages`).

The gateway is built by its entry point, `rhi_gateway.main.build`, from the pools of
`config.example.toml` written as the deployment writes them, and each request goes to the
coursework pool on both Messages routes. moto stands in for the tables and the secrets, a small
double for the target groups' health, and an `httpx.MockTransport` for the engine, which records
what reaches it. The student's key is drawn when the test runs.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Generator
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
from rhi_gateway.main import aws_clients, build
from rhi_gateway.settings import read_settings

REGION = "us-east-1"
ACCOUNT = "123456789012"
TABLES = {
    "RHI_KEYS_TABLE": (tables.KEYS, "keys-of-the-test"),
    "RHI_USAGE_TABLE": (tables.USAGE, "usage-of-the-test"),
    "RHI_POOL_STATE_TABLE": (tables.POOL_STATE, "pool-state-of-the-test"),
}
ROUTES = ["/v1/messages", "/v1/messages/count_tokens"]

# The request Claude Code 2.1.323 sends to run a web search, its system text and `metadata` left out.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
WEB_SEARCH: dict[str, Any] = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 128000,
    "stream": True,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Perform a web search for the query: tides"}]}],
    "tools": [WEB_SEARCH_TOOL],
    "tool_choice": {"type": "auto"},
    "output_config": {"effort": "medium"},
}
SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}}}
BASH = {"type": "bash_20250124", "name": "bash"}
CUSTOM = {"type": "custom", "name": "Bash", "description": "Run a command.", "input_schema": SCHEMA}
UNTYPED = {"name": "Bash", "description": "Run a command.", "input_schema": SCHEMA}


def request_with(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "List the files."}]}],
        "tools": [tool],
    }


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
        now = int(time.time())
        booked = Session("coursework", instances=1, starts_at=now, ends_at=now + 3600, set_by="owner", set_at=now)
        boto3.resource("dynamodb").Table(TABLES["RHI_POOL_STATE_TABLE"][1]).put_item(Item=booked.to_item())
        yield


def exchange(path: str, request: dict[str, Any]) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    """The status and body the gateway answers to `request` on `path`, and the bodies its engine received."""
    example = config.load({"RHI_CONFIG": "example"})
    names = [pool.name for pool in example.pools]
    secretsmanager = boto3.client("secretsmanager")
    settings = read_settings(
        {
            "RHI_POOLS": json.dumps({pool.name: config.deployed_entry(pool) for pool in example.pools}),
            "RHI_DEFAULT_POOL": str(example.default_pool),
            "RHI_UPSTREAM_URL": "http://internal.example.com:8000",
            "RHI_TARGET_GROUPS": json.dumps(
                {name: f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{name}/0123" for name in names}
            ),
            "RHI_POOL_SECRETS": json.dumps(
                {name: secretsmanager.create_secret(Name=f"pool-{name}", SecretString="s")["ARN"] for name in names}
            ),
            "RHI_COURSE_END": "2027-01-15",
            **{variable: name for variable, (_, name) in TABLES.items()},
        }
    )
    key = generate_key()
    now = int(time.time())
    student = StudentKey(key_hash(key), 7, "ada", None, now - 60, now + 86_400, None, 2)
    boto3.resource("dynamodb").Table(TABLES["RHI_KEYS_TABLE"][1]).put_item(Item=student.to_item())
    sent: list[dict[str, Any]] = []

    def engine(asked: httpx.Request) -> httpx.Response:
        sent.append(json.loads(asked.content))
        if asked.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 12})
        answer = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Done."}]}
        return httpx.Response(200, json={**answer, "stop_reason": "end_turn", "usage": {"input_tokens": 12}})

    gateway = build(settings, replace(aws_clients(), elbv2=Targets()), transport=httpx.MockTransport(engine))
    answered: list[httpx.Response] = []

    async def run() -> None:
        app = gateway.app
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://gateway.example.com") as client,
        ):
            while not gateway.readiness.pool("coursework").ready:
                await asyncio.sleep(0.01)
            answered.append(await client.post(path, headers={"Authorization": f"Bearer {key}"}, json=request))

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    [answer] = answered
    return answer.status_code, answer.json(), sent


@pytest.mark.usefixtures("aws")
@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("request_body", [WEB_SEARCH, request_with(BASH)], ids=["web_search", "bash_20250124"])
def test_a_request_declaring_one_of_anthropics_tools_is_refused_before_it_reaches_an_engine(
    path: str, request_body: dict[str, Any]
) -> None:
    status, answer, sent = exchange(path, request_body)
    assert (status, answer["error"]["type"], sent) == (400, "request_refused", [])


@pytest.mark.usefixtures("aws")
@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("tool", [CUSTOM, UNTYPED, {**UNTYPED, "type": None}], ids=["custom", "no_type", "null_type"])
def test_a_request_whose_tool_is_a_custom_tool_is_forwarded_with_its_tools_unchanged(
    path: str, tool: dict[str, Any]
) -> None:
    status, _, [body] = exchange(path, request_with(tool))
    assert (status, body["tools"]) == (200, [tool])


@pytest.mark.usefixtures("aws")
@pytest.mark.parametrize("path", ROUTES)
def test_a_server_tool_in_a_system_messages_tool_addition_is_not_refused(path: str) -> None:
    addition = {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": WEB_SEARCH_TOOL}}
    request = request_with(CUSTOM)
    request["messages"] = [*request["messages"], {"role": "system", "content": [addition]}]
    status, _, sent = exchange(path, request)
    assert (status, len(sent)) == (200, 1)
