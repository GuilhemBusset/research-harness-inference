"""The conversations a replay sends, frozen before any of them is sent
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol"): what a trace holds,
that no answer changes it, and what a seed, a repetition and a conversation each change of it.

Every answer here is made up, and nothing is sent anywhere: a replay runs against a transport of
the test.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

import loadgen
import s5_capacity
import scrub
from evaluate import plan, traces
from rhi_common.keys import generate_key

REPO = Path(__file__).resolve().parents[3]
MODEL = "coursework"
SEEDS = (1001, 7, 2**32 - 1)
COURSE = traces.read_course()
# The capture of a course harness that documents the tool the fixture scripts.
DOCUMENTED = json.loads((REPO / json.loads(traces.COURSE.read_text(encoding="utf-8"))["source"]).read_text("utf-8"))
# What the answers of a replay are varied over: the text an answer streams, and the tool it calls.
ANSWERS: dict[str, tuple[str, str | None]] = {
    "no text": ("", None),
    "a short text": ("made-up answer", None),
    "a long text": ("made-up words of a long answer " * 400, None),
    "another tool call": ("made-up answer", "Write"),
}


def bodies(trace: traces.Trace) -> list[dict[str, Any]]:
    return [json.loads(exchange.request.body) for exchange in trace.exchanges]


def tool_turns(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The calls and the results of a request, in order."""
    blocks = [block for message in body["messages"] for block in message["content"]]
    return [block for block in blocks if block["type"] in ("tool_use", "tool_result")]


def drawn(body: dict[str, Any]) -> int:
    """The tokens of a request as the generator counts them: the words of the text it drew, which
    is the opening and every text of the messages but the fixture's own."""
    texts = [body["system"][1]["text"]]
    for message in body["messages"]:
        for block in message["content"]:
            inner = block["content"][len(COURSE.result["content"]) :] if block["type"] == "tool_result" else [block]
            texts += [one["text"] for one in inner if one["type"] == "text"]
    return sum(len(text.split()) for text in texts)


def event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name} | data)}\n\n".encode()


def answer_stream(text: str, tool: str | None, input_tokens: int) -> bytes:
    """A Messages stream that answers with `text`, then calls `tool` if one is given, and reports its usage."""
    events = [event("message_start", {"message": {"usage": {"input_tokens": input_tokens, "output_tokens": 1}}})]
    if text:
        events.append(event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}))
    if tool:
        block = {"type": "tool_use", "id": "toolu_made_up", "name": tool, "input": {}}
        delta = {"type": "input_json_delta", "partial_json": '{"file_path": "other.txt"}'}
        events += [
            event("content_block_start", {"index": 1, "content_block": block}),
            event("content_block_delta", {"index": 1, "delta": delta}),
        ]
    events += [event("message_delta", {"usage": {"output_tokens": len(text.split())}}), event("message_stop", {})]
    return b"".join(events)


