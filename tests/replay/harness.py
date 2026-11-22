"""Replay a capture through the gateway (system design, section 6.2).

A capture (`tests/captures/README.md`) holds a client's request, the answer an engine gave to it
and the usage that answer reports. A replay sends the request to the gateway, whole: its
authentication, the body filter of the route, the reservation, the proxy, the usage parser of the
wire format and the settlement, as `rhi_gateway.app` puts them together. Nothing of the gateway is
rewritten here. What stands in is around it:

- **the client** speaks ASGI to the application, with the capture's request and a key made for the
  replay (a capture holds none), and can leave in the middle of an answer;
- **the upstream** is the transport of the proxy's own client: it keeps the body it is sent and
  answers the recorded bytes in the chunks the replay chooses;
- **the ledger** is the gateway's in-memory one, which also notes each reservation and whether
  each settlement removed its lease; the proxy is the gateway's own, which also notes what each
  settlement observed.

**The cuts.** The network cuts an answer wherever it likes, so an answer is replayed once for each
cut of `cuts`: the whole answer still arrives, in chunks that end at the cut's positions. A short
answer (`SHORT_BYTES` at most) is cut at every byte position, one replay each; a longer one at
`SEEDED_CUTS` positions drawn with a fixed seed, so that a failure comes back on the next run. The
last replay of either cuts at all those positions at once.

**The disconnects.** A streamed answer is also replayed once for each of its events, the upstream
handing over one event at a time and the client leaving as soon as it has that event: the upstream
then hands over nothing more. `Replayed.handed` is the usage reported by what the gateway was
handed until then, read here (`reported`) and not by the gateway's parsers.

**The forwarded body.** When a capture says what the gateway must forward, every replay compares
it with the body the upstream received, as JSON values, field by field. The request ID and the
cache salt are left out: the gateway writes them for each request and each student. Any other
difference raises `ForwardedBodyDiffers`.

**The request profile.** A capture may also state the request profile it is replayed with: the
names of its entries, in order, and the reasoning-effort mapping, as a pool's configuration writes
them. Every pool of its replay then has that profile, and the body filters run it as the gateway's
entry point has them run it (`rhi_gateway.profile.build`, then `rhi_gateway.profile.rewrites`), so
the forwarded body compared is the one the profile produced. A capture that states one states the
body to forward too. A capture that states none is replayed with no profile.

A replay returns what happened (`Replayed`); `tests/replay/test_replay.py` says what must have.
The pools are those of `config.example.toml`. Every capture of `CAPTURES` is replayed, those of its
subdirectories included, each named by its path under the folder. Nothing here reaches the network
or AWS.
"""

from __future__ import annotations

import asyncio
import datetime
import itertools
import json
import random
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import Response
from starlette.types import Message, Receive, Scope

import scrub
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.students import StudentKey, utc_day
from rhi_gateway import profile, proxy, routes
from rhi_gateway.affinity import Affinity
from rhi_gateway.app import Filter, Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import CACHE_SALT_FIELD, REQUEST_ID_FIELD, BodyFilter, Forwarded, Rewrite
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.errors import WireFormat
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import PoolView

REPO = Path(__file__).resolve().parents[2]
CAPTURES = REPO / "tests" / "captures"

_EXAMPLE = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
POOLS = parse_pools(_EXAMPLE["pools"])
DEFAULT_POOL = next(pool for pool in POOLS if pool.name == _EXAMPLE["default_pool"])

# An answer of this many bytes at most is cut at every byte position; a longer one at seeded ones.
SHORT_BYTES = 5000
SEEDED_CUTS = 256
SEED = 62

# What the gateway writes for each request and each student: left out when two bodies are compared.
LEFT_OUT = frozenset({REQUEST_ID_FIELD, CACHE_SALT_FIELD})
# What a capture's `profile` may hold: a pool's two fields of its request profile, the first required.
PROFILE_FIELDS = frozenset({"request_profile", "reasoning_effort"})
# The headers that carry a key. A capture has neither: the replay presents a key of its own.
KEY_HEADERS = frozenset({"authorization", "x-api-key"})

