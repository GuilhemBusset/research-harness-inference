"""Keep a student on one instance of a pool (system design, section 5.5).

An agent sends its whole transcript again on every turn, and the engine has what it computed for
that transcript only on the instance that served the turn before. Each pool's target group
therefore has duration-based stickiness: the internal load balancer names the target it chose in
a cookie on its answer, and a request that carries the cookie goes to that target (evidence E13).

The gateway is the load balancer's client, so the gateway keeps the cookie, one per student and
pool, on the student's budget record. The write that reserves budget returns it and the write that
settles stores it (`rhi_gateway.budget`), so every gateway task sees the same cookie and a
student's parallel requests follow the same one. This module is one request's part in that:

- `Affinity(cookie)` is made from the cookie the reservation returned, `None` if the record holds
  none for the pool;
- `headers()` is what the proxy adds to the request it sends: a `Cookie` header with that cookie,
  and nothing when there is none, so that the load balancer chooses a target itself;
- `answered(...)` takes the cookie from the `Set-Cookie` headers of the load balancer's answer;
- `ended` is the cookie the request ended with, which the settlement stores: the one the answer
  set, and the one sent when it set none.

**The cookie** is the one named `AWSALB`. The load balancer generates and encrypts it, sets it
again on every answer with a new expiry, and replaces it when the target it names has left. Its
value is opaque: it is kept as the load balancer set it, without the attributes that follow it
(`Expires`, `Path`), and the gateway does not know which instance a student is on. No other
cookie is taken: not `AWSALBCORS`, which the load balancer sets beside it for browsers, and
nothing an engine might set. A value that is not a cookie's (empty, with a space, a quote, a
comma or a control character, or longer than 4,096 bytes) is not taken either, so that what the
record holds can always be sent back.

The cookie never reaches the student: the proxy writes the client's answer itself and copies none
of the upstream's headers into it (`rhi_gateway.proxy`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# The cookie of the load balancer's duration-based stickiness.
COOKIE_NAME = "AWSALB"

# A cookie's value as RFC 6265 writes it (`cookie-octet`), and no longer than a cookie may be.
_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}", re.ASCII)


def cookie_set(set_cookie: Iterable[str]) -> str | None:
    """The load balancer's cookie among the values of an answer's `Set-Cookie` headers, if one sets it."""
    found = None
    for header in set_cookie:
        name, _, value = header.split(";", 1)[0].partition("=")
        if name.strip() == COOKIE_NAME and _VALUE.fullmatch(value.strip()):
            found = value.strip()
    return found


class Affinity:
    """One request's cookie: the one to send, and the one the request ends with."""

    def __init__(self, cookie: str | None = None) -> None:
        self._sent = cookie
        self._set: str | None = None

    def headers(self) -> dict[str, str]:
        """The headers that carry the cookie to the load balancer: none when there is no cookie."""
        return {} if self._sent is None else {"Cookie": f"{COOKIE_NAME}={self._sent}"}

    def answered(self, set_cookie: Iterable[str]) -> None:
        """Take the cookie the load balancer's answer sets, from the values of its `Set-Cookie` headers."""
        self._set = cookie_set(set_cookie) or self._set

    @property
    def ended(self) -> str | None:
        """The cookie the request ended with: the one the answer set, else the one it was sent."""
        return self._set or self._sent
