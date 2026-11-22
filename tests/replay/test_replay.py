"""Every capture of `tests/captures/`, replayed through the gateway (system design, section 6.2).

The gateway's JSON and SSE code decides who pays for what, so each capture is replayed with its
upstream answer cut at byte positions and with its client leaving after each event
(`tests/replay/harness.py`). A capture added to the folder is replayed by these tests as it is.
The captures some tests need beside the folder's are built here, and their keys at run time.
"""

from __future__ import annotations

import copy
import importlib
import itertools
import json
import runpy
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import harness
import scrub
from harness import Capture, Replayed, Settled
from rhi_common.keys import generate_key
from rhi_gateway import profile
from rhi_gateway.errors import WireFormat

CAPTURES = harness.folder()

# A capture built here: a Messages request as a client sends it, with a field of the engine's own
# that the gateway drops, and a whole answer.
BUILT: dict[str, Any] = {
    "request": {
        "method": "POST",
        "path": "/v1/messages",
        "headers": {"host": "gateway.example.com", "content-type": "application/json"},
        "body": json.dumps(
            {
                "model": "claude-of-a-test",
                "max_tokens": 64,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": "Hello."}],
            }
        ),
    },
    "answer": {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello."}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            }
        ),
    },
    "usage": {"input_tokens": 12, "output_tokens": 3},
}
# What the gateway must forward for it: the pool's name for the model, and no field of the engine's own.
BUILT_FORWARD: dict[str, Any] = {
    "model": harness.DEFAULT_POOL.name,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Hello."}],
}


# Two entries of a request profile, written by a test where the gateway looks its entries up: one
# maps the reasoning effort a Messages request sends through the pool's mapping, the other turns
# `xhigh` into `medium`. Applied in one order or the other, they give two bodies.
ENTRY = """
from rhi_gateway import routes
from rhi_gateway.errors import WireFormat
from rhi_gateway.profile import Entry


def rewrite(body, pool, route):
    effort = body.get("output_config", {{}}).get("effort")
    if effort is None:
        return body
    return {{**body, "output_config": {{**body["output_config"], "effort": {effort}}}}}


ENTRY = Entry(
    name={name!r},
    wire_formats=frozenset({{WireFormat.MESSAGES}}),
    routes=frozenset({{routes.MESSAGES, routes.COUNT_TOKENS}}),
    defect="https://example.com/engine/issues/1",
    rewrite=rewrite,
)
"""
ENTRIES = {
    "map_effort": "(pool.reasoning_effort or {}).get(effort, effort)",
    "cap_effort": "{'xhigh': 'medium'}.get(effort, effort)",
}


def built(**parts: Any) -> dict[str, Any]:
    return {**copy.deepcopy(BUILT), **parts}


@pytest.fixture
def entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """The two entries of `ENTRIES`, where the gateway finds its entries, for one test."""
    monkeypatch.setattr(profile, "__path__", [*profile.__path__, str(tmp_path)])
    for name, effort in ENTRIES.items():
        (tmp_path / f"{name}.py").write_text(ENTRY.format(name=name, effort=effort), encoding="utf-8")
    importlib.invalidate_caches()
    yield
    for name in ENTRIES:
        sys.modules.pop(f"{profile.__name__}.{name}", None)
        vars(profile).pop(name, None)


def assert_whole(capture: Capture, replayed: Replayed) -> None:
    """The client has the recorded bytes, and the one reservation is settled once, having observed the usage expected."""
    cut = replayed.cut if len(replayed.cut) <= 1 else f"{len(replayed.cut)} positions"
    where = f"{capture.name}, cut at {cut}"
    assert replayed.status == capture.status, where
    assert replayed.received == capture.answer, where
    assert replayed.reserved == 1, where
    usage = capture.usage
    assert replayed.settled == (Settled(usage.input_tokens, usage.output_tokens, True, True),), where
    # The student's record agrees: no lease is left, and the day's tokens hold nothing charged.
    assert (replayed.leases, replayed.tokens) == (0, 0), where