STREAM_TYPE = "text/event-stream"

# The student of every replay, and the moment the gateway's clocks stand at. Both are invented.
STUDENT = 1
NOW = 1_800_000_000
# The course end the ledger's rows expire by.
COURSE_END_DATE = datetime.date(2027, 6, 30)
# No replay may wait this long, in seconds: one that does has hung, and fails.
GUARD_SECONDS = 30.0


class ForwardedBodyDiffers(AssertionError):
    """The body the upstream received is not the one the capture says the gateway must forward."""


@dataclass(frozen=True)
class Usage:
    """The tokens an answer reports: the whole input, and the output."""

    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Capture:
    """One capture, read: the request, the profile and the body to forward if given, the answer, the usage expected."""

    name: str
    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes
    forward: Mapping[str, Any] | None
    profile: Mapping[str, Any] | None
    status: int
    answer_headers: Mapping[str, str]
    answer: bytes
    usage: Usage

    @property
    def route(self) -> routes.Route:
        """The forwarded route of the gateway's table that the request is for."""
        route = routes.match(self.method, self.target.encode("ascii"))
        if route is None or route.wire_format is None:
            raise ValueError(f"the capture {self.name} is for no forwarded route of the gateway")
        return route

    @property
    def wire_format(self) -> WireFormat:
        wire_format = self.route.wire_format
        assert wire_format is not None
        return wire_format

    @property
    def streamed(self) -> bool:
        """Whether the upstream's answer is a stream, as the proxy tells: by its content type."""
        content_type = {name.lower(): value for name, value in self.answer_headers.items()}.get("content-type", "")
        return content_type.split(";", 1)[0].strip().lower() == STREAM_TYPE


def _text(value: object) -> bytes:
    """A body of a capture file: one text, or texts to join end to end."""
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "".join(value).encode("utf-8")
    raise ValueError("a body is a text, or a list of texts")


def parse(name: str, value: object) -> Capture:
    """The capture a JSON value describes. The scrubber reads it first, and its refusal is raised."""
    scrub.check(value)
    if not isinstance(value, dict) or not value.keys() <= {"request", "profile", "forward", "answer", "usage"}:
        raise ValueError(
            f"the capture {name} is not an object of a request, a profile, a body to forward, an answer and a usage"
        )
    request, answer, usage = value["request"], value["answer"], value["usage"]
    headers = dict(request["headers"])
    if KEY_HEADERS & {header.lower() for header in headers}:
        raise ValueError(f"the capture {name} has a header that carries a key")
    forward = value.get("forward")
    if forward is not None and not isinstance(forward, dict):
        raise ValueError(f"the body to forward of the capture {name} is not a JSON object")
    stated = value.get("profile")
    if stated is not None:
        if not isinstance(stated, dict) or "request_profile" not in stated or not stated.keys() <= PROFILE_FIELDS:
            raise ValueError(
                f"the profile of the capture {name} is not an object of `request_profile`, `reasoning_effort`"
            )
        if forward is None:
            raise ValueError(f"the capture {name} states a profile and not the body the engine must receive")
        pools(stated)  # A profile no pool may have is refused here, by the pools' own parser.
    return Capture(
        name=name,
        method=request["method"],
        target=request["path"],
        headers=headers,
        body=_text(request["body"]),
        forward=forward,
        profile=stated,
        status=answer["status"],
        answer_headers=dict(answer["headers"]),
        answer=_text(answer["body"]),
        usage=Usage(usage["input_tokens"], usage["output_tokens"]),
    )


def load(path: Path, name: str | None = None) -> Capture:
    """The capture of one file, named after the file unless a name is given."""
    return parse(name or path.stem, json.loads(path.read_text(encoding="utf-8")))


def folder(path: Path = CAPTURES) -> list[Capture]:
    """Every capture of a folder and of its subdirectories, named by its path under the folder, in that path's order."""
    files = sorted(path.rglob("*.json"), key=lambda file: file.relative_to(path).as_posix())
    return [load(file, file.relative_to(path).with_suffix("").as_posix()) for file in files]


