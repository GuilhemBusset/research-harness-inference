"""Authentication from the headers, before the body is read (system design, section 6.2, step 2).

The key arrives in `Authorization: Bearer` or in `x-api-key`: harnesses differ. A request is
accepted in this order, and each step costs more than the one before it:

1. every key header the request carries holds one and the same key, and there is at least one;
2. the key has the shape of a key and carries its own checksum (`rhi_common.keys.is_valid_key`),
   so junk is refused with no lookup;
3. the store holds the SHA-256 of the key, and the key is neither revoked nor past its expiry.

The store's answer, found or not, is kept for 60 seconds. That cache is the bound of section 6.4:
a key that was replaced or revoked stops working within 60 seconds. Expiry is checked on every
request, from the cached record.

A forwarded request reads its body after it is accepted, and a body may take long to arrive. So
right after it takes its concurrency slot, the write that makes the student's usage row, the request has its
key read once more, past the cache (`Authenticator.confirm`). A key revoked while the request was
on its way, or gone from the store as the admin's erase leaves it (`rhi_admin.privacy`), has its
reservation taken back, and the usage row deleted when nothing else is left in it: the read comes
after the write, so a request accepted before the erase, however late its write, leaves no row of
its own behind.

Every refusal is the cause `invalid_key` and nothing else: the error never says whether the key
was missing, malformed, unknown, revoked or expired.

`Authenticator.authenticate` is given the headers and has no way to read a body. `authenticated`
fills a handler slot of `rhi_gateway.app` with a handler that is called only for an accepted key,
so whatever that handler does (reading the body, taking a concurrency slot) is not done for a refused
request.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response

from rhi_common.causes import Cause
from rhi_common.keys import is_valid_key, key_hash
from rhi_gateway import telemetry
from rhi_gateway.app import Handler
from rhi_gateway.errors import GatewayError, WireFormat, render_json
from rhi_gateway.keystore import KeyRecord, KeyStore
from rhi_gateway.routes import Route

# How long an answer of the store is used, in seconds (sections 6.2 and 6.4).
CACHE_SECONDS = 60
# The most answers kept. Anyone can compute a checksum, so well-formed keys that nobody issued can
# be sent without end: past this number the oldest answer is dropped, which costs one more lookup.
CACHE_ENTRIES = 10_000

# A handler that needs a student: it is called with the record of the accepted key.
AuthenticatedHandler = Callable[[Request, Route, KeyRecord], Awaitable[Response]]


def _refused() -> GatewayError:
    return GatewayError(Cause.INVALID_KEY)


def _bearer(value: str) -> str | None:
    """The credential of an `Authorization` value of the scheme `Bearer`; `None` for any other value."""
    scheme, _, credential = value.partition(" ")
    return credential.strip(" ") if scheme.lower() == "bearer" else None


def presented_key(headers: Headers) -> str:
    """The key a request presents, well formed; `invalid_key` when it presents none or more than one.

    Every `Authorization` and `x-api-key` header counts, a repeated one too: an `Authorization`
    that is not a bearer credential is a header that differs from a key.
    """
    presented = [_bearer(value) for value in headers.getlist("authorization")] + headers.getlist("x-api-key")
    if not presented:
        raise _refused()
    key = presented[0]
    if key is None or any(other != key for other in presented) or not is_valid_key(key):
        raise _refused()
    return key


class Authenticator:
    """Accepts or refuses a request from its headers, with one lookup per key and minute.

    `wall_clock` gives the seconds since the epoch, which a key's expiry is compared with.
    `monotonic` measures the age of a cached answer, so that a step of the wall clock cannot
    keep a revoked key alive.
    """

    def __init__(
        self,
        store: KeyStore,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        max_entries: int = CACHE_ENTRIES,
    ) -> None:
        self._store = store
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._max_entries = max_entries
        # By SHA-256: when the lookup began, and its answer. The oldest answers come first.
        self._cache: dict[str, tuple[float, KeyRecord | None]] = {}

    async def authenticate(self, headers: Headers) -> KeyRecord:
        """The record of the student whose key the headers carry; `invalid_key` for any other request."""
        record = await self._lookup(key_hash(presented_key(headers)))
        if record is None or record.revoked or record.expires_at <= self._wall_clock():
            raise _refused()
        return record

    async def confirm(self, student: KeyRecord) -> KeyRecord:
        """The key of that accepted record, read again past the cache; `invalid_key` if it no longer passes."""
        now = self._monotonic()
        record = await run_in_threadpool(self._store.lookup, student.key_hash)
        self._remember(student.key_hash, now, record)
        if record is None or record.revoked or record.expires_at <= self._wall_clock():
            raise _refused()
        return record

    async def _lookup(self, digest: str) -> KeyRecord | None:
        now = self._monotonic()
        cached = self._cache.get(digest)
        if cached is not None and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        # The SDK blocks: the read is made on a thread, and the other requests go on meanwhile.
        record = await run_in_threadpool(self._store.lookup, digest)
        self._remember(digest, now, record)
        return record

    def _remember(self, digest: str, at: float, record: KeyRecord | None) -> None:
        self._cache.pop(digest, None)
        while self._cache:
            oldest = next(iter(self._cache))
            if len(self._cache) < self._max_entries and at - self._cache[oldest][0] < CACHE_SECONDS:
                break
            del self._cache[oldest]
        self._cache[digest] = (at, record)


def authenticated(authenticator: Authenticator, handler: AuthenticatedHandler) -> Handler:
    """A handler for a slot of the application: `handler`, called only when the key is accepted.

    A refused request is answered 401 in the wire format of its route, before anything reads its
    body.
    """

    async def slot(request: Request, entry: Route) -> Response:
        try:
            student = await authenticator.authenticate(request.headers)
        except GatewayError as refused:
            telemetry.note_cause(refused.cause)
            # A route the gateway answers itself has no wire format: it answers as `not_found` does.
            answer = render_json(refused, entry.wire_format or WireFormat.MESSAGES)
            return Response(content=answer.body, status_code=answer.status, headers=answer.headers)
        telemetry.note_student(student)
        return await handler(request, entry, student)

    return slot