@pytest.mark.parametrize("capture", CAPTURES, ids=lambda capture: capture.name)
def test_every_cut_gives_the_recorded_answer(capture: Capture) -> None:
    replays = 0
    for replayed in harness.replay_cuts(capture):
        assert_whole(capture, replayed)
        replays += 1
    assert replays == len(harness.cuts(capture.answer)) > 1


def test_a_short_answer_is_cut_at_every_byte_and_a_long_one_at_seeded_positions() -> None:
    short, long = bytes(harness.SHORT_BYTES), bytes(harness.SHORT_BYTES + 1)

    every = tuple(range(1, len(short)))
    assert harness.cuts(short) == [*((position,) for position in every), every]

    *single, together = harness.cuts(long)
    assert harness.cuts(long) == [*single, together]  # Drawn with a fixed seed: the same on every call.
    assert len(set(single)) == harness.SEEDED_CUTS
    assert all(0 < position < len(long) for (position,) in single)
    assert together == tuple(position for (position,) in single)

    answer = bytes(range(10))
    assert harness.split(answer, (3, 4)) == [answer[:3], answer[3:4], answer[4:]]


def test_the_folder_holds_each_format_streamed_and_not() -> None:
    held = {(capture.wire_format, capture.streamed) for capture in CAPTURES}
    assert held == set(itertools.product(WireFormat, (True, False)))


def stream_that_reports_much() -> Capture:
    """A Messages stream whose usage is far above the size of its request, as a long cached transcript's is."""

    def event(name: str, **data: Any) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

    start = {"type": "message", "role": "assistant", "content": []}
    stream = [
        event("message_start", message={**start, "usage": {"input_tokens": 400, "cache_read_input_tokens": 8600}}),
        event("ping"),
        event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        event("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hello."}),
        event("content_block_stop", index=0),
        event("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 40}),
        event("message_stop"),
    ]
    answer = {"status": 200, "headers": {"content-type": "text/event-stream"}, "body": stream}
    return harness.parse("built-stream", built(answer=answer, usage={"input_tokens": 9000, "output_tokens": 40}))


def test_disconnects_settle_once_with_what_was_observed() -> None:
    streamed = [capture for capture in CAPTURES if capture.streamed]
    assert streamed
    for capture in (*streamed, stream_that_reports_much()):
        events = harness.events(capture.answer)
        # The harness's own reading of a stream's usage, which the settlements below are held to: of
        # the whole stream, it is the usage the capture expects.
        assert harness.reported(b"".join(events)) == capture.usage
        replays = list(harness.replay_disconnects(capture))
        assert [replayed.left_after for replayed in replays] == list(range(1, len(events) + 1))
        for after, replayed in enumerate(replays, start=1):
            where = f"{capture.name}, the client leaving after event {after}"
            # The client took these events and no more, and the gateway was handed no more either.
            assert replayed.received == b"".join(events[:after]), where
            assert replayed.forwarded is not None, where
            assert replayed.reserved == 1, where
            # The tokens observed are those the events handed over report, and nothing is charged.
            observed = replayed.handed
            assert observed == harness.reported(b"".join(events[:after])), where
            assert replayed.settled == (Settled(observed.input_tokens, observed.output_tokens, False, True),), where
            assert (replayed.leases, replayed.tokens) == (0, 0), where


