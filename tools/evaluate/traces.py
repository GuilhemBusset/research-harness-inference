"""Freezes the conversations a replay sends, before any of them is sent
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol").

Spike S5's synthetic session puts the text of each answer into the next request
(`loadgen.SyntheticSession`), so two configurations that answer differently are sent different
inputs from the second turn on, whatever the seed. `build` makes the whole conversation from the
seed alone instead: a `Trace`, whose requests and pauses are fixed before the first is sent. The
replay still waits for each answer to end before the next turn, and nothing of the answer enters a
later request. It reads one fixture and nothing else: no network, no clock, no credentials.

**What a trace is.** The requests of one conversation, each with the pause before it, as the
recorded-session reader holds them (`loadgen.RecordedSession`): `Trace.session()` is that session,
and `Trace.recorded()` is its JSON text, which `loadgen.read_recorded` reads. A request is a
streamed Messages request as Claude Code sends it in spike S5 (`s5_capacity.as_claude_code`), with
its tools: the system prompt, the tool definitions, the messages, adaptive thinking at the effort
`high`. It carries the two headers a synthetic session sends: no host, no key.

**The tool turns** come from a course fixture, `fixtures/replay/course.json`: the `Read` tool as
its capture documents it (`tests/captures/profile-document-as-text/`), its definition, the call an
assistant makes of it and the result that answers the call. The first request is the starting
transcript. Each later one is the request before it, whole, then two canonical turns: the
assistant's, text and the call under an ID of its own, and the user's, the call's result. The text
of the assistant's turn stands for the answer and is as long as the output a request asks for;
the result holds the turn's new input, as the text of the file that was read, where the capture
attaches a document.

**Sizes** are spike S5's (`S5`): an opening of 20,000 tokens inside a starting transcript of
50,000 to 100,000, turns that add 1,000 to 5,000 tokens of input and ask for 500 of output, 5 to
10 seconds of tools between turns, and ten turns after the first. The text is S5's, one word to a
token, drawn as a synthetic session draws its own. The opening is the same text in every trace,
S5's own (`s5_capacity.opening`). The sizes, the pauses and the rest of the text depend on the
seed and on the conversation's number, and on nothing else.

**The prefix namespace.** The first block of the system prompt names the repetition: text that
depends on the seed and on the namespace's number alone. It stands before the opening, which is
what a class reuses, so two repetitions share no prompt prefix, and every conversation of one
repetition shares the namespace, the opening and the tools (`Trace.prefix`). The same seed and
namespace give the same bytes for every treatment that is compared. The per-student cache salt is
the gateway's and is not written here.

**What is recorded.** `Trace.record()` is what was requested, and it is known before anything is
sent: for each request its pause, the tokens it holds and adds as the generator counts them, the
output it asks for and its length in bytes, with the hash of the trace and of its prefix.
`trace_hash` is over the pauses and the requests themselves, so a trace read back from a file has
the hash it was built with. What an engine counts is another record: token counts come from the
usage it serves (`contracts.RequestRecord.usage`) and differ with the tokenizer, and nothing of
that is in a trace.

It needs `spikes` on the path, for the session generator.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

import loadgen

if TYPE_CHECKING:
    from collections.abc import Iterable

COURSE = Path(__file__).resolve().parent / "fixtures" / "replay" / "course.json"

# Tokens. The opening every conversation shares, and the seed its text is drawn from: spike S5's
# (`s5_capacity.OPENING_TOKENS`, `OPENING_SEED`).
OPENING_TOKENS = 20_000
OPENING_SEED = 2**63
# Turns after a conversation's first, as in spike S5 (`s5_capacity.TURNS_PER_SESSION`).
TURNS = 10
# The reasoning Claude Code asks for by default (`s5_capacity.CLAUDE_CODE_THINKING`, `CLAUDE_CODE_EFFORT`).
THINKING = {"type": "adaptive"}
EFFORT = "high"
# What a synthetic session of spike S5 sends with a request.
HEADERS = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
# The ID of the call of turn `n`, in the form the course fixture writes its own.
TOOL_ID = "toolu_{:02d}"


@dataclass(frozen=True)
class Sizes:
    """The sizes conversations are drawn to: spike S5's, unless given."""

    opening_tokens: int = OPENING_TOKENS
    # The starting transcript, the opening included.
    transcript_tokens: tuple[int, int] = loadgen.TRANSCRIPT_TOKENS
    turn_input_tokens: tuple[int, int] = loadgen.TURN_INPUT_TOKENS
    turn_output_tokens: int = loadgen.TURN_OUTPUT_TOKENS
    tool_delay_ms: tuple[int, int] = (round(loadgen.PAUSE_SECONDS[0] * 1000), round(loadgen.PAUSE_SECONDS[1] * 1000))
    turns: int = TURNS

    def __post_init__(self) -> None:
        if not 0 <= self.opening_tokens <= self.transcript_tokens[0]:
            raise ValueError("the opening is inside the starting transcript")


