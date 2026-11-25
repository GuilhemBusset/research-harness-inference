"""The routes the gateway answers itself: the models, the status, the wake and the hello (system design, sections 5.3, 6.2).

The four local routes of the table are answered here.

| Route | Key | Answer |
| --- | --- | --- |
| `GET /v1/models` | needed | the model name of every configured pool, and which one the `claude-` names map to |
| `GET /status` | needed | one record per configured pool: its model name, its state and, when unavailable, its cause |
| `POST /wake` | needed | the record of the pool its body's `model` names, at once; the wake itself (below) is not waited for |
| `HEAD /api/hello` | none | 200 with no body |

Every student may use every configured pool, so both lists are the configuration's, in its order.
A request without a valid key is refused by `rhi_gateway.auth.authenticated` before anything here
runs, and a key that is not well formed is refused with no lookup.

A pool's state comes from what the gateway already keeps of it (`rhi_gateway.readiness`): whether
it has a healthy target, its mode and the scaler's note. Answering reads nothing else, asks for no
wake and takes no concurrency slot: cheap calls never wake a GPU, and a status call is not demand
(sections 5.1 and 5.3). Only the wake is given the waker.

| State | When |
| --- | --- |
| `unavailable` | the hold's rule (`rhi_gateway.hold`) would end a request for the pool at once, with its cause: `pool_disabled` or `pool_scheduled_only` |
| `ready` | otherwise, when the pool has a healthy target |
| `unavailable` | otherwise, with `capacity_unavailable`, when a launch found no capacity in the past 10 minutes |
| `warming` | otherwise, when the scaler asked for capacity |
| `cold` | otherwise |

A pool whose own session is not active (section 5.6, `rhi_gateway.readiness.PoolView.in_session`)
is listed `unavailable` with the cause `no_session`, whatever else the gateway keeps of it and
whoever asks, and every other pool by the table above. A wake for such a pool is answered
`no_session` (503, `rhi_gateway.errors.render_json`), asking for nothing and recording nothing. The
models and the hello answer as ever.

The first row is the hold itself, stepped once at the time of the call: the status cannot say a
pool can start when a request for it would be refused. A step is pure, and the wake it may return
is a value that is dropped here.

**The wake** starts a pool without a request, under a request's rules (sections 5.1 and 5.3). Its
body is a JSON object whose `model` names the pool as a request's does (`rhi_gateway.body.pool_named`):
a configured pool's name, or a `claude-` name for the default pool; anything else is
`request_refused`, and a pool whose session is not active is `no_session`. Otherwise the answer is the pool's record, as the status gives it. When that record is unavailable for one of the hold's immediate causes the wake asks for
nothing; for any other pool, one unavailable for capacity included, the scaler is asked to run
(`rhi_gateway.waker.Waker.ask`, which the scaler retries) and the wake is recorded as an explicit
wake, demand until the pool is ready or 10 minutes have passed. The wake takes no concurrency
slot and writes no student's record.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from starlette.requests import Request
from starlette.responses import Response

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool
from rhi_common.records import Mode, ScalerNote
from rhi_gateway import routes, telemetry
from rhi_gateway.app import Handler, RequestBody
from rhi_gateway.auth import Authenticator, authenticated
from rhi_gateway.body import MODEL_FIELD, parse, pool_named, read_body
from rhi_gateway.errors import GatewayError, WireFormat, render_json
from rhi_gateway.hold import NO_CAPACITY_SECONDS, End, Hold
from rhi_gateway.keystore import KeyRecord
from rhi_gateway.readiness import PoolView
from rhi_gateway.routes import Route
from rhi_gateway.settings import NUMERIC_DEFAULTS, SESSION_MAX_HOURS
from rhi_gateway.waker import Waker

# The mode a pool is in before the admin writes one, by its `wake` (section 11.3).
_CONFIGURED_MODE = {"on-demand": Mode.ENABLED, "scheduled-only": Mode.SCHEDULED_ONLY}
# The causes the hold's rule gives at once, before any deadline.
_IMMEDIATE = frozenset({Cause.POOL_DISABLED, Cause.POOL_SCHEDULED_ONLY})


class State(StrEnum):
    """A pool's state as the status gives it (section 5.3)."""

    READY = "ready"
    WARMING = "warming"
    COLD = "cold"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PoolStatus:
    """One pool's record in the status. `cause` is set exactly when the state is `unavailable`."""

    model: str
    state: State
    cause: Cause | None = None

    def to_json(self) -> dict[str, str]:
        record = {"model": self.model, "state": self.state.value}
        if self.cause is not None:
            record["cause"] = self.cause.value
        return record


def _recently_found_none(note: ScalerNote | None, now: float) -> bool:
    # Compared as the hold compares it at its deadline.
    found_none_at = None if note is None else note.no_capacity_at
    return found_none_at is not None and now - found_none_at <= NO_CAPACITY_SECONDS