def test_the_forwarded_body_is_checked(entries: None) -> None:
    def replay(forward: dict[str, Any], **parts: Any) -> Replayed:
        capture = harness.parse("built", built(forward=forward, **parts))
        replayed = harness.replay(capture)
        assert_whole(capture, replayed)
        return replayed

    replay(BUILT_FORWARD)
    # The request ID and the cache salt are the gateway's own for each request: whatever they hold passes.
    replay({**BUILT_FORWARD, "request_id": "of-another-request", "cache_salt": "of-another-student"})

    one_field_off = {
        "max_tokens": {**BUILT_FORWARD, "max_tokens": 65},
        # A field the gateway does not forward, and one it forwards, left out.
        "chat_template_kwargs": {**BUILT_FORWARD, "chat_template_kwargs": {"enable_thinking": False}},
        "messages": {name: value for name, value in BUILT_FORWARD.items() if name != "messages"},
    }
    for field, forward in one_field_off.items():
        with pytest.raises(harness.ForwardedBodyDiffers, match=f"differs from the capture's in {field}$"):
            replay(forward)

    # A capture that states a profile is replayed for a pool with it: its request sends effort
    # `high`, which the profile maps to `xhigh`, and its stated body must say what the engine receives.
    request = copy.deepcopy(BUILT["request"])
    request["body"] = json.dumps({**json.loads(request["body"]), "output_config": {"effort": "high"}})

    def effort(value: str) -> dict[str, Any]:
        return {**BUILT_FORWARD, "output_config": {"effort": value}}

    def stated(*names: str) -> dict[str, Any]:
        return {"request_profile": list(names), "reasoning_effort": {"high": "xhigh"}}

    replayed = replay(effort("xhigh"), request=request, profile=stated("map_effort"))
    assert replayed.forwarded is not None
    sent = {name: value for name, value in json.loads(replayed.forwarded).items() if name not in harness.LEFT_OUT}
    assert sent == effort("xhigh")
    # The body the gateway forwards with no profile is not what the engine receives with this one.
    with pytest.raises(harness.ForwardedBodyDiffers, match="differs from the capture's in output_config$"):
        replay(effort("high"), request=request, profile=stated("map_effort"))

    # The entries run in the order the profile states them: high, xhigh, medium; or high, high, xhigh.
    replay(effort("medium"), request=request, profile=stated("map_effort", "cap_effort"))
    replay(effort("xhigh"), request=request, profile=stated("cap_effort", "map_effort"))
    with pytest.raises(harness.ForwardedBodyDiffers, match="differs from the capture's in output_config$"):
        replay(effort("medium"), request=request, profile=stated("cap_effort", "map_effort"))

    # A capture that states a profile states the engine-side body too.
    with pytest.raises(ValueError, match="states a profile and not the body"):
        harness.parse("built", built(request=request, profile=stated("map_effort")))


def test_captures_in_subdirectories_are_replayed(tmp_path: Path) -> None:
    for place in ("one", "two/deeper"):
        (tmp_path / place).mkdir(parents=True)
        (tmp_path / place / "built.json").write_text(json.dumps(built()), encoding="utf-8")

    assert [capture.name for capture in harness.folder(tmp_path)] == ["one/built", "two/deeper/built"]


def test_the_scrubber_refuses() -> None:
    scrub.check(built())

    with_a_key, foreign_host, with_an_address = built(), built(), built()
    with_a_key["request"]["headers"]["x-api-key"] = generate_key()
    foreign_host["request"]["headers"]["host"] = "gateway.example.net"
    with_an_address["request"]["headers"]["x-forwarded-for"] = "192.0.2.7"

    for rule, capture in ((scrub.KEY, with_a_key), (scrub.HOST, foreign_host), (scrub.ADDRESS, with_an_address)):
        with pytest.raises(scrub.Refused) as refused:
            scrub.check(capture)
        assert refused.value.rule == rule


def test_a_capture_the_scrubber_refuses_is_not_replayed(tmp_path: Path) -> None:
    capture = built()
    capture["request"]["body"] = json.dumps(
        {"model": "mock", "messages": [{"role": "user", "content": generate_key()}]}
    )
    (tmp_path / "leaking.json").write_text(json.dumps(capture), encoding="utf-8")

    with pytest.raises(scrub.Refused):
        harness.folder(tmp_path)


def test_a_fresh_recording_of_the_mock_engine_replays() -> None:
    record = runpy.run_path(str(Path(harness.__file__).with_name("record.py")))["record"]

    recorded = [harness.parse(name, value) for name, value in record().items()]

    assert {(capture.wire_format, capture.streamed) for capture in recorded} == set(
        itertools.product(WireFormat, (True, False))
    )
    for capture in recorded:
        assert (harness.CAPTURES / f"{capture.name}.json").is_file()
        assert capture.forward is not None
        assert_whole(capture, harness.replay(capture))
