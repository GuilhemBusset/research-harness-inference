"""Authentication from the headers (`rhi_gateway.auth`), over the in-memory store.

Every key is generated when the tests run, and a malformed one is made from a generated one: no
key is written in this file.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from rhi_common.causes import Cause
from rhi_common.keys import KEY_PATTERN, generate_key, is_valid_key, key_hash
from rhi_common.students import StudentKey
from rhi_gateway import routes
from rhi_gateway.app import build_app
from rhi_gateway.auth import Authenticator, authenticated
from rhi_gateway.errors import GatewayError
from rhi_gateway.keystore import InMemoryKeyStore, KeyRecord

NOW = 1_800_000_000
COURSE_END = NOW + 90 * 86400


class Clock:
    """The time the test sets: it gives the authenticator both of its clocks."""

    def __init__(self) -> None:
        self.now = float(NOW)

    def __call__(self) -> float:
        return self.now


def issued(key: str, **changes: int | None) -> StudentKey:
    """The item of a key issued to one student; `changes` replace attributes of it."""
    item = StudentKey(
        key_hash=key_hash(key),
        github_id=4242,
        alias="otter",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=COURSE_END,
        revoked_at=None,
        daily_token_budget=2_000_000,
        concurrency_limit=4,
    )
    return replace(item, **changes)


def setup(*keys: StudentKey) -> tuple[Authenticator, InMemoryKeyStore, Clock]:
    store, clock = InMemoryKeyStore(*keys), Clock()
    return Authenticator(store, wall_clock=clock, monotonic=clock), store, clock


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + key}


def api_key(key: str) -> dict[str, str]:
    return {"x-api-key": key}


def accept(authenticator: Authenticator, headers: dict[str, str] | list[tuple[str, str]]) -> KeyRecord:
    """The record the authenticator gives for a request with these headers, a repeated one as a list."""
    pairs = list(headers.items()) if isinstance(headers, dict) else headers
    return asyncio.run(authenticator.authenticate(Headers(raw=raw_headers(pairs))))


def raw_headers(pairs: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """Headers as a server gives them: lower-case names, bytes."""
    return [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in pairs]


def refused(authenticator: Authenticator, headers: dict[str, str] | list[tuple[str, str]]) -> bool:
    """Whether the request is refused, with the cause `invalid_key` and no word on why."""
    try:
        accept(authenticator, headers)
    except GatewayError as error:
        assert error.cause is Cause.INVALID_KEY
        assert str(error) == ""
        return True
    return False


def malformed(key: str) -> dict[str, str]:
    """Strings that are not keys, each made from a key: a wrong shape, then a wrong checksum."""
    return {
        "empty": "",
        "short": key[:-1],
        "long": key + "0",
        "prefix": "xyz_" + key[4:],
        "character": key[:10] + "-" + key[11:],
        "space": key[:10] + " " + key[11:],
        "checksum": key[:-1] + ("1" if key[-1] == "0" else "0"),
    }


def send(
    authenticator: Authenticator, headers: dict[str, str], route: routes.Route = routes.MESSAGES
) -> tuple[int, dict[str, str], bytes, list[str]]:
    """What the application answers to a request with these headers, and what the request did.

    The request's body raises when it is read. The route's handler stands for the ones that read
    the body and reserve budget: it records that it ran.
    """
    did: list[str] = []

    async def handler(request: Request, entry: routes.Route, student: KeyRecord) -> Response:
        did.append("handler for " + student.alias)
        return Response(status_code=204)

    async def receive() -> Message:
        did.append("body read")
        raise AssertionError("the body was read")

    sent: list[Message] = []

    async def keep(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "server": ("gateway.example.com", 80),
        "client": ("192.0.2.1", 50000),
        "method": route.method,
        "root_path": "",
        "path": route.path,
        "raw_path": route.path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers(list(headers.items())),
        "state": {},
    }
    app = build_app({route: authenticated(authenticator, handler)})
    asyncio.run(app(scope, receive, keep))
    answered = {name.decode("latin-1"): value.decode("latin-1") for name, value in sent[0]["headers"]}
    return sent[0]["status"], answered, b"".join(message.get("body", b"") for message in sent[1:]), did


@pytest.mark.parametrize("headers", [bearer, api_key, lambda key: bearer(key) | api_key(key)])
def test_each_header_is_accepted(headers: Callable[[str], dict[str, str]]) -> None:
    key = generate_key()
    authenticator, store, _ = setup(issued(key))

    student = accept(authenticator, headers(key))

    assert student == KeyRecord(
        github_id=4242,
        alias="otter",
        daily_token_budget=2_000_000,
        concurrency_limit=4,
        expires_at=COURSE_END,
        revoked=False,
    )
    assert store.lookups == [key_hash(key)]


def test_an_accepted_request_reaches_its_handler_with_the_student() -> None:
    key = generate_key()
    authenticator, _, _ = setup(issued(key))

    status, _, _, did = send(authenticator, api_key(key))

    assert (status, did) == (204, ["handler for otter"])


def test_differing_headers_are_refused_with_a_401() -> None:
    key, other = generate_key(), generate_key()
    authenticator, store, _ = setup(issued(key), issued(other))

    status, _, body, did = send(authenticator, bearer(key) | api_key(other))

    assert status == 401
    assert json.loads(body)["error"]["type"] == "invalid_key"
    assert did == []
    # Two valid keys, and neither was looked up.
    assert store.lookups == []


def test_a_request_that_presents_no_single_key_is_refused() -> None:
    key, other = generate_key(), generate_key()
    authenticator, store, _ = setup(issued(key), issued(other))

    for headers in (
        {},
        {"Authorization": key},
        {"Authorization": "Basic " + key},
        {"Authorization": "Basic " + key} | api_key(key),
        bearer(key) | api_key(""),
        [("x-api-key", key), ("x-api-key", other)],
        [("Authorization", "Bearer " + key), ("Authorization", "Bearer " + other)],
    ):
        assert refused(authenticator, headers)
    assert store.lookups == []


def test_a_malformed_key_makes_no_lookup() -> None:
    key = generate_key()
    authenticator, store, _ = setup(issued(key))

    for why, candidate in malformed(key).items():
        assert not is_valid_key(candidate), why
        assert refused(authenticator, bearer(candidate)), why
        assert refused(authenticator, api_key(candidate)), why

    assert store.lookups == []


def test_no_refusal_says_which_rule_refused() -> None:
    known, unknown, revoked, expired = (generate_key() for _ in range(4))
    authenticator, _, _ = setup(issued(known), issued(revoked, revoked_at=NOW - 1), issued(expired, expires_at=NOW))

    answers = [
        send(authenticator, headers)
        for headers in (
            {},
            bearer(known) | api_key(unknown),
            api_key(malformed(known)["checksum"]),
            api_key(unknown),
            api_key(revoked),
            api_key(expired),
        )
    ]

    status, _, body, did = answers[0]
    assert (status, did) == (401, [])
    assert json.loads(body)["error"] == {"type": "invalid_key", "message": Cause.INVALID_KEY.sentence}
    assert all(answer == answers[0] for answer in answers)


def test_a_refusal_is_in_the_wire_format_of_its_route() -> None:
    authenticator, _, _ = setup()

    for route in (routes.RESPONSES, routes.CHAT_COMPLETIONS, routes.STATUS):
        status, _, body, did = send(authenticator, {}, route)
        assert (status, did) == (401, [])
        assert json.loads(body)["error"]["type"] == "invalid_key"


def test_the_body_is_not_read() -> None:
    known, unknown = generate_key(), generate_key()
    authenticator, _, _ = setup(issued(known))

    # Refused at each step: no key, two keys, a malformed key, and a key the store does not hold.
    for headers in ({}, bearer(known) | api_key(unknown), bearer(malformed(known)["checksum"]), bearer(unknown)):
        status, _, _, did = send(authenticator, headers)
        # Neither the body was read nor the handler, which is where budget is reserved, ran.
        assert (status, did) == (401, [])


def test_the_cache_lasts_60_seconds() -> None:
    known, unknown = generate_key(), generate_key()

    # An answer is cached found or not: a key the store holds, then one it does not.
    for key in (known, unknown):
        for seconds_apart, lookups in ((59, 1), (61, 2)):
            authenticator, store, clock = setup(issued(known))

            first = refused(authenticator, bearer(key))
            clock.now += seconds_apart
            second = refused(authenticator, api_key(key))

            assert first == second == (key is unknown)
            assert store.lookups == [key_hash(key)] * lookups


def test_a_revoked_key_stops_within_60_seconds() -> None:
    key = generate_key()
    authenticator, store, clock = setup(issued(key))

    assert not refused(authenticator, bearer(key))
    clock.now += 1
    store.revoke(key_hash(key), at=NOW + 1)

    clock.now = NOW + 61
    assert refused(authenticator, bearer(key))


def test_a_key_past_its_expiry_is_refused_from_the_cache() -> None:
    key = generate_key()
    authenticator, store, clock = setup(issued(key, expires_at=NOW + 30))

    clock.now = NOW + 29
    assert not refused(authenticator, bearer(key))
    clock.now = NOW + 30
    assert refused(authenticator, bearer(key))
    assert len(store.lookups) == 1


def test_the_cache_holds_no_more_than_its_limit() -> None:
    store, clock = InMemoryKeyStore(), Clock()
    authenticator = Authenticator(store, wall_clock=clock, monotonic=clock, max_entries=2)
    first, second, third = (generate_key() for _ in range(3))

    for key in (first, second, third, third, second, first):
        assert refused(authenticator, bearer(key))

    # The third pushed the first out: only the first is looked up again.
    assert store.lookups == [key_hash(key) for key in (first, second, third, first)]


def test_no_key_is_written_in_the_test_files() -> None:
    for name in ("test_auth.py", "test_keystore.py"):
        assert re.search(KEY_PATTERN, Path(__file__).with_name(name).read_text(encoding="utf-8")) is None
