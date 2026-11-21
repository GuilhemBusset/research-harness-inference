"""A load generator that replays agent sessions through the gateway (system design, sections 4.7, 7.2 and 12).

An agent re-sends its whole transcript on every turn, so a pool is measured with sessions, not with
independent requests (evidence E31). `replay` sends sessions through the gateway, as students do, so
that the per-student cache salt, the request profile, the hold and affinity are all in the path and
the numbers are what a student gets. The spikes S3, S5 and S6 call it; it has no command line.

**A session** is an ordered list of requests, each sent when the answer to the one before has
ended and the session's pause has passed. There are two kinds:

- A `SyntheticSession` is built to given sizes, so that the shapes of section 4.7 are reproducible:
  the tokens of the transcript at its start, the input tokens each later turn adds and the pause
  before it, and the output tokens each turn asks for. It speaks Messages, streamed, as Claude Code
  does. Its first request is the starting transcript; each later one is the request before it,
  whole, then the text of the answer that request got, then the turn's new input. The text is
  drawn from the session's seed, one common three-letter word and a space to a token: four
  characters, which the mock engine counts as one token and a model's tokenizer as about one.
- A `RecordedSession` replays requests as a harness sent them. Each is the `request` part of a
  capture (`tests/captures/README.md`) with the seconds the session paused before it, counted from
  the end of the answer before. `read_recorded` reads one from JSON:

      {"exchanges": [{"pause_seconds": 0, "capture": {"request": {...}, ...}}, ...]}

  A capture holds no key: the replay adds the student's. Of a capture's headers, the host and the
  body's length are the recording's own and are not sent.

**The students.** The cache salt is per student and a student may run only a few requests at once
(section 17), so each session runs as a canary student of its own, with that student's key
(`common.canary_keys`). A `Load` is the sessions and how many of them one student runs: session
`n` is the session of student `n // sessions_per_student`, who is known in the records by that
number and never by the key. `replay` runs `at_once` sessions at a time, in the load's order, and
refuses a load with more students than keys before it sends anything.

**The two shapes of section 4.7.** `hands_on` gives each student one agent session: a transcript of
50,000 to 100,000 tokens, turns that add 1,000 to 5,000 input tokens and ask for 500 output
tokens, and 5 to 10 seconds of tools between turns. `several_agents` gives each student two such
sessions, run at once. Both draw their sizes from a seed, so a run can be repeated.

**What is recorded**, one `Record` for each request, is what the gateway's own histograms measure
(section 7.2; `rhi_gateway.experience`), seen from the student's side:

- the seconds from the request sent to its first token, and to its first token that is not
  reasoning. A token is an event of the stream that carries output, told as the gateway tells it
  (`rhi_gateway.proxy.output_of`): text, reasoning or a tool call's arguments, and the answer is
  whatever is not reasoning;
- the output tokens per second, by the gateway's rule: the output tokens after the first, over the
  seconds from the first token to the last. It is `None` where the rule gives nothing: an answer
  that is not a stream, fewer than two output tokens, or every token in one moment;
- the input (whole, cached or not), cached input and output tokens the answer reported, read from
  its usage where each wire format puts them (`None` for a count the answer did not report);
- the student, the session, whether the request was its session's first turn, and the status of
  the answer.

A session ends at its first request that is not answered 200: a transcript cannot grow by an
answer that did not come. A connection that fails raises, and ends the replay: a run with one is
not a measurement.

`replay` takes its clock and its `sleep`, so a test controls both.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from random import Random
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from rhi_gateway.sse import SseEvent, SseReader

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence

# The sizes of an agent session (section 4.7): tokens of the transcript at its start, input tokens
# a turn adds, output tokens a turn asks for, and seconds of tools between turns.
TRANSCRIPT_TOKENS = (50_000, 100_000)
TURN_INPUT_TOKENS = (1_000, 5_000)
TURN_OUTPUT_TOKENS = 500
PAUSE_SECONDS = (5.0, 10.0)
# The sessions one student runs at once in the second session's shape.
AGENTS_PER_STUDENT = 2

# Seconds. The read is the idle timeout of the load balancers (section 5.3): a held stream is kept
# alive well inside it, and a quiet one is the engine computing.
TIMEOUT = httpx.Timeout(connect=10.0, read=1200.0, write=60.0, pool=None)

MESSAGES_PATH = "/v1/messages"
STREAM_TYPE = "text/event-stream"
# The headers of a capture that are not replayed: the recording's host and length, and a key.
_NOT_REPLAYED = frozenset({"host", "content-length", "authorization", "x-api-key"})
# What stands for the answer in a synthetic transcript when the answer held no text.
_NO_TEXT = "(no text)"
# One token each, with the space that follows: common words of three letters.
_WORDS = (
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her", "was",
    "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new", "now", "old",
    "see", "two", "way", "who", "boy", "did", "its", "let", "put", "say", "she", "too", "use",
)  # fmt: skip


class Wire(StrEnum):
    """The wire format of a request, by its path."""

    MESSAGES = "messages"
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


_WIRE = {MESSAGES_PATH: Wire.MESSAGES, "/v1/chat/completions": Wire.CHAT_COMPLETIONS, "/v1/responses": Wire.RESPONSES}


def wire_of(path: str) -> Wire | None:
    """The wire format of the route a path names, its query string left out; `None` for any other route."""
    return _WIRE.get(path.partition("?")[0])


# Sessions.


@dataclass(frozen=True)
class Request:
    """One request as a client sends it, without its key: what a capture's `request` part holds."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes


