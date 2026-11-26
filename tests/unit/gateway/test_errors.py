"""The causes, and an error rendered in each wire format (`rhi_common.causes`, `rhi_gateway.errors`).

The statuses and the Retry-After seconds are written here from the table of the system design,
section 6.2, and not read from the enumeration. The sentences are the enumeration's: a test here
checks where a sentence goes, not how it is worded.
"""

from __future__ import annotations

import json

import pytest

from rhi_common.causes import Cause
from rhi_gateway.errors import GatewayError, WireFormat, cause_of, render_json, render_stream_event

# Cause: (status, seconds of Retry-After or None), as in the design's table.
DESIGN = {
    "invalid_key": (401, None),
    "not_found": (404, None),
    "request_refused": (400, None),
    "concurrency_limit": (429, 5),
    "warming": (503, 30),
    "capacity_unavailable": (503, 60),
    "no_session": (503, None),
    "engine_error": (502, None),
    "time_limit": (504, None),
    "context_length_exceeded": (400, None),
}
FORMATS = ("messages", "responses", "chat_completions")
EVERY_CAUSE_IN_EVERY_FORMAT = [(name, wire) for name in DESIGN for wire in FORMATS]


def anthropic_error(name: str) -> dict[str, object]:
    return {"type": "error", "error": {"type": name, "message": Cause(name).sentence}}


def openai_error(name: str) -> dict[str, object]:
    return {"error": {"message": Cause(name).sentence, "type": name, "param": None, "code": name}}


def read_event(raw: bytes) -> tuple[str | None, object]:
    """The name and the parsed data of the one server-sent event that `raw` must be."""
    text = raw.decode("ascii")
    assert text.endswith("\n\n")
    lines = text.removesuffix("\n\n").split("\n")
    fields = dict(line.split(": ", 1) for line in lines)
    assert len(fields) == len(lines)
    assert set(fields) <= {"event", "data"}
    return fields.get("event"), json.loads(fields["data"])


def test_statuses_are_the_designs() -> None:
    assert {cause.value: (cause.status, cause.retry_after) for cause in Cause} == DESIGN
    assert len(Cause) == 10

    for name, (status, seconds) in DESIGN.items():
        for wire in FORMATS:
            answer = render_json(Cause(name), WireFormat(wire))
            assert answer.status == status
            # The header is there with the design's seconds, or it is not there at all.
            assert answer.headers.get("Retry-After") == (None if seconds is None else str(seconds))
            assert [header.lower() for header in answer.headers].count("retry-after") <= 1

    given = {Cause.WARMING: 45, Cause.CAPACITY_UNAVAILABLE: 120, Cause.CONCURRENCY_LIMIT: 2}
    for cause, seconds in given.items():
        for wire in FORMATS:
            assert render_json(cause, WireFormat(wire), retry_after=given).headers["Retry-After"] == str(seconds)
    # A value for one cause leaves the two others at their defaults, and adds no header elsewhere.
    only_warming = {Cause.WARMING: 45}
    assert render_json(Cause.CAPACITY_UNAVAILABLE, WireFormat.MESSAGES, only_warming).headers["Retry-After"] == "60"
    assert "Retry-After" not in render_json(Cause.NO_SESSION, WireFormat.MESSAGES, only_warming).headers
    # No caller can give the header to one of the six others.
    for name, (_, seconds) in DESIGN.items():
        if seconds is None:
            with pytest.raises(ValueError, match="carries none"):
                render_json(Cause(name), WireFormat.MESSAGES, retry_after={Cause(name): 10})


def test_each_cause_has_its_own_sentence() -> None:
    sentences = [cause.sentence for cause in Cause]
    assert all(sentence.strip() for sentence in sentences)
    assert len(set(sentences)) == len(sentences)


@pytest.mark.parametrize(("name", "wire"), EVERY_CAUSE_IN_EVERY_FORMAT)
def test_a_cause_renders_to_the_json_answer_of_its_format(name: str, wire: str) -> None:
    answer = render_json(Cause(name), WireFormat(wire))

    assert answer.status == DESIGN[name][0]
    assert answer.headers["Content-Type"] == "application/json"
    expected = anthropic_error(name) if wire == "messages" else openai_error(name)
    assert json.loads(answer.body) == expected


@pytest.mark.parametrize(("name", "wire"), EVERY_CAUSE_IN_EVERY_FORMAT)
def test_a_cause_renders_to_the_event_that_ends_a_stream_of_its_format(name: str, wire: str) -> None:
    event = read_event(render_stream_event(Cause(name), WireFormat(wire)))

    if wire == "messages":
        assert event == ("error", anthropic_error(name))
    elif wire == "responses":
        failed = {"object": "response", "status": "failed", "error": {"code": name, "message": Cause(name).sentence}}
        assert event == ("response.failed", {"type": "response.failed", "response": failed})
    else:
        # Chat Completions has no named event: its clients read a chunk that holds `error`.
        assert event == (None, openai_error(name))


def test_an_exception_renders_as_the_cause_it_carries_or_as_engine_error() -> None:
    for cause in Cause:
        assert cause_of(cause) is cause
        assert cause_of(GatewayError(cause)) is cause
    assert cause_of(RuntimeError("anything")) is Cause.ENGINE_ERROR

    for wire in WireFormat:
        assert render_json(GatewayError(Cause.NO_SESSION), wire) == render_json(Cause.NO_SESSION, wire)
        assert render_json(TimeoutError(), wire) == render_json(Cause.ENGINE_ERROR, wire)
        assert render_stream_event(GatewayError(Cause.TIME_LIMIT), wire) == render_stream_event(Cause.TIME_LIMIT, wire)
        assert render_stream_event(KeyError("pool"), wire) == render_stream_event(Cause.ENGINE_ERROR, wire)


def test_no_foreign_text() -> None:
    # Built here, so that the repository holds neither: an engine's address and a task's ARN, with
    # the documentation's account.
    account = "".join(str(digit % 10) for digit in range(1, 13))
    address = ".".join(str(part) for part in (10, 0, 12, 34)) + ":8443"
    arn = ":".join(("arn", "aws", "ecs", "eu-west-1", account, "task/fleet/0f3a9c"))
    message = f"connection to {address} refused while calling {arn}"

    errors: list[BaseException] = [GatewayError(cause, message) for cause in Cause]
    errors += [RuntimeError(message), ConnectionError(address, arn), KeyError(message)]
    try:
        try:
            raise OSError(message)
        except OSError as inner:
            raise GatewayError(Cause.CAPACITY_UNAVAILABLE, message) from inner
    except GatewayError as chained:
        errors.append(chained)

    outputs = []
    for error in errors:
        assert address in repr(error.args)
        assert arn in repr(error.args)
        for wire in WireFormat:
            answer = render_json(error, wire)
            assert answer.status == cause_of(error).status
            outputs += [answer.body.decode("ascii"), repr(answer.headers), str(answer.status)]
            outputs.append(render_stream_event(error, wire).decode("ascii"))

    assert len(outputs) == len(errors) * 3 * 4
    for output in outputs:
        assert address not in output
        assert arn not in output
        assert account not in output
        assert "arn" + ":" not in output