def pools(stated: Mapping[str, Any] | None = None) -> tuple[Pool, ...]:
    """The pools of a replay: the example's, each with the stated request profile when one is stated."""
    if stated is None:
        return POOLS
    return parse_pools({name: {**entry, **stated} for name, entry in _EXAMPLE["pools"].items()})


def filters(stated: Mapping[str, Any] | None = None) -> dict[routes.Route, Filter]:
    """The body filter of each forwarded route, built as the gateway builds them (`rhi_gateway.main`).

    With a stated request profile, every pool has it and each filter runs it; with none, no filter
    runs a profile.
    """
    replayed = pools(stated)
    default_pool = next(pool for pool in replayed if pool.name == DEFAULT_POOL.name)
    profiles = profile.build(replayed) if stated is not None else {}

    def rewrites(route: routes.Route) -> dict[str, Rewrite]:
        return profile.rewrites(profiles, route)

    return {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, replayed, default_pool, rewrites(routes.MESSAGES)),
        routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, replayed, default_pool, rewrites(routes.COUNT_TOKENS)),
        routes.CHAT_COMPLETIONS: ChatBodyFilter(replayed, default_pool, rewrites(routes.CHAT_COMPLETIONS)),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, replayed, default_pool, rewrites(routes.RESPONSES)),
    }


def cuts(answer: bytes) -> list[tuple[int, ...]]:
    """Where each replay cuts an answer: one position at a time, then all of them at once."""
    positions: Sequence[int] = range(1, len(answer))
    if len(answer) > SHORT_BYTES:
        positions = sorted(random.Random(SEED).sample(positions, SEEDED_CUTS))
    return [*((position,) for position in positions), tuple(positions)]


def split(answer: bytes, cut: Sequence[int]) -> list[bytes]:
    """The whole answer, in chunks that end at the cut's positions."""
    edges = [0, *cut, len(answer)]
    return [answer[start:end] for start, end in itertools.pairwise(edges)]


def events(answer: bytes) -> list[bytes]:
    """A stream's events, each with the empty line that ends it. Bytes after the last one are in none."""
    found: list[bytes] = []
    start = position = 0
    for line in answer.splitlines(keepends=True):
        position += len(line)
        if line in (b"\n", b"\r\n"):
            found.append(answer[start:position])
            start = position
    return found


_INPUT_COUNTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "prompt_tokens")
_OUTPUT_COUNTS = ("output_tokens", "completion_tokens")


def reported(answer: bytes) -> Usage:
    """The usage that an answer, or the part of a stream received so far, reports.

    Read here, apart from the gateway's parsers, for the three formats at once: a usage object
    stands at `usage`, at `message.usage` or at `response.usage` of an answer or of an event's
    data, and each count is the last one given. A count nobody gave is zero.
    """
    counts: dict[str, int] = {}
    texts = [answer.decode("utf-8", "replace")]
    # Split at line feeds only: a string of an event's data may hold a character that Python calls a line's end.
    texts += [line.removeprefix("data:") for line in texts[0].split("\n") if line.startswith("data:")]
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        for holder in (data, data.get("message"), data.get("response")) if isinstance(data, dict) else ():
            usage = holder.get("usage") if isinstance(holder, dict) else None
            if isinstance(usage, dict):
                counts.update({name: count for name, count in usage.items() if isinstance(count, int)})
    return Usage(
        sum(counts.get(name, 0) for name in _INPUT_COUNTS), sum(counts.get(name, 0) for name in _OUTPUT_COUNTS)
    )


@dataclass(frozen=True)
class Settled:
    """One settlement: what the proxy observed, and whether the ledger removed the lease (`False`: nothing was there)."""

    input_tokens: int
    output_tokens: int
    ended_cleanly: bool
    removed: bool


