"""Independent issue 52 checks: enumeration, headers, wire payloads and safe messages."""

import json
from enum import Enum

import pytest

from rhi_common.causes import Cause
from rhi_gateway.errors import GatewayError, WireFormat, render_json, render_stream_event

EXPECTED = {
    "invalid_key": (401, None),
    "not_found": (404, None),
    "request_refused": (400, None),
    "concurrency_limit": (429, 5),
    "budget_reached": (402, None),
    "pool_ceiling_reached": (402, None),
    "warming": (503, 30),
    "capacity_unavailable": (503, 60),
    "pool_scheduled_only": (503, None),
    "pool_disabled": (503, None),
    "engine_error": (502, None),
    "time_limit": (504, None),
}
FORMATS = ("messages", "responses", "chat_completions")


def parse_sse(raw):
    assert isinstance(raw, bytes)
    frames = raw.decode("utf-8").split("\n\n")
    assert len(frames) == 2 and frames[-1] == ""
    fields = {}
    for line in frames[0].splitlines():
        key, _, value = line.partition(":")
        assert key not in fields
        fields[key] = value.lstrip(" ")
    assert set(fields) in ({"data"}, {"event", "data"})
    return fields.get("event"), json.loads(fields["data"])


def check_error(payload, name, sentence, anthropic=False, response=False):
    if anthropic:
        assert payload == {"type": "error", "error": {"type": name, "message": sentence}}
    elif response:
        assert payload["type"] == "response.failed"
        assert payload["response"]["status"] == "failed"
        assert payload["response"]["error"] == {"code": name, "message": sentence}
    else:
        assert payload == {"error": {"type": name, "code": name, "message": sentence, "param": None}}


def test_enumeration_and_design_table():
    assert issubclass(Cause, Enum)
    assert len(Cause) == 12
    assert {cause.value: (cause.status, cause.retry_after) for cause in Cause} == EXPECTED
    for cause in Cause:
        assert isinstance(cause.sentence, str) and cause.sentence.strip()


@pytest.mark.parametrize("name", EXPECTED)
@pytest.mark.parametrize("format_name", FORMATS)
def test_json_and_terminal_event(name, format_name):
    cause = Cause(name)
    wire = WireFormat(format_name)
    answer = render_json(cause, wire)
    status, retry = EXPECTED[name]
    assert answer.status == status
    headers = {key.lower(): value for key, value in answer.headers.items()}
    assert headers["content-type"] == "application/json"
    assert headers.get("retry-after") == (str(retry) if retry is not None else None)
    check_error(json.loads(answer.body), name, cause.sentence, anthropic=format_name == "messages")

    event_name, payload = parse_sse(render_stream_event(cause, wire))
    assert event_name == {"messages": "error", "responses": "response.failed", "chat_completions": None}[format_name]
    check_error(
        payload,
        name,
        cause.sentence,
        anthropic=format_name == "messages",
        response=format_name == "responses",
    )


@pytest.mark.parametrize("format_name", FORMATS)
def test_retry_overrides_are_per_cause(format_name):
    wire = WireFormat(format_name)
    replacements = {Cause.WARMING: 17, Cause.CAPACITY_UNAVAILABLE: 91, Cause.CONCURRENCY_LIMIT: 8}
    for cause in Cause:
        answer = render_json(cause, wire, retry_after=replacements)
        headers = {key.lower(): value for key, value in answer.headers.items()}
        expected = replacements.get(cause)
        assert headers.get("retry-after") == (str(expected) if expected is not None else None)
    assert render_json(Cause.WARMING, wire, {Cause.CONCURRENCY_LIMIT: 8}).headers["Retry-After"] == "30"


class UnprintableError(RuntimeError):
    def __str__(self):
        raise AssertionError("The renderer inspected exception text")


@pytest.mark.parametrize("format_name", FORMATS)
def test_exception_text_and_chains_never_reach_output(format_name):
    wire = WireFormat(format_name)
    address = "engine." + "example.com" + ":8080"
    account = "".join(("1111", "2222", "3333"))
    arn = ":".join(("arn", "aws", "ecs", "eu-west-1", account, "task/example"))
    foreign = f"{address} {arn}"
    for cause in Cause:
        error = GatewayError(cause, foreign)
        error.__cause__ = RuntimeError(foreign)
        error.__context__ = ConnectionError(foreign)
        assert render_json(error, wire) == render_json(cause, wire)
        assert render_stream_event(error, wire) == render_stream_event(cause, wire)
        answer = render_json(error, wire)
        combined = answer.body + repr(answer.headers).encode() + render_stream_event(error, wire)
        for forbidden in (address, arn, account):
            assert forbidden.encode() not in combined
    for unknown in (RuntimeError(foreign), UnprintableError(foreign)):
        assert render_json(unknown, wire) == render_json(Cause.ENGINE_ERROR, wire)
        assert render_stream_event(unknown, wire) == render_stream_event(Cause.ENGINE_ERROR, wire)
