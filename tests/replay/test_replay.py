"""Every capture of `tests/captures/`, replayed through the gateway (system design, section 6.2).

The gateway's JSON and SSE code decides who pays for what, so each capture is replayed with its
upstream answer cut at byte positions and with its client leaving after each event
(`tests/replay/harness.py`). A capture added to the folder is replayed by these tests as it is.
The captures some tests need beside the folder's are built here, and their keys at run time.
"""

from __future__ import annotations

import copy
import itertools
import json
import runpy
from pathlib import Path
from typing import Any

import pytest

import harness
import scrub
from harness import Capture, Replayed, Settled
from rhi_common.keys import generate_key
from rhi_gateway.budget import estimate_input_tokens
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


def built(**parts: Any) -> dict[str, Any]:
    return {**copy.deepcopy(BUILT), **parts}


def assert_whole(capture: Capture, replayed: Replayed) -> None:
    """The client has the recorded bytes, and the one reservation is settled once, to the usage expected."""
    cut = replayed.cut if len(replayed.cut) <= 1 else f"{len(replayed.cut)} positions"
    where = f"{capture.name}, cut at {cut}"
    assert replayed.status == capture.status, where
    assert replayed.received == capture.answer, where
    assert len(replayed.estimates) == 1, where
    usage = capture.usage
    assert replayed.settled == (Settled(usage.input_tokens, usage.output_tokens, True, usage.total),), where
    # The student's record agrees: no lease is left, and the day holds the usage and nothing reserved.
    assert (replayed.leases, replayed.charged) == (0, usage.total), where


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
    """A Messages stream whose usage is far above the estimate of its request, as a long cached transcript's is."""

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


def test_disconnects_are_charged() -> None:
    streamed = [capture for capture in CAPTURES if capture.streamed]
    assert streamed
    above = below = 0
    for capture in (*streamed, stream_that_reports_much()):
        events = harness.events(capture.answer)
        # The harness's own reading of a stream's usage, which the charges below are held to: of
        # the whole stream, it is the usage the capture expects.
        assert harness.reported(b"".join(events)) == capture.usage
        replays = list(harness.replay_disconnects(capture))
        assert [replayed.left_after for replayed in replays] == list(range(1, len(events) + 1))
        for after, replayed in enumerate(replays, start=1):
            where = f"{capture.name}, the client leaving after event {after}"
            # The client took these events and no more, and the gateway was handed no more either.
            assert replayed.received == b"".join(events[:after]), where
            assert replayed.forwarded is not None, where
            (estimate,) = replayed.estimates
            assert estimate == estimate_input_tokens(replayed.forwarded), where
            # The tokens observed are those the events handed over report; never less than the estimate.
            observed = replayed.handed
            assert observed == harness.reported(b"".join(events[:after])), where
            charge = max(observed.total, estimate)
            assert replayed.settled == (Settled(observed.input_tokens, observed.output_tokens, False, charge),), where
            assert (replayed.leases, replayed.charged) == (0, charge), where
            above += observed.total > estimate
            below += observed.total < estimate
    # Both sides of the rule were met: disconnects charged what was observed, and others the estimate.
    assert above and below


def test_the_forwarded_body_is_checked() -> None:
    def replay(forward: dict[str, Any]) -> Replayed:
        capture = harness.parse("built", built(forward=forward))
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
