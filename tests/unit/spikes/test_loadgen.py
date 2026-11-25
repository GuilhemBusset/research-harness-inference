"""The load generator (`spikes/loadgen.py`), against the local stack the smoke tests run on
(`tests/smoke/conftest.py`): the gateway served on a loopback port, the mock engine in process as
its pool. Every student is enrolled with a key drawn while the tests run, and no time here is a
measurement: the mock computes nothing.

The local stack's hop to its engine is in process and hands the gateway each answer whole, so a
stream's times are checked apart, on the mock's recorded streams paced by a clock of the test.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import harness
import loadgen
from rhi_common.keys import generate_key
from rhi_common.students import SPIKE_GITHUB_ID_BASE
from rhi_gateway import proxy
from rhi_gateway.sse import SseReader
from rhi_mock_engine.app import ANSWER, REASONING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator

REPO = Path(__file__).resolve().parents[3]
STUDENTS = 8
# What the mock engine answers a request that declares no tool: its reasoning, then its text.
MOCK_OUTPUT_TOKENS = len(REASONING) + len(ANSWER)


def _local_stack() -> Any:
    """The module of the smoke tests' stack, which is in no folder of the import path."""
    spec = importlib.util.spec_from_file_location("smoke_stack", REPO / "tests" / "smoke" / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while it is run.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _local_stack()


@pytest.fixture(scope="module")
def stack() -> Generator[tuple[str, list[str]]]:
    """The local stack's address, and the keys of the students enrolled in it for these tests."""
    with smoke.local_stack(smoke.MOCK_POOL) as (address, _):
        # Spike students, served whether or not a session is on (section 5.6), as the spikes' own are.
        yield address, [
            smoke.enrol(github_id=SPIKE_GITHUB_ID_BASE + 1 + number, alias=f"canary-{number}")
            for number in range(STUDENTS)
        ]


class Clock:
    """A clock that only its own `sleep` moves, by what it is asked to wait."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class Noting(httpx.AsyncBaseTransport):
    """Sends each request as httpx does, and notes it first with the time on the clock given."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sending = httpx.AsyncHTTPTransport()
        self.sent: list[tuple[float, httpx.Request]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((self._clock(), request))
        return await self._sending.handle_async_request(request)

    async def aclose(self) -> None:
        await self._sending.aclose()

    def bodies(self, key: str) -> list[dict[str, Any]]:
        """The bodies of the requests sent with a student's key, in the order they were sent."""
        return [
            json.loads(request.content)
            for _, request in self.sent
            if request.headers["authorization"] == f"Bearer {key}"
        ]


class Paced(httpx.AsyncBaseTransport):
    """Answers each request with one stream, an event at a time, `seconds` on the clock before each."""

    def __init__(self, stream: bytes, clock: Clock, seconds: float) -> None:
        self._events = stream.split(b"\n\n")
        self._clock = clock
        self._seconds = seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async def events() -> AsyncIterator[bytes]:
            for event in self._events:
                await self._clock.sleep(self._seconds)
                yield event + b"\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=events())


def session(seed: int, *turns: loadgen.Turn, start_tokens: int = 200) -> loadgen.SyntheticSession:
    return loadgen.SyntheticSession(
        model=smoke.MOCK_POOL, start_tokens=start_tokens, turns=turns, output_tokens=500, seed=seed
    )


def test_eight_synthetic_sessions_are_eight_students_whose_transcripts_grow(stack: tuple[str, list[str]]) -> None:
    address, keys = stack
    turns = (loadgen.Turn(pause_seconds=7.5, input_tokens=40), loadgen.Turn(pause_seconds=5.0, input_tokens=60))
    sessions = tuple(session(number, *turns, start_tokens=200 + 10 * number) for number in range(STUDENTS))
    noting, pauses = Noting(), []

    async def no_wait(seconds: float) -> None:
        pauses.append(seconds)

    records = asyncio.run(
        loadgen.replay(loadgen.Load(sessions), keys, address, at_once=STUDENTS, sleep=no_wait, transport=noting)
    )

    assert sorted({record.student for record in records}) == list(range(STUDENTS))
    # The eight ran at once: each had sent its first request before any had an answer to grow by.
    assert {request.headers["authorization"] for _, request in noting.sent[:STUDENTS]} == {
        f"Bearer {key}" for key in keys
    }
    # Each session paused as it says, before each turn after its first.
    assert sorted(pauses) == sorted([7.5, 5.0] * STUDENTS)
    for student, own in enumerate(sessions):
        first, second, third = (record for record in records if record.student == student)
        assert [record.first_turn for record in (first, second, third)] == [True, False, False]

        # The second request is the first one's transcript, the answer it got and the turn's new input.
        opening, grown, _ = (body["messages"] for body in noting.bodies(keys[student]))
        assert grown[: len(opening)] == opening
        answer, added = grown[len(opening) :]
        assert answer == {"role": "assistant", "content": "".join(ANSWER)}
        assert (added["role"], len(added["content"].split())) == ("user", 40)

        # What is recorded of each request. The mock counts a word of the transcript as a token.
        for record in (first, second, third):
            assert record.status == 200
            assert record.first_token_seconds is not None and record.first_answer_seconds is not None
            assert 0 < record.first_token_seconds <= record.first_answer_seconds
            assert record.output_tokens == MOCK_OUTPUT_TOKENS
        assert first.input_tokens is not None and 0 <= first.input_tokens - own.start_tokens < 16
        # The engine had nothing of a first turn, and all of it at the next: the student is one student.
        assert (first.cached_input_tokens, second.cached_input_tokens) == (0, first.input_tokens)


def capture(path: str, body: dict[str, Any], *, lines: bool = False) -> dict[str, Any]:
    """A capture, as `tests/captures/README.md` describes one. The replay reads its request, so the
    answer and the usage are left empty here."""
    text = json.dumps(body)
    request = {
        "method": "POST",
        "path": path,
        "headers": {"host": "gateway.example.com", "content-type": "application/json"},
        "body": [text[:20], text[20:]] if lines else text,
    }
    return {"request": request, "answer": {"status": 200, "headers": {}, "body": ""}, "usage": {}}


def test_a_recorded_session_replays_in_order(stack: tuple[str, list[str]]) -> None:
    address, keys = stack
    said = [{"role": "user", "content": "Say hello."}]
    # One request in each wire format: streamed Messages and Chat Completions, and a whole Responses answer.
    exchanges = [
        (0, "/v1/messages?beta=true", {"model": "mock", "max_tokens": 64, "stream": True, "messages": said}),
        (7.5, "/v1/chat/completions", {"model": "mock", "stream": True, "stream_options": {"include_usage": True}, "messages": said}),
        (12.25, "/v1/responses", {"model": "mock", "store": False, "input": said}),
    ]  # fmt: skip
    recording = json.dumps(
        {
            "exchanges": [
                {"pause_seconds": pause, "capture": capture(path, body, lines=pause == 0)}
                for pause, path, body in exchanges
            ]
        }
    )
    clock = Clock()
    noting = Noting(clock)

    records = asyncio.run(
        loadgen.replay(
            loadgen.Load((loadgen.read_recorded(recording),)),
            keys,
            address,
            at_once=1,
            clock=clock,
            sleep=clock.sleep,
            transport=noting,
        )
    )

    # The requests, in their recorded order, each sent when the pause recorded before it had passed.
    assert [(request.url.raw_path.decode(), json.loads(request.content)) for _, request in noting.sent] == [
        (path, body) for _, path, body in exchanges
    ]
    assert [later - earlier for (earlier, _), (later, _) in pairwise(noting.sent)] == [7.5, 12.25]
    # They went to the stack with the student's key, not to the recording's host, and each answer was read.
    assert all(
        request.url.host == "127.0.0.1" == request.headers["host"].partition(":")[0] for _, request in noting.sent
    )
    assert [(record.status, record.first_turn) for record in records] == [(200, True), (200, False), (200, False)]
    for record in records:
        assert record.output_tokens == MOCK_OUTPUT_TOKENS
        assert record.input_tokens and record.cached_input_tokens is not None


def test_a_recording_of_another_shape_is_refused_without_repeating_it() -> None:
    held = {"exchanges": [{"pause_seconds": 1, "capture": {"request": {"method": "POST", "path": "/v1/messages"}}}]}
    with pytest.raises(ValueError, match="not a recorded session") as refused:
        loadgen.read_recorded(json.dumps(held))
    assert "/v1/messages" not in str(refused.value)


def test_a_students_sessions_share_one_key_and_a_load_needs_a_key_for_each_student(
    stack: tuple[str, list[str]],
) -> None:
    address, keys = stack
    two = (session(1), session(2))
    noting = Noting()

    records = asyncio.run(
        loadgen.replay(loadgen.Load(two, sessions_per_student=2), keys[:1], address, at_once=2, transport=noting)
    )
    assert [(record.student, record.session) for record in records] == [(0, 0), (0, 1)]
    assert len(noting.bodies(keys[0])) == 2

    # Two sessions of two students, and one key: nothing is sent.
    unsent = Noting()
    with pytest.raises(ValueError, match="more students"):
        asyncio.run(loadgen.replay(loadgen.Load(two), keys[:1], address, at_once=2, transport=unsent))
    assert unsent.sent == []


def test_no_more_sessions_run_at_once_than_asked() -> None:
    class Counting(httpx.AsyncBaseTransport):
        running = most = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.running += 1
            self.most = max(self.most, self.running)
            await asyncio.sleep(0.01)
            self.running -= 1
            return httpx.Response(200, json={})

    counting = Counting()
    load = loadgen.Load(tuple(session(number) for number in range(5)))
    keys = [generate_key() for _ in load.sessions]

    records = asyncio.run(loadgen.replay(load, keys, "http://gateway.example.com", at_once=2, transport=counting))

    assert (len(records), counting.most) == (5, 2)


def test_a_session_ends_at_the_request_that_is_refused(stack: tuple[str, list[str]]) -> None:
    address, _ = stack
    turn = loadgen.Turn(pause_seconds=0, input_tokens=10)

    # A well-formed key that no student holds.
    records = asyncio.run(loadgen.replay(loadgen.Load((session(1, turn),)), [generate_key()], address, at_once=1))

    (refused,) = records
    assert (refused.status, refused.first_turn) == (401, True)
    assert (refused.first_token_seconds, refused.output_tokens_per_second, refused.input_tokens) == (None, None, None)


def test_the_two_shapes_have_the_sizes_of_the_estimate() -> None:
    hands_on = loadgen.hands_on("coursework", 20, turns=6, seed=3)
    several = loadgen.several_agents("coursework", 4, turns=6, seed=3)

    # One session a student, and two a student.
    assert (hands_on.students, len(hands_on.sessions), hands_on.sessions_per_student) == (20, 20, 1)
    assert (several.students, len(several.sessions), several.sessions_per_student) == (4, 8, 2)
    for one in (*hands_on.sessions, *several.sessions):
        assert isinstance(one, loadgen.SyntheticSession)
        assert 50_000 <= one.start_tokens <= 100_000
        assert one.output_tokens == 500
        assert len(one.turns) == 6
        assert all(1_000 <= turn.input_tokens <= 5_000 and 5 <= turn.pause_seconds <= 10 for turn in one.turns)
    # Drawn from the seed: the sessions differ, and the same seed gives the same ones.
    assert len(set(hands_on.sessions)) == 20
    assert hands_on == loadgen.hands_on("coursework", 20, turns=6, seed=3)


@pytest.mark.parametrize("name", ["mock-messages-stream", "mock-chat-completions-stream", "mock-responses-stream"])
def test_a_stream_is_timed_and_counted_as_the_gateway_does(name: str) -> None:
    recorded = harness.load(harness.CAPTURES / f"{name}.json")
    request = loadgen.Request(recorded.method, recorded.target, recorded.headers, recorded.body)
    clock = Clock()

    (record,) = asyncio.run(
        loadgen.replay(
            loadgen.Load((loadgen.RecordedSession((loadgen.Exchange(0, request),)),)),
            [generate_key()],
            "http://gateway.example.com",
            at_once=1,
            clock=clock,
            transport=Paced(recorded.answer, clock, seconds=0.25),
        )
    )

    # The events the gateway counts as tokens, and as the answer's: event `n` arrives at 0.25 (n + 1) seconds.
    carried = [proxy.output_of(recorded.wire_format, event) for event in SseReader().feed(recorded.answer)]
    tokens = [at for at, output in enumerate(carried) if output is not None]
    answered = carried.index(proxy.Output.ANSWER)
    assert tokens[0] < answered
    assert record.first_token_seconds == 0.25 * (tokens[0] + 1)
    assert record.first_answer_seconds == 0.25 * (answered + 1)
    assert record.output_tokens_per_second == (recorded.usage.output_tokens - 1) / (0.25 * (tokens[-1] - tokens[0]))
    assert (record.input_tokens, record.output_tokens) == (recorded.usage.input_tokens, recorded.usage.output_tokens)


def test_a_failed_connection_ends_the_replay_unless_failures_are_kept() -> None:
    keys = [generate_key(), generate_key()]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {keys[0]}":
            raise httpx.ConnectError("refused")
        return httpx.Response(
            503, json={"type": "error", "error": {"type": "no_session", "message": "no session"}}
        )

    sessions = (session(1, loadgen.Turn(1.0, 10)), session(2, loadgen.Turn(1.0, 10)))
    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(
            loadgen.replay(loadgen.Load(sessions), keys, "http://gateway.example.com", at_once=2,
                           transport=httpx.MockTransport(answer))
        )  # fmt: skip
    assert raised.group_contains(httpx.ConnectError)

    seen: list[loadgen.Record] = []
    records = asyncio.run(
        loadgen.replay(loadgen.Load(sessions), keys, "http://gateway.example.com", at_once=2,
                       transport=httpx.MockTransport(answer), keep_failures=True, on_record=seen.append)
    )  # fmt: skip
    # Each session ends at its first request: one failed, the other refused with the gateway's cause.
    assert seen == records
    assert [(one.student, one.status, one.failure, one.cause) for one in records] == [
        (0, None, loadgen.Failure.CONNECT_ERROR, None),
        (1, 503, None, "no_session"),
    ]
    assert all(one.sent_at is not None for one in records)