class Session(Protocol):
    """An ordered list of requests, with the pause before each."""

    def requests(self) -> Generator[tuple[float, Request], str]:
        """Yields each request with the seconds to wait before sending it, and is sent the text of its answer."""
        ...


@dataclass(frozen=True)
class Turn:
    """A turn after a session's first: the seconds of pause before it, and the input tokens it adds."""

    pause_seconds: float
    input_tokens: int


@dataclass(frozen=True)
class SyntheticSession:
    """A session built to sizes, for the pool `model` names: `start_tokens` of transcript at its
    start, then `turns`, each request asking for `output_tokens`. `seed` gives its text."""

    model: str
    start_tokens: int
    turns: tuple[Turn, ...]
    output_tokens: int
    seed: int = 0

    def requests(self) -> Generator[tuple[float, Request], str]:
        words = Random(self.seed)
        messages = [_message("user", _text(self.start_tokens, words))]
        answer = yield 0.0, self._request(messages)
        for turn in self.turns:
            messages += [_message("assistant", answer or _NO_TEXT), _message("user", _text(turn.input_tokens, words))]
            answer = yield turn.pause_seconds, self._request(messages)

    def _request(self, messages: list[dict[str, str]]) -> Request:
        body = {"model": self.model, "max_tokens": self.output_tokens, "stream": True, "messages": messages}
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
        return Request("POST", MESSAGES_PATH, headers, json.dumps(body).encode())