@dataclass(frozen=True)
class Replayed:
    """What one replay did."""

    cut: tuple[int, ...]
    """The positions the upstream's answer was cut at."""
    left_after: int | None
    """How many events the client took before it left; `None` for a client that stayed."""
    status: int | None
    received: bytes
    """What the client received of the answer's body."""
    forwarded: bytes | None
    """The body the upstream received; `None` if the gateway forwarded nothing."""
    handed: Usage
    """The usage reported by what the upstream had handed to the gateway when the replay ended."""
    reserved: int
    """The reservations the gateway made: one, for a request that was forwarded."""
    settled: tuple[Settled, ...]
    """Every settlement the ledger was asked for, in order."""
    leases: int
    """The leases still in the student's record at the end."""
    tokens: int
    """The tokens of the student's day at the end: 0, since nothing is charged."""


class _Ledger(InMemoryLedger):
    """The gateway's in-memory ledger, noting each reservation and each settlement it is asked for."""

    def __init__(self) -> None:
        super().__init__(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
        self.reserved: list[Reservation] = []
        self.settled: list[bool] = []

    def reserve(self, *arguments: Any, **named: Any) -> Reservation:
        reservation = super().reserve(*arguments, **named)
        self.reserved.append(reservation)
        return reservation

    def settle(self, reservation: Reservation, **named: Any) -> bool:
        removed = super().settle(reservation, **named)
        self.settled.append(removed)
        return removed


class _Proxy(proxy.Proxy):
    """The gateway's proxy, noting what each settlement it makes observed."""

    def __init__(self, *arguments: Any, **named: Any) -> None:
        super().__init__(*arguments, **named)
        self.observed: list[proxy.Observed] = []

    async def forward(
        self,
        route: routes.Route,
        forwarded: Forwarded,
        on_end: Callable[[proxy.Observed], Awaitable[None]],
        *,
        receive: Receive | None = None,
        token_sent: proxy.TokenSent | None = None,
        affinity: Affinity | None = None,
    ) -> Response:
        async def noted(observed: proxy.Observed) -> None:
            self.observed.append(observed)
            await on_end(observed)

        return await super().forward(route, forwarded, noted, receive=receive, token_sent=token_sent, affinity=affinity)


class _Upstream:
    """The engine's stand-in: it keeps the body it is sent, and answers the recorded bytes chunk by chunk.

    Once the client has left it hands over nothing more, not even the end of the answer: what the
    gateway observed is then what `handed` holds.
    """

    def __init__(self, capture: Capture, chunks: Sequence[bytes], client_left: asyncio.Event) -> None:
        self._capture = capture
        self._chunks = chunks
        self._client_left = client_left
        self.received: bytes | None = None
        self.handed = b""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received = await request.aread()
        return httpx.Response(self._capture.status, headers=dict(self._capture.answer_headers), content=self._answer())

    async def _answer(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            await self._unless_the_client_left()
            self.handed += chunk
            yield chunk
        await self._unless_the_client_left()

    async def _unless_the_client_left(self) -> None:
        if self._client_left.is_set():
            await asyncio.get_running_loop().create_future()  # Never set: the gateway closes this answer.


class _Client:
    """The client's side of one request over ASGI: it sends the capture's request, and may leave mid-answer."""

    def __init__(self, capture: Capture, key: str, leave_at: int | None) -> None:
        self._request: list[Message] = [{"type": "http.request", "body": capture.body, "more_body": False}]
        self._leave_at = leave_at  # Leaves once it has received this many bytes.
        self.left = asyncio.Event()
        self.status: int | None = None
        self.received = b""
        path, _, query = capture.target.partition("?")
        key_header = (
            ("x-api-key", key) if capture.wire_format is WireFormat.MESSAGES else ("authorization", f"Bearer {key}")
        )
        self.scope: Scope = {
            "type": "http",
            # The version uvicorn speaks: Starlette then listens for the client's disconnect itself.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": capture.method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "root_path": "",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in (*capture.headers.items(), key_header)
            ],
            "client": None,
            "server": None,
        }

    async def receive(self) -> Message:
        if self._request:
            return self._request.pop()
        await self.left.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.received += message.get("body", b"")
            if self._leave_at is not None and len(self.received) >= self._leave_at:
                self.left.set()


def _compare_forwarded(capture: Capture, received: bytes | None) -> None:
    """Raise `ForwardedBodyDiffers` unless the upstream received the body the capture gives, if it gives one."""
    if capture.forward is None:
        return
    if received is None:
        raise ForwardedBodyDiffers(f"{capture.name}: the gateway forwarded nothing")
    sent = json.loads(received)

    def written(body: Mapping[str, Any], name: str) -> str | None:
        # As JSON text: `1`, `1.0` and `true` are three values, which Python's `==` would call equal.
        return json.dumps(body[name], sort_keys=True) if name in body else None

    differing = sorted(
        name
        for name in (sent.keys() | capture.forward.keys()) - LEFT_OUT
        if written(sent, name) != written(capture.forward, name)
    )
    if differing:
        raise ForwardedBodyDiffers(
            f"{capture.name}: the body the upstream received differs from the capture's in {', '.join(differing)}"
        )


async def _replay(
    capture: Capture, chunks: Sequence[bytes], *, cut: Sequence[int] = (), left_after: int | None = None
) -> Replayed:
    """One replay: a gateway of its own, the request, the answer in `chunks`, and what came of it."""
    key = generate_key()
    student = StudentKey(
        key_hash=key_hash(key),
        github_id=STUDENT,
        alias="replay",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=NOW + 86_400,
        revoked_at=None,
        daily_token_budget=10**9,
        concurrency_limit=1,
    )
    leave_at = None if left_after is None else sum(len(chunk) for chunk in chunks[:left_after])
    client = _Client(capture, key, leave_at)
    upstream = _Upstream(capture, chunks, client.left)
    ledger = _Ledger()
    secrets = {pool.name: "the-pool-secret-of-a-replay" for pool in POOLS}
    gateway_proxy = _Proxy(proxy.upstream_client(httpx.MockTransport(upstream)), "http://pools.example.com", secrets)
    forwarding = Forwarding(
        authenticator=Authenticator(InMemoryKeyStore(student), wall_clock=lambda: float(NOW)),
        ledger=ledger,
        filters=filters(capture.profile),
        proxy=gateway_proxy,
        # Every pool is ready: the request is forwarded at once, with no hold.
        holding=Holding(views=lambda: {pool.name: PoolView(ready=True) for pool in POOLS}, wake=lambda pool: None),
    )
    app = build_app(forwarding_handlers(forwarding))
    await asyncio.wait_for(app(client.scope, client.receive, client.send), timeout=GUARD_SECONDS)
    _compare_forwarded(capture, upstream.received)
    day = ledger.day(STUDENT, utc_day(NOW))
    return Replayed(
        cut=tuple(cut),
        left_after=left_after,
        status=client.status,
        received=client.received,
        forwarded=upstream.received,
        handed=reported(upstream.handed),
        reserved=len(ledger.reserved),
        settled=tuple(
            Settled(observed.input_tokens, observed.output_tokens, observed.ended_cleanly, removed)
            for observed, removed in zip(gateway_proxy.observed, ledger.settled, strict=True)
        ),
        leases=0 if day is None else len(day.leases),
        tokens=0 if day is None else day.tokens,
    )


def replay(capture: Capture, cut: Sequence[int] = ()) -> Replayed:
    """Replay the capture once, its answer cut at these positions (whole, by default), the client staying to the end."""
    return asyncio.run(_replay(capture, split(capture.answer, cut), cut=cut))


def replay_cuts(capture: Capture) -> Iterator[Replayed]:
    """Replay the capture once for each cut of its answer (`cuts`), the client staying to the end."""
    with asyncio.Runner() as runner:
        for cut in cuts(capture.answer):
            yield runner.run(_replay(capture, split(capture.answer, cut), cut=cut))


def replay_disconnects(capture: Capture) -> Iterator[Replayed]:
    """Replay a streamed capture once for each of its events, the client leaving once it has that event.

    The upstream hands over one event at a time. An answer that is not a stream has no event, and
    gives no replay.
    """
    if not capture.streamed:
        return
    parts = events(capture.answer)
    with asyncio.Runner() as runner:
        for after in range(1, len(parts) + 1):
            yield runner.run(_replay(capture, parts, left_after=after))