def pool_status(pool: Pool, view: PoolView, now: float) -> PoolStatus:
    """The state of one pool at the time `now` (seconds since the epoch), from its kept view."""
    mode = _CONFIGURED_MODE[pool.wake] if view.mode is None else view.mode.mode
    # One step of a hold that starts now: what a request arriving now would be told at once.
    first = Hold(pool, WireFormat.MESSAGES, streams=True, started_at=now).step(
        now, mode=mode, note=view.note, ready=view.ready
    )
    for action in first:
        if isinstance(action, End) and action.cause in _IMMEDIATE:
            return PoolStatus(pool.name, State.UNAVAILABLE, action.cause)
    if view.ready:
        return PoolStatus(pool.name, State.READY)
    if _recently_found_none(view.note, now):
        return PoolStatus(pool.name, State.UNAVAILABLE, Cause.CAPACITY_UNAVAILABLE)
    if view.note is not None and view.note.capacity_asked > 0:
        return PoolStatus(pool.name, State.WARMING)
    return PoolStatus(pool.name, State.COLD)


def _json(body: object) -> Response:
    return Response(
        content=json.dumps(body, separators=(",", ":")).encode("ascii"),
        status_code=200,
        media_type="application/json",
    )


@dataclass(frozen=True)
class LocalRoutes:
    """What the local routes are made of: who is let in, the configured pools and the kept views.

    `default_pool` is the pool of the `claude-` names, the one the body filters are given, or None
    when the deployment has none. `views` gives the kept view of every pool
    (`rhi_gateway.readiness.Readiness.views`); a pool it does not hold yet reads as not ready with
    no records. `clock` gives the seconds since the epoch, the clock of the scaler's note and of a
    session's start and end.

    `waker` is the wake's: without it the wake is not served here. `session_max_hours` is the
    longest a session may last, the configuration's, under which each pool's session is read.
    """

    authenticator: Authenticator
    pools: tuple[Pool, ...]
    views: Callable[[], Mapping[str, PoolView]]
    default_pool: Pool | None = None
    clock: Callable[[], float] = time.time
    waker: Waker | None = None
    session_max_hours: int = NUMERIC_DEFAULTS[SESSION_MAX_HOURS]

    def __post_init__(self) -> None:
        if self.default_pool is not None and self.default_pool not in self.pools:
            raise ValueError("the default pool is not one of the configured pools")

    def status_of(self, pool: Pool) -> PoolStatus:
        """One pool's record now: unavailable with `no_session` exactly when its own session is not active."""
        view, now = self.views().get(pool.name, PoolView()), self.clock()
        if not view.in_session(now, session_max_hours=self.session_max_hours):
            return PoolStatus(pool.name, State.UNAVAILABLE, Cause.NO_SESSION)
        return pool_status(pool, view, now)

    def statuses(self) -> list[PoolStatus]:
        """One record per configured pool, in the configuration's order."""
        return [self.status_of(pool) for pool in self.pools]

    async def models(self, request: Request, entry: Route, student: KeyRecord) -> Response:
        default = None if self.default_pool is None else self.default_pool.name
        return _json(
            {
                "object": "list",
                "data": [
                    {"id": pool.name, "object": "model", "type": "model", "default": pool.name == default}
                    for pool in self.pools
                ],
                "default": default,
            }
        )

    async def status(self, request: Request, entry: Route, student: KeyRecord) -> Response:
        return _json({"pools": [record.to_json() for record in self.statuses()]})

    async def wake(self, request: Request, entry: Route, student: KeyRecord) -> Response:
        waker = self.waker
        if waker is None:
            raise RuntimeError("the wake is served only with a waker")
        try:
            body = parse(await read_body(RequestBody(request.receive)))
            pool = pool_named(body.get(MODEL_FIELD), self.pools, self.default_pool)
            telemetry.note_pool(pool.name)
            record = self.status_of(pool)
            if record.cause is Cause.NO_SESSION:
                # No session on the pool the body names: nothing is recorded and the scaler is asked for nothing.
                raise GatewayError(Cause.NO_SESSION)
        except GatewayError as refused:
            telemetry.note_cause(refused.cause)
            answer = render_json(refused, WireFormat.MESSAGES)
            return Response(content=answer.body, status_code=answer.status, headers=answer.headers)
        if record.cause is not None:
            # The cause the wake's answer states is the request's cause in its log record.
            telemetry.note_cause(record.cause)
        if record.cause not in _IMMEDIATE:
            waker.record(pool.name)
            waker.ask(pool.name)
        return _json(record.to_json())

    def handlers(self) -> dict[Route, Handler]:
        """The handlers of the local routes: all but the hello behind authentication, the wake when it is given the waker."""
        served: dict[Route, Handler] = {
            routes.MODELS: authenticated(self.authenticator, self.models),
            routes.STATUS: authenticated(self.authenticator, self.status),
            routes.HELLO: hello,
        }
        if self.waker is not None:
            served[routes.WAKE] = authenticated(self.authenticator, self.wake)
        return served


async def hello(request: Request, entry: Route) -> Response:
    """The hello: 200 and no body, for anyone. It reads nothing."""
    return Response(status_code=200)