S5 = Sizes()


@dataclass(frozen=True)
class Course:
    """The tool turns of the course fixture: the definitions, a call and the result that answers it."""

    tools: tuple[dict[str, Any], ...]
    call: dict[str, Any]
    result: dict[str, Any]


def read_course(path: Path = COURSE) -> Course:
    """The course fixture a file holds."""
    held = json.loads(path.read_text(encoding="utf-8"))
    return Course(tuple(held["tools"]), held["call"], held["result"])


@dataclass(frozen=True)
class Requested:
    """What one request of a trace asks for, as the generator counts it: one token to a word of
    the text it drew. The few words of the fixture and of the namespace are in no count."""

    turn: int
    pause_ms: int
    # The drawn text of the whole request: `opening_tokens`, then every message.
    transcript_tokens: int
    # The part of it that stands for the answer before: the canonical assistant turn.
    scripted_output_tokens: int
    # The part of it that is new input: the transcript after the opening, then each turn's result.
    new_input_tokens: int
    # The output the request asks for.
    max_tokens: int
    body_bytes: int


@dataclass(frozen=True)
class Trace:
    """One conversation, frozen: its requests with the pause before each, and what they ask for."""

    model: str
    seed: int
    namespace: int
    conversation: int
    opening_tokens: int
    exchanges: tuple[loadgen.Exchange, ...]
    requested: tuple[Requested, ...]

    @property
    def trace_hash(self) -> str:
        return trace_hash(self.exchanges)

    @property
    def prefix(self) -> bytes:
        """What every conversation of the repetition starts with, in every treatment: the system
        prompt, which is the namespace and then the opening, and the tools."""
        body = json.loads(self.exchanges[0].request.body)
        return json.dumps({"system": body["system"], "tools": body["tools"]}).encode()

    def session(self) -> loadgen.RecordedSession:
        """The trace as the session a replay sends."""
        return loadgen.RecordedSession(self.exchanges)

    def recorded(self) -> str:
        """The trace as the JSON text of a recorded session (`loadgen.read_recorded`)."""
        held = [
            {
                "pause_seconds": exchange.pause_seconds,
                "capture": {
                    "request": {
                        "method": exchange.request.method,
                        "path": exchange.request.path,
                        "headers": dict(exchange.request.headers),
                        "body": exchange.request.body.decode(),
                    }
                },
            }
            for exchange in self.exchanges
        ]
        return json.dumps({"exchanges": held}, indent=1) + "\n"

    def record(self) -> dict[str, Any]:
        """What was requested, which is known before anything is sent. It holds nothing an answer reports."""
        return {
            "trace_hash": self.trace_hash,
            "prefix_hash": _sha256(self.prefix),
            "model": self.model,
            "seed": self.seed,
            "namespace": self.namespace,
            "conversation": self.conversation,
            "opening_tokens": self.opening_tokens,
            "requests": [asdict(one) for one in self.requested],
        }