def _message(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def _text(tokens: int, words: Random) -> str:
    return " ".join(words.choices(_WORDS, k=tokens))


@dataclass(frozen=True)
class Exchange:
    """One request of a recorded session, and the seconds the session paused before it."""

    pause_seconds: float
    request: Request


@dataclass(frozen=True)
class RecordedSession:
    """A session as a harness sent it: its requests in their recorded order."""

    exchanges: tuple[Exchange, ...]

    def requests(self) -> Generator[tuple[float, Request], str]:
        for exchange in self.exchanges:
            yield exchange.pause_seconds, exchange.request


def read_recorded(text: str | bytes) -> RecordedSession:
    """The recorded session a JSON text holds, in the layout of this module's description.
    ValueError, which repeats nothing of the text, for any other text."""
    try:
        exchanges = []
        for held in json.loads(text)["exchanges"]:
            pause, request = float(held["pause_seconds"]), held["capture"]["request"]
            method, path, headers, body = (request[part] for part in ("method", "path", "headers", "body"))
            # A body is text, or lines of text joined end to end.
            texts = [method, path, *headers, *headers.values(), *([body] if isinstance(body, str) else body)]
            if pause < 0 or not all(isinstance(one, str) for one in texts):
                raise ValueError
            body = body if isinstance(body, str) else "".join(body)
            exchanges.append(Exchange(pause, Request(method, path, dict(headers), body.encode())))
    except (ValueError, TypeError, KeyError, AttributeError):
        # Whatever the parser or a missing part raised would quote the recording: it does not travel.
        raise ValueError("the text is not a recorded session: exchanges of a pause and a capture") from None
    return RecordedSession(tuple(exchanges))


# The students, and the two shapes.


def _always() -> bool:
    return True


@dataclass(frozen=True)
class Load:
    """Sessions to replay, and how many of them one student runs: session `n` is student
    `n // sessions_per_student`'s. `sending` says whether the load still sends: it is read before
    each request, once the pause before it has passed, and a session it says no to sends nothing
    more, its requests in flight left to end."""

    sessions: tuple[Session, ...]
    sessions_per_student: int = 1
    sending: Callable[[], bool] = _always

    def __post_init__(self) -> None:
        if self.sessions_per_student < 1:
            raise ValueError("a student runs one session, or more")

    @property
    def students(self) -> int:
        """How many students the sessions are spread over."""
        return -(-len(self.sessions) // self.sessions_per_student)


def agent_session(model: str, turns: int, seed: int) -> SyntheticSession:
    """One agent session of section 4.7's sizes, drawn from `seed`: its starting transcript, then `turns` turns."""
    sizes = Random(seed)
    return SyntheticSession(
        model=model,
        start_tokens=sizes.randint(*TRANSCRIPT_TOKENS),
        turns=tuple(
            Turn(pause_seconds=sizes.uniform(*PAUSE_SECONDS), input_tokens=sizes.randint(*TURN_INPUT_TOKENS))
            for _ in range(turns)
        ),
        output_tokens=TURN_OUTPUT_TOKENS,
        seed=seed,
    )


def hands_on(model: str, students: int, *, turns: int, seed: int = 0) -> Load:
    """Students working hands-on: one agent session each."""
    return Load(tuple(agent_session(model, turns, seed + number) for number in range(students)))


def several_agents(model: str, students: int, *, turns: int, seed: int = 0) -> Load:
    """Students running several agents, as in the second session: two agent sessions each, at once."""
    sessions = tuple(agent_session(model, turns, seed + number) for number in range(students * AGENTS_PER_STUDENT))
    return Load(sessions, sessions_per_student=AGENTS_PER_STUDENT)


# What an answer is read for.


def _object(text: str) -> dict[str, Any]:
    """The JSON object a text holds, or an empty one: an answer is someone else's bytes."""
    try:
        held = json.loads(text)
    except ValueError:
        return {}
    return held if isinstance(held, dict) else {}


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _has_arguments(function: object) -> bool:
    return isinstance(function, dict) and _is_text(function.get("arguments"))


def _chat_output(delta: Mapping[str, Any]) -> bool | None:
    calls = delta.get("tool_calls")
    called = any(isinstance(call, dict) and _has_arguments(call.get("function")) for call in calls or ())
    if called or _has_arguments(delta.get("function_call")):
        return True
    if _is_text(delta.get("content")) or _is_text(delta.get("refusal")):
        return True
    # Chat Completions has no reasoning field: text in any other field, the engine's own, is reasoning.
    named = {"role", "tool_calls", "function_call"}
    return False if any(name not in named and _is_text(value) for name, value in delta.items()) else None


def output_of(wire: Wire, event: SseEvent) -> bool | None:
    """What an event of a stream carries of the output, as the gateway tells it
    (`rhi_gateway.proxy.output_of`): `None` for none, false for reasoning alone, true for a piece of
    the answer, which is its text or a tool call's arguments."""
    if event.comment:
        return None
    data = _object(event.data)
    if wire is Wire.CHAT_COMPLETIONS:
        choices = data.get("choices")
        found = {
            _chat_output(choice["delta"])
            for choice in (choices if isinstance(choices, list) else [])
            if isinstance(choice, dict) and isinstance(choice.get("delta"), dict)
        }
        return True if True in found else False if False in found else None
    kind = event.name or data.get("type")
    delta = data.get("delta")
    if not isinstance(kind, str):
        return None
    if wire is Wire.RESPONSES:
        return not kind.startswith("response.reasoning") if kind.endswith(".delta") and _is_text(delta) else None
    if kind != "content_block_delta" or not isinstance(delta, dict):
        return None
    field = {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "partial_json"}.get(
        delta.get("type") if isinstance(delta.get("type"), str) else ""
    )
    return delta.get("type") != "thinking_delta" if field and _is_text(delta.get(field)) else None


def _usage(data: Mapping[str, Any]) -> dict[str, Any]:
    """The usage object an event's data or a whole answer carries: its own, its message's or its response's."""
    for holder in (data, data.get("message"), data.get("response")):
        if isinstance(holder, dict) and isinstance(holder.get("usage"), dict):
            return holder["usage"]
    return {}


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _detail(usage: Mapping[str, Any], name: str) -> int | None:
    details = usage.get(name)
    return _whole(details.get("cached_tokens")) if isinstance(details, dict) else None


def tokens_of(wire: Wire | None, usage: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    """The input (whole, cached or not), cached input and output tokens of a usage object, read where
    each format's reference puts them; `None` for a count that is not there."""
    if wire is Wire.CHAT_COMPLETIONS:
        return (
            _whole(usage.get("prompt_tokens")),
            _detail(usage, "prompt_tokens_details"),
            _whole(usage.get("completion_tokens")),
        )
    if wire is Wire.RESPONSES:
        return (
            _whole(usage.get("input_tokens")),
            _detail(usage, "input_tokens_details"),
            _whole(usage.get("output_tokens")),
        )
    # Messages counts its input in three parts: not cached, written to the cache and read from it.
    parts = [_whole(usage.get(name)) for name in ("input_tokens", "cache_creation_input_tokens")]
    cached = _whole(usage.get("cache_read_input_tokens"))
    counted = [part for part in (*parts, cached) if part is not None]
    return (sum(counted) if counted else None, cached, _whole(usage.get("output_tokens")))


# The replay.


@dataclass(frozen=True)
class Record:
    """One request of a replay, as its student saw it."""

    student: int
    """The student's number: the position of their key among the keys given."""
    session: int
    """The session's position in the load."""
    first_turn: bool
    """Whether the request was its session's first."""
    status: int
    first_token_seconds: float | None
    """From the request sent to its first token; `None` when the answer streamed none."""
    first_answer_seconds: float | None
    """The same, to its first token that is not reasoning."""
    output_tokens_per_second: float | None
    """The output tokens after the first, over the seconds from the first token to the last."""
    input_tokens: int | None
    """The whole input the answer reported, cached or not."""
    cached_input_tokens: int | None
    output_tokens: int | None


async def _send(
    http: httpx.AsyncClient, request: Request, key: str, clock: Callable[[], float]
) -> tuple[str, dict[str, Any]]:
    """Sends one request as the student whose key is given and reads its answer to the end: the text
    of the answer, and the fields of a `Record` that the answer gives."""
    wire = wire_of(request.path)
    headers = {name: value for name, value in request.headers.items() if name.lower() not in _NOT_REPLAYED}
    headers["authorization"] = f"Bearer {key}"
    reader, usage, text, whole = SseReader(), {}, [], bytearray()
    first = answered = last = None
    sent = clock()
    async with http.stream(request.method, request.path, headers=headers, content=request.body) as answer:
        streamed = answer.headers.get("content-type", "").startswith(STREAM_TYPE)
        async for chunk in answer.aiter_bytes():
            if not streamed:
                whole += chunk
                continue
            now = clock()
            for event in reader.feed(chunk):
                data = _object(event.data)
                usage |= _usage(data)
                carried = output_of(wire, event) if wire else None
                if carried is None:
                    continue
                first, last = (now if first is None else first), now
                if carried and answered is None:
                    answered = now
                delta = data.get("delta")
                if wire is Wire.MESSAGES and isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text.append(delta["text"])
    if not streamed:
        usage = _usage(_object(whole.decode(errors="replace")))
    input_tokens, cached, output_tokens = tokens_of(wire, usage)
    speed = None
    if first is not None and last is not None and last > first and output_tokens is not None and output_tokens >= 2:
        speed = (output_tokens - 1) / (last - first)
    return "".join(text), {
        "status": answer.status_code,
        "first_token_seconds": None if first is None else first - sent,
        "first_answer_seconds": None if answered is None else answered - sent,
        "output_tokens_per_second": speed,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
    }


async def replay(
    load: Load,
    keys: Sequence[str],
    endpoint: str,
    *,
    at_once: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Record]:
    """Replays the load's sessions through the gateway at `endpoint`, `at_once` of them at a time,
    each as its student, and returns a record for every request sent, by session and then by turn.

    `keys` are the canary students' keys, one student each. ValueError, with nothing sent, for a
    load with more students than keys or for less than one session at once. `clock` gives the
    seconds every time is read in, and `sleep` waits a session's pauses; `transport` is what the
    requests are sent through, httpx's own unless given.
    """
    if load.students > len(keys):
        raise ValueError("the load has more students than there are keys")
    if at_once < 1:
        raise ValueError("a replay runs one session at once, or more")
    slots = asyncio.Semaphore(at_once)

    async def run(number: int, session: Session) -> list[Record]:
        student, records = number // load.sessions_per_student, []
        async with slots:
            requests = session.requests()
            step = next(requests, None)
            while step is not None:
                pause, request = step
                if pause > 0:
                    await sleep(pause)
                if not load.sending():
                    break
                text, read = await _send(http, request, keys[student], clock)
                records.append(Record(student=student, session=number, first_turn=not records, **read))
                try:
                    step = requests.send(text) if read["status"] == httpx.codes.OK else None
                except StopIteration:
                    step = None
        return records

    # Nothing of the machine's own configuration, a proxy among it, takes part in a request, and
    # the client never makes a session wait for a connection: `at_once` alone bounds them.
    limits = httpx.Limits(max_connections=None)
    async with (
        httpx.AsyncClient(
            base_url=endpoint, timeout=TIMEOUT, limits=limits, transport=transport, trust_env=False
        ) as http,
        asyncio.TaskGroup() as group,
    ):
        tasks = [group.create_task(run(number, session)) for number, session in enumerate(load.sessions)]
    return [record for task in tasks for record in task.result()]