def replayed(session: loadgen.Session, text: str, tool: str | None) -> tuple[list[bytes], list[float], list[Any]]:
    """What a replay of the session sends when every request is answered with `text` and a call
    of `tool`: the body of each request, the pauses it waited, and its records."""
    sent: list[bytes] = []
    pauses: list[float] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        stream = answer_stream(text, tool, input_tokens=len(request.content) // 4)
        return httpx.Response(200, headers={"content-type": loadgen.STREAM_TYPE}, content=stream)

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    records = asyncio.run(
        loadgen.replay(
            loadgen.Load((session,)),
            [generate_key()],
            "http://gateway.example.com",
            at_once=1,
            sleep=sleep,
            transport=httpx.MockTransport(answer),
        )
    )
    return sent, pauses, records


def hygiene(folder: Path, *paths: str | Path) -> subprocess.CompletedProcess[str]:
    """The repository's hygiene check, as `scripts/check.sh hygiene` runs it, on these files."""
    scanner = [sys.executable, str(REPO / "tools" / "hygiene" / "scan.py"), "--paths", *map(str, paths)]
    return subprocess.run(scanner, cwd=folder, capture_output=True, text=True, check=False)


def test_a_trace_is_a_recorded_session_of_messages_with_the_documented_tool_turns() -> None:
    trace = traces.build(MODEL, SEEDS[0], 1)
    text = trace.recorded()

    # The recorded-session reader gives back the requests and the pauses, and so their hash.
    session = loadgen.read_recorded(text)
    assert session.exchanges == trace.exchanges == trace.session().exchanges
    assert traces.trace_hash(session.exchanges) == trace.trace_hash
    assert traces.build(MODEL, SEEDS[0], 1).recorded() == text

    # Scrubbed: no host and no key travel with a request, and the capture scrubber refuses none.
    for held in json.loads(text)["exchanges"]:
        scrub.check(held["capture"])
    for exchange in trace.exchanges:
        request = exchange.request
        assert (request.method, request.path) == ("POST", loadgen.MESSAGES_PATH)
        assert set(request.headers) == {"content-type", "anthropic-version"}

    # The tool is the one the course capture documents: its definition, its call and its result.
    documented = json.loads(DOCUMENTED["request"]["body"])
    call, result = documented["messages"][1]["content"][1], documented["messages"][2]["content"][0]
    assert list(COURSE.tools) == documented["tools"]
    assert (COURSE.call, COURSE.result["content"]) == (call, result["content"][:1])
    assert COURSE.result["tool_use_id"] == call["id"] == traces.TOOL_ID.format(1)

    defined = {tool["name"]: tool["input_schema"] for tool in COURSE.tools}
    for turn, body in enumerate(bodies(trace)):
        assert body["stream"] is True
        assert body["tools"] == documented["tools"]
        assert [message["role"] for message in body["messages"]] == ["user", *["assistant", "user"] * turn]
        # Every call is of a defined tool with the input it requires, and the next turn answers it.
        turns = tool_turns(body)
        calls, results = turns[0::2], turns[1::2]
        assert len(calls) == len(results) == turn
        assert len({one["id"] for one in calls}) == turn
        for one, answered in zip(calls, results, strict=True):
            assert (one["type"], answered["type"]) == ("tool_use", "tool_result")
            assert set(defined[one["name"]]["required"]) <= set(one["input"])
            assert answered["tool_use_id"] == one["id"]
        for message in body["messages"][1::2]:
            assert [block["type"] for block in message["content"]] == ["text", "tool_use"]


@pytest.mark.parametrize("seed", SEEDS)
def test_the_sizes_are_those_of_spike_s5_and_are_recorded_as_requested(seed: int) -> None:
    trace = traces.build(MODEL, seed, 0)
    requested = trace.requested
    sent = bodies(trace)

    assert (traces.OPENING_TOKENS, traces.TURNS) == (s5_capacity.OPENING_TOKENS, s5_capacity.TURNS_PER_SESSION)
    assert len(sent) == len(requested) == 1 + s5_capacity.TURNS_PER_SESSION
    low, high = loadgen.TRANSCRIPT_TOKENS
    assert low <= requested[0].transcript_tokens <= high
    assert requested[0].new_input_tokens == requested[0].transcript_tokens - s5_capacity.OPENING_TOKENS
    assert (requested[0].pause_ms, requested[0].scripted_output_tokens) == (0, 0)
    for before, later in pairwise(requested):
        assert loadgen.TURN_INPUT_TOKENS[0] <= later.new_input_tokens <= loadgen.TURN_INPUT_TOKENS[1]
        assert loadgen.PAUSE_SECONDS[0] * 1000 <= later.pause_ms <= loadgen.PAUSE_SECONDS[1] * 1000
        assert later.scripted_output_tokens == loadgen.TURN_OUTPUT_TOKENS
        assert (
            later.transcript_tokens == before.transcript_tokens + later.scripted_output_tokens + later.new_input_tokens
        )
    # A turn does not always add the same input or wait the same time.
    assert len({one.new_input_tokens for one in requested[1:]}) > 1
    assert len({one.pause_ms for one in requested[1:]}) > 1

    # What is recorded as requested is what the bytes hold, and what a replay waits.
    for one, body, exchange in zip(requested, sent, trace.exchanges, strict=True):
        assert one.transcript_tokens == drawn(body)
        assert one.max_tokens == body["max_tokens"] == loadgen.TURN_OUTPUT_TOKENS
        assert one.body_bytes == len(exchange.request.body)
        assert one.pause_ms == round(exchange.pause_seconds * 1000)
        assert (body["thinking"], body["output_config"]) == (
            s5_capacity.CLAUDE_CODE_THINKING,
            {"effort": s5_capacity.CLAUDE_CODE_EFFORT},
        )

    # The suite states the same profile for its replay.
    replay = plan.load_suite().protocol["replay"]
    assert traces.S5 == traces.Sizes(
        opening_tokens=replay["opening_tokens"],
        transcript_tokens=tuple(replay["transcript_tokens"]),
        turn_input_tokens=tuple(replay["turn_input_tokens"]),
        turn_output_tokens=replay["turn_output_tokens"],
        tool_delay_ms=tuple(replay["tool_delay_ms"]),
    )


def test_no_answer_changes_a_later_request_a_tool_turn_or_a_pause() -> None:
    trace = traces.build(MODEL, SEEDS[0], 1, sizes=traces.Sizes(turns=4))
    session = loadgen.read_recorded(trace.recorded())
    frozen = [exchange.request.body for exchange in trace.exchanges]
    planned = [exchange.pause_seconds for exchange in trace.exchanges[1:]]
    record = trace.record()

    observed = set()
    for text, tool in ANSWERS.values():
        sent, pauses, records = replayed(session, text, tool)
        # Each request went out as it was frozen, after the pause that was frozen, whatever came back.
        assert sent == frozen
        assert pauses == planned
        assert [tool_turns(json.loads(body)) for body in sent] == [tool_turns(body) for body in bodies(trace)]
        observed.add(tuple((one.input_tokens, one.output_tokens) for one in records))
    assert len(frozen) == 5
    assert all(tool_turns(body) for body in bodies(trace)[1:])

    # The usage that was observed differs with the answers. What was requested is one record,
    # made before any answer, and holds no count an answer reports.
    assert len(observed) > 1
    assert trace.record() == record == traces.build(MODEL, SEEDS[0], 1, sizes=traces.Sizes(turns=4)).record()
    assert record["trace_hash"] == trace.trace_hash
    assert [one["body_bytes"] for one in record["requests"]] == [len(body) for body in frozen]
    assert "usage" not in json.dumps(record)
    assert set(record["requests"][0]) == {
        "turn", "pause_ms", "transcript_tokens", "scripted_output_tokens", "new_input_tokens", "max_tokens",
        "body_bytes",
    }  # fmt: skip

    # The same answers do change what spike S5's synthetic session sends next: its seed freezes nothing.
    synthetic = loadgen.agent_session(MODEL, 2, SEEDS[0])
    later = {tuple(replayed(synthetic, text, tool)[0][1:]) for text, tool in ANSWERS.values()}
    assert len(later) > 1


def test_each_request_is_the_one_before_it_then_two_new_turns() -> None:
    trace = traces.build(MODEL, SEEDS[1], 2)

    for (before, sent_before), (later, sent_later) in pairwise(zip(bodies(trace), trace.exchanges, strict=True)):
        assert (later["system"], later["tools"]) == (before["system"], before["tools"])
        held = len(before["messages"])
        assert later["messages"][:held] == before["messages"]
        assert [message["role"] for message in later["messages"][held:]] == ["assistant", "user"]
        # In bytes too: the request before, as far as its last message, opens the next one.
        raw = sent_before.request.body
        assert sent_later.request.body.startswith(raw[: raw.rindex(b'], "thinking"')])
        assert len(sent_later.request.body) > len(raw)


def test_every_conversation_starts_with_the_opening_of_spike_s5() -> None:
    built = [traces.build(MODEL, seed, namespace, number) for seed, namespace, number in ((1001, 0, 0), (7, 3, 5))]

    for trace in built:
        for body in bodies(trace):
            namespace, opening = body["system"]
            assert opening == {"type": "text", "text": s5_capacity.opening()}
            assert namespace["text"] == traces.namespace_text(trace.seed, trace.namespace)
    assert len(s5_capacity.opening().split()) == s5_capacity.OPENING_TOKENS
    # After the opening the two conversations are their own.
    first, second = (bodies(trace)[0]["messages"] for trace in built)
    assert first != second


def test_a_repetition_has_a_prefix_namespace_of_its_own_and_the_same_bytes_in_every_treatment() -> None:
    seed = SEEDS[0]
    one, other = traces.build(MODEL, seed, 1), traces.build(MODEL, seed, 2)

    # Another namespace: the prompts part ways inside the namespace, before anything a class reuses.
    assert bodies(one)[0]["system"][0] != bodies(other)[0]["system"][0]
    shared = len(os.path.commonprefix([one.prefix, other.prefix]))
    assert shared < one.prefix.index(s5_capacity.opening().encode()[:64])
    assert len({traces.namespace_text(seed, number) for seed in SEEDS for number in range(64)}) == 64 * len(SEEDS)
    # Everything after the namespace is the same content: only the prefix is fresh.
    for fresh, again in zip(bodies(one), bodies(other), strict=True):
        assert (fresh["system"][1:], fresh["tools"], fresh["messages"]) == (
            again["system"][1:],
            again["tools"],
            again["messages"],
        )
    assert one.trace_hash != other.trace_hash

    # Inside a repetition, every conversation shares the prefix and has its own transcript. The
    # prefix is in every request as it is written: the system prompt, then the tools.
    neighbour = traces.build(MODEL, seed, 1, 1)
    assert neighbour.prefix == one.prefix
    assert all(
        exchange.request.body.count(one.prefix[1:-1]) == 1 for exchange in (*one.exchanges, *neighbour.exchanges)
    )
    assert bodies(neighbour)[0]["messages"] != bodies(one)[0]["messages"]

    # Another treatment that generates the same seed and namespace is sent the same bytes.
    paired = traces.build(MODEL, seed, 1)
    assert paired.prefix == one.prefix
    assert paired.exchanges == one.exchanges
    assert paired.record() == one.record()
    assert traces.build("another-pool", seed, 1).prefix == one.prefix


def test_another_seed_is_another_trace() -> None:
    one, other = traces.build(MODEL, SEEDS[0], 1), traces.build(MODEL, SEEDS[1], 1)

    assert one.trace_hash != other.trace_hash
    assert one.trace_hash == traces.build(MODEL, SEEDS[0], 1).trace_hash
    assert [one.pause_ms for one in one.requested] != [one.pause_ms for one in other.requested]
    assert [one.new_input_tokens for one in one.requested] != [one.new_input_tokens for one in other.requested]
    for first, second in zip(bodies(one), bodies(other), strict=True):
        assert first["messages"][0] != second["messages"][0]
        assert first["messages"][-1] != second["messages"][-1]
        assert first["system"][0] != second["system"][0]


def test_generated_traces_pass_the_repository_hygiene_check(tmp_path: Path) -> None:
    for seed, namespace, conversation in ((SEEDS[0], 0, 0), (SEEDS[0], 0, 1), (SEEDS[1], 3, 0)):
        trace = traces.build(MODEL, seed, namespace, conversation)
        name = f"{seed}-{namespace}-{conversation}.json"
        (tmp_path / f"trace-{name}").write_text(trace.recorded(), encoding="utf-8")
        (tmp_path / f"record-{name}").write_text(json.dumps(trace.record()), encoding="utf-8")

    # The traces as they are generated, then the fixture they are generated from, as it is committed.
    for clean in (hygiene(tmp_path, "."), hygiene(REPO, traces.COURSE.parent.relative_to(REPO))):
        assert clean.returncode == 0, clean.stdout

    # The same check refuses a trace that carries a student's key.
    leaking = tmp_path / "leaking"
    leaking.mkdir()
    (leaking / "trace.json").write_text(traces.build(generate_key(), 1, 0).recorded(), encoding="utf-8")
    assert hygiene(leaking, ".").returncode == 1


def test_an_opening_longer_than_the_transcript_is_refused() -> None:
    with pytest.raises(ValueError, match="opening"):
        traces.Sizes(opening_tokens=loadgen.TRANSCRIPT_TOKENS[0] + 1)