def trace_hash(exchanges: Iterable[loadgen.Exchange]) -> str:
    """The hash of a session's pauses, to the millisecond, and of its requests as they are sent."""
    digest = hashlib.sha256()
    for exchange in exchanges:
        request = exchange.request
        pause_ms, headers = round(exchange.pause_seconds * 1000), sorted(request.headers.items())
        head = json.dumps([pause_ms, request.method, request.path, headers, len(request.body)])
        digest.update(head.encode() + b"\n" + request.body + b"\n")
    return "sha256:" + digest.hexdigest()


def namespace_text(seed: int, namespace: int) -> str:
    """The text that opens every prompt of one repetition: a digest of the seed and the
    namespace's number, so it differs within its first words for another of either, and depends on
    nothing else."""
    return _sha256(json.dumps(["namespace", seed, namespace]).encode()).removeprefix("sha256:") + " replay namespace"


@cache
def opening(tokens: int = OPENING_TOKENS) -> str:
    """The opening every conversation shares: spike S5's, when it has S5's size."""
    return _drawn(tokens, OPENING_SEED)


def build(
    model: str,
    seed: int,
    namespace: int,
    conversation: int = 0,
    *,
    sizes: Sizes = S5,
    course: Course | None = None,
) -> Trace:
    """Conversation `conversation` of the repetition whose input seed is `seed`, under the prefix
    namespace `namespace`, for the pool `model` names. The same arguments give the same trace."""
    course = read_course() if course is None else course
    sizes_of = Random(_derive("sizes", seed, conversation))
    transcript = sizes_of.randint(*sizes.transcript_tokens)
    own = transcript - sizes.opening_tokens
    system = [_block(namespace_text(seed, namespace)), _block(opening(sizes.opening_tokens))]
    messages: list[dict[str, Any]] = [_message("user", _block(_text(own, seed, conversation, 0, "user")))]

    def request() -> loadgen.Request:
        body = {
            "model": model,
            "max_tokens": sizes.turn_output_tokens,
            "stream": True,
            "system": system,
            "tools": list(course.tools),
            "messages": messages,
            "thinking": THINKING,
            "output_config": {"effort": EFFORT},
        }
        return loadgen.Request("POST", loadgen.MESSAGES_PATH, dict(HEADERS), json.dumps(body).encode())

    exchanges = [loadgen.Exchange(0.0, request())]
    requested = [Requested(0, 0, transcript, 0, own, sizes.turn_output_tokens, len(exchanges[0].request.body))]
    for turn in range(1, sizes.turns + 1):
        pause_ms, added = sizes_of.randint(*sizes.tool_delay_ms), sizes_of.randint(*sizes.turn_input_tokens)
        answer = _block(_text(sizes.turn_output_tokens, seed, conversation, turn, "assistant"))
        read = _block(_text(added, seed, conversation, turn, "user"))
        call = course.call | {"id": TOOL_ID.format(turn)}
        result = course.result | {"tool_use_id": call["id"], "content": [*course.result["content"], read]}
        messages += [_message("assistant", answer, call), _message("user", result)]
        transcript += sizes.turn_output_tokens + added
        exchanges.append(loadgen.Exchange(pause_ms / 1000, request()))
        size = len(exchanges[-1].request.body)
        requested.append(
            Requested(turn, pause_ms, transcript, sizes.turn_output_tokens, added, sizes.turn_output_tokens, size)
        )
    return Trace(model, seed, namespace, conversation, sizes.opening_tokens, tuple(exchanges), tuple(requested))


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _derive(*parts: object) -> int:
    """A seed that depends on every part and on nothing else."""
    return int.from_bytes(hashlib.sha256(json.dumps(parts).encode()).digest()[:8])


def _drawn(tokens: int, seed: int) -> str:
    """`tokens` of spike S5's text, drawn from `seed` as a synthetic session draws its own."""
    _, first = next(loadgen.SyntheticSession("", tokens, (), 0, seed).requests())
    return json.loads(first.body)["messages"][0]["content"]


def _text(tokens: int, *parts: object) -> str:
    return _drawn(tokens, _derive("text", *parts))


def _block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _message(role: str, *content: dict[str, Any]) -> dict[str, Any]:
    return {"role": role, "content": list(content)}
