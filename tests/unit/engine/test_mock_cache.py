"""The mock engine's imitation of a prefix cache, and what the mock reports of it (system design, section 5.5).

The cache (`rhi_mock_engine.cache`) is first called by itself, with one letter for a token. The
application is then called in process, as a loopback client, with transcripts whose items are
made to a number of tokens: an answer's usage is read from where its format's reference puts the
whole input and its cached part, and the metrics from `/metrics`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from rhi_common.engine_metrics import MOCK, Kind, Quantity, metric_names
from rhi_mock_engine import app as mock
from rhi_mock_engine.app import Settings, SettingsError, create_app
from rhi_mock_engine.cache import PrefixCache

# A made-up value, not a credential: the mock only compares it with what a request carries.
SECRET = "-".join(["a", "pool", "secret", "for", "the", "cache"])
CHAT, RESPONSES, MESSAGES, _ = mock.INFERENCE_ROUTES
FORMATS = (CHAT, RESPONSES, MESSAGES)
# What a request with nothing before its transcript counts for that: an empty object, one token.
HEAD = 1


class Usage(NamedTuple):
    """What an answer reports of its input: all of it, and the part read from the cache."""

    input: int
    cached: int


def turn(tokens: int, mark: str = "x") -> dict[str, str]:
    """One item of a transcript that counts for that many tokens: four characters each, as canonical JSON."""
    empty = {"content": "", "role": "user"}
    return {**empty, "content": mark * (4 * tokens - len(json.dumps(empty, separators=(",", ":"))))}


def usage(route: str, response: httpx.Response) -> Usage:
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: {")]
    found: dict[str, Any] = {}
    for data in map(json.loads, events or [response.text]):
        for holder in (data, data.get("message"), data.get("response")):
            if isinstance(holder, dict) and isinstance(holder.get("usage"), dict):
                found.update(holder["usage"])
    if route == CHAT:
        return Usage(found["prompt_tokens"], found["prompt_tokens_details"]["cached_tokens"])
    if route == RESPONSES:
        return Usage(found["input_tokens"], found["input_tokens_details"]["cached_tokens"])
    # Messages: `input_tokens` is the part that was not read from the cache.
    return Usage(found["input_tokens"] + found["cache_read_input_tokens"], found["cache_read_input_tokens"])


async def no_sleep(seconds: float) -> None:
    del seconds


class Engine:
    """One mock engine, and so one cache."""

    def __init__(self, **settings: Any) -> None:
        self.app = create_app(Settings(secret=SECRET, **settings), sleep=no_sleep)

    def _call(self, method: str, route: str, **arguments: Any) -> httpx.Response:
        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 40000))
            async with httpx.AsyncClient(transport=transport, base_url="http://engine") as http:
                return await http.request(method, route, **arguments)

        response = asyncio.run(call())
        assert response.status_code == 200
        return response

    def ask(self, route: str, salt: str | None, *transcript: dict[str, str], stream: bool = False) -> Usage:
        """The usage of the answer to a transcript sent under a cache salt, as the gateway writes it, or under none."""
        body: dict[str, Any] = {"model": "mock", "max_tokens": 64, "stream": stream}
        body["input" if route == RESPONSES else "messages"] = list(transcript)
        if salt is not None:
            body["cache_salt"] = salt
        if stream and route == CHAT:
            body["stream_options"] = {"include_usage": True}
        return usage(route, self._call("POST", route, json=body, headers={"Authorization": f"Bearer {SECRET}"}))

    def metric(self, quantity: Quantity) -> float:
        suffix = {Kind.GAUGE: "", Kind.COUNTER: "_total"}[quantity.kind]
        families = text_string_to_metric_families(self._call("GET", "/metrics").text)
        [value] = [
            sample.value
            for family in families
            for sample in family.samples
            if sample.name == metric_names(MOCK)[quantity] + suffix
        ]
        return value


# --- The cache by itself -------------------------------------------------------------------------


def test_the_cached_tokens_are_those_shared_from_the_start_with_the_last_input_of_the_salt() -> None:
    cache = PrefixCache(capacity=100)

    assert cache.serve("ana", tuple("abcdef")) == 0
    assert cache.serve("ana", tuple("abcdefgh")) == 6
    # Another salt has sent nothing yet, whatever the first one sent.
    assert cache.serve("ben", tuple("abcdefgh")) == 0
    # A request that leaves the transcript at its fourth token shares three, and is then the one kept.
    assert cache.serve("ana", tuple("abcXYZgh")) == 3
    assert cache.serve("ana", tuple("abcdefgh")) == 3
    # A request that repeats the last one whole has all its tokens cached, and so has one that stops short of its end.
    assert cache.serve("ana", tuple("abcdefgh")) == 8
    assert cache.serve("ana", tuple("abcde")) == 5
    assert cache.serve("ana", tuple("abcdefgh")) == 5
    assert (cache.kept, cache.use) == (16, 0.16)


def test_forgetting_goes_by_the_last_request_and_never_leaves_more_than_the_capacity() -> None:
    cache = PrefixCache(capacity=12)
    for salt in ("ana", "ben", "cho"):
        cache.serve(salt, tuple("abcd"))
    assert cache.kept == 12

    # The first salt seen comes back: the oldest last request is now the second one's.
    cache.serve("ana", tuple("abcd"))
    cache.serve("dee", tuple("abcd"))
    assert cache.kept == 12
    assert cache.serve("ana", tuple("abcde")) == 4
    assert cache.serve("ben", tuple("abcde")) == 0

    # An input larger than the capacity pushes out the others, and then itself.
    assert cache.serve("ben", tuple("abcde" + "f" * 8)) == 5
    assert cache.kept == 0
    assert cache.serve("ben", tuple("abcde")) == 0

    with pytest.raises(ValueError, match="capacity"):
        PrefixCache(capacity=0)


# --- What the mock reports -----------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
@pytest.mark.parametrize("route", FORMATS)
def test_a_repeated_transcript_is_cached_for_the_salt_that_sent_it(route: str, stream: bool) -> None:
    engine = Engine()
    first, more = turn(1000 - HEAD), turn(100)

    assert engine.ask(route, "ana", first, stream=stream) == Usage(1000, 0)
    assert engine.ask(route, "ana", first, more, stream=stream) == Usage(1100, 1000)
    assert engine.ask(route, "ben", first, more, stream=stream) == Usage(1100, 0)


def test_the_oldest_session_is_forgotten_first() -> None:
    engine = Engine(cache_tokens=300)
    openings = {salt: turn(100 - HEAD, mark) for salt, mark in (("ana", "a"), ("ben", "b"), ("cho", "c"))}
    for salt, opening in openings.items():
        assert engine.ask(CHAT, salt, opening) == Usage(100, 0)
    assert engine.metric(Quantity.CACHE_USAGE) == 1

    # The first of these next turns takes the cache over its capacity, and the oldest session goes.
    more = turn(10)
    assert engine.ask(CHAT, "ben", openings["ben"], more) == Usage(110, 100)
    assert engine.ask(CHAT, "cho", openings["cho"], more) == Usage(110, 100)
    assert engine.ask(CHAT, "ana", openings["ana"], more) == Usage(110, 0)


def test_metrics_follow_the_cache() -> None:
    engine = Engine(cache_tokens=1000)
    opening, other = turn(200 - HEAD), turn(100 - HEAD, "y")

    answers = [
        engine.ask(CHAT, "ana", opening),
        engine.ask(MESSAGES, "ben", other, stream=True),
        engine.ask(CHAT, "ana", opening, turn(50)),
        engine.ask(MESSAGES, "ben", other, turn(25), stream=True),
        # With no salt, nothing of a request is read from the cache and nothing of it is kept.
        engine.ask(CHAT, None, opening),
    ]

    assert [answer.cached for answer in answers] == [0, 0, 200, 100, 0]
    assert engine.metric(Quantity.CACHED_INPUT_TOKENS) == sum(answer.cached for answer in answers)
    assert engine.metric(Quantity.INPUT_TOKENS) == sum(answer.input for answer in answers)
    # The cache keeps the last input of each of the two salts.
    assert engine.metric(Quantity.CACHE_USAGE) == (answers[2].input + answers[3].input) / 1000 == 0.375


def test_the_capacity_is_a_setting() -> None:
    assert Settings.from_env({"RHI_POOL_SECRET": SECRET}).cache_tokens == Settings(secret=SECRET).cache_tokens
    assert Settings.from_env({"RHI_POOL_SECRET": SECRET, "RHI_MOCK_CACHE_TOKENS": "5000"}).cache_tokens == 5000
    for refused in ("0", "-1", "1.5", "many"):
        with pytest.raises(SettingsError):
            Settings.from_env({"RHI_POOL_SECRET": SECRET, "RHI_MOCK_CACHE_TOKENS": refused})
