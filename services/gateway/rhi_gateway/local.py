"""The routes the gateway answers itself: the models, the status and the hello (system design, sections 5.3, 6.2).

The three local routes of the table are answered here.

| Route | Key | Answer |
| --- | --- | --- |
| `GET /v1/models` | needed | the model name of every configured pool, and which one the `claude-` names map to |
| `GET /status` | needed | one record per configured pool: its model name, its state, when unavailable its cause, and while its session is active the time that session ends |
| `HEAD /api/hello` | none | 200 with no body |

Every student may use every configured pool, so both lists are the configuration's, in its order.
A request without a valid key is refused by `rhi_gateway.auth.authenticated` before anything here
runs, and a key that is not well formed is refused with no lookup.

A pool's state comes from what the gateway already keeps of it (`rhi_gateway.readiness`): its
session, whether it has a healthy target, and the scaler's note. Answering reads nothing else,
starts nothing and takes no concurrency slot (section 5.3).

| State | Cause | When |
| --- | --- | --- |
| `ready` | | the session is active and the pool has a healthy target |
| `warming` | | the session is active, no target is healthy and at least one instance is starting |
| `unavailable` | `capacity_unavailable` | the session is active, and no instance is in service or starting |
| `unavailable` | `no_session` | the pool's session is not active |

The rule is the request path's own (`rhi_gateway.readiness.PoolView.refusal`): the status says of
a pool what a request for it would be told now. A pool that would be answered `warming` is in the
state `warming`, and the two other causes make it `unavailable`. The record of a pool whose
session is active carries `session_ends_at`, the time that session ends in seconds since the
epoch, as every reader takes it (`rhi_common.sessions.end`); the record of any other pool carries
none. The models and the hello answer whatever the pools' states.
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
from rhi_gateway import routes
from rhi_gateway.app import Handler
from rhi_gateway.auth import Authenticator, authenticated
from rhi_gateway.keystore import KeyRecord
from rhi_gateway.readiness import PoolView
from rhi_gateway.routes import Route
from rhi_gateway.settings import NUMERIC_DEFAULTS, SESSION_MAX_HOURS


class State(StrEnum):
    """A pool's state as the status gives it (section 5.3)."""

    READY = "ready"
    WARMING = "warming"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PoolStatus:
    """One pool's record in the status.

    `cause` is set exactly when the state is `unavailable`, and `session_ends_at` (seconds since
    the epoch) exactly while the pool's session is active.
    """

    model: str
    state: State
    cause: Cause | None = None
    session_ends_at: int | None = None

    def to_json(self) -> dict[str, str | int]:
        record: dict[str, str | int] = {"model": self.model, "state": self.state.value}
        if self.cause is not None:
            record["cause"] = self.cause.value
        if self.session_ends_at is not None:
            record["session_ends_at"] = self.session_ends_at
        return record


def pool_status(pool: Pool, view: PoolView, now: float, *, session_max_hours: int) -> PoolStatus:
    """The state of one pool at the time `now` (seconds since the epoch), from its kept view.

    It is what a request for the pool would be told now (`PoolView.refusal`).
    """
    refusal = view.refusal(now, session_max_hours=session_max_hours)
    ends_at = view.session_end(now, session_max_hours=session_max_hours)
    if refusal is None:
        return PoolStatus(pool.name, State.READY, session_ends_at=ends_at)
    if refusal is Cause.WARMING:
        return PoolStatus(pool.name, State.WARMING, session_ends_at=ends_at)
    return PoolStatus(pool.name, State.UNAVAILABLE, refusal, session_ends_at=ends_at)


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
    no records. `clock` gives the seconds since the epoch, the clock of a session's start and end.
    `session_max_hours` is the longest a session may last, the configuration's, under which each
    pool's session is read.
    """

    authenticator: Authenticator
    pools: tuple[Pool, ...]
    views: Callable[[], Mapping[str, PoolView]]
    default_pool: Pool | None = None
    clock: Callable[[], float] = time.time
    session_max_hours: int = NUMERIC_DEFAULTS[SESSION_MAX_HOURS]

    def __post_init__(self) -> None:
        if self.default_pool is not None and self.default_pool not in self.pools:
            raise ValueError("the default pool is not one of the configured pools")

    def status_of(self, pool: Pool) -> PoolStatus:
        """One pool's record now, from its kept view. Reads nothing."""
        view = self.views().get(pool.name, PoolView())
        return pool_status(pool, view, self.clock(), session_max_hours=self.session_max_hours)

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

    def handlers(self) -> dict[Route, Handler]:
        """The handlers of the local routes: all but the hello behind authentication."""
        return {
            routes.MODELS: authenticated(self.authenticator, self.models),
            routes.STATUS: authenticated(self.authenticator, self.status),
            routes.HELLO: hello,
        }


async def hello(request: Request, entry: Route) -> Response:
    """The hello: 200 and no body, for anyone. It reads nothing."""
    return Response(status_code=200)
