"""The gateway's closed route table (system design, section 6.2, step 1).

The engine's own request surface is too large to hand to students unfiltered, and vLLM answers
routes such as `/invocations` and `/pause` without its key (section 4.1). The gateway therefore
serves the seven routes written here and nothing else: four are forwarded to the engine, three are
answered by the gateway itself, and any other method or path is the cause `not_found`.

A request is in the table when its method and its path equal an entry's, the path being compared
as the bytes the client sent. Nothing is decoded, normalized or folded before the comparison, so a
trailing slash, another letter case, a percent-encoded spelling or a dot segment is another path,
and is refused. The query string is not part of the path: it never makes a path match and never
stops one from matching (Claude Code sends `POST /v1/messages?beta=true`).

The upstream URL of a forwarded route is made by `upstream_url` from the engine's base URL and the
entry's own constant. That function has no parameter through which a request, a path or a query
could arrive, so nothing of the client's reaches the URL the gateway calls.

Nothing here reads configuration or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rhi_gateway.errors import WireFormat


class Kind(StrEnum):
    """What the gateway does with a route of the table."""

    FORWARDED = "forwarded"
    LOCAL = "local"


@dataclass(frozen=True)
class Route:
    """One entry of the table.

    A forwarded entry carries the path the gateway calls on the engine and the wire format of its
    requests; an entry the gateway answers itself carries neither.
    """

    method: str
    path: str
    upstream_path: str | None = None
    wire_format: WireFormat | None = None

    @property
    def kind(self) -> Kind:
        return Kind.LOCAL if self.upstream_path is None else Kind.FORWARDED


# Forwarded. The upstream paths are the inference routes of the engine contract (section 4.1).
MESSAGES = Route("POST", "/v1/messages", "/v1/messages", WireFormat.MESSAGES)
COUNT_TOKENS = Route("POST", "/v1/messages/count_tokens", "/v1/messages/count_tokens", WireFormat.MESSAGES)
CHAT_COMPLETIONS = Route("POST", "/v1/chat/completions", "/v1/chat/completions", WireFormat.CHAT_COMPLETIONS)
RESPONSES = Route("POST", "/v1/responses", "/v1/responses", WireFormat.RESPONSES)
# Answered locally.
MODELS = Route("GET", "/v1/models")
STATUS = Route("GET", "/status")
HELLO = Route("HEAD", "/api/hello")

TABLE: tuple[Route, ...] = (MESSAGES, COUNT_TOKENS, CHAT_COMPLETIONS, RESPONSES, MODELS, STATUS, HELLO)

_BY_REQUEST = {(route.method, route.path.encode("ascii")): route for route in TABLE}


def match(method: str, raw_path: bytes | None) -> Route | None:
    """The entry a request is for, or `None` when it is outside the table.

    `raw_path` is the path as the client sent it (the `raw_path` of an ASGI scope). A query string
    still attached to it is left out. A server that does not give the raw path gives `None`, and
    matches nothing: the decoded path cannot tell `/v1/messages` from an encoded spelling of it.
    """
    if raw_path is None:
        return None
    path, _, _ = raw_path.partition(b"?")
    return _BY_REQUEST.get((method, path))


def upstream_url(base_url: str, route: Route) -> str:
    """The URL the gateway calls for a forwarded route: the engine's base URL and the entry's constant.

    `base_url` is configuration (the address of a pool's engines), never something a client sent.
    """
    if route.upstream_path is None:
        raise ValueError("a route the gateway answers itself has no upstream URL")
    return base_url.rstrip("/") + route.upstream_path
