"""Keep a student on one instance of a pool (system design, section 5.5).

An agent sends its whole transcript again on every turn, and the engine has what it computed for
that transcript only on the instance that served the turn before. Each pool's target group
therefore has duration-based stickiness: the internal load balancer names the target it chose in
a cookie on its answer, and a request that carries the cookie goes to that target (evidence E13).

The gateway is the load balancer's client, so the gateway keeps the cookie, one per student and
pool, on the student's usage record. The write that takes the concurrency slot returns it and the
write that releases it stores it (`rhi_gateway.budget`), so every gateway task sees the same cookie and a
student's parallel requests follow the same one. This module is one request's part in that:

- `Affinity.judged(...)` is made from the cookie the reservation returned, `None` if the record
  holds none for the pool, and decides whether this request sends it (see "Rebalancing");
- `headers()` is what the proxy adds to the request it sends: a `Cookie` header with that cookie,
  and nothing when there is none, so that the load balancer chooses a target itself;
- `answered(...)` takes the cookie from the `Set-Cookie` headers of the load balancer's answer;
- `ended` is the cookie the request ended with, which the settlement stores: the one the answer
  set, and the one sent when it set none;
- `number` and `dropped` are what the settlement stores of the judgement.

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

**Rebalancing.** Stickiness alone would leave a new instance empty, and one instance queuing while
another idles. So the scaler raises the pool's rebalance number while the pool is lopsided, and
the gateway, which reads the number with the scaler's note (`rhi_gateway.readiness`), drops the
cookie of a share of the pool's students for each new number: the pool's `rebalance_share`, about
one student in ten. A request is judged when its reservation returns, on the number the gateway
then holds:

- the student's record keeps, per pool, the last number applied (`rebalance_applied`), which the
  reservation returns with the cookie. A request whose number equals it sends the cookie: each
  number acts once on a student;
- a number that differs is new to the student, and `selects` decides: a hash of the student and
  the number, below the share. It depends on nothing else, so every gateway task makes the same
  choice, and each number chooses its own students;
- a student chosen sends no cookie on this request. Least-outstanding routing sends it to the
  emptiest instance, whose answer sets the cookie the student then follows;
- the settlement stores the number, chosen or not, in the write that stores the cookie
  (`rhi_gateway.budget`). A request chosen that ended with no cookie (it never reached the load
  balancer, or its answer set none) takes the dropped one off the record there, so that the number
  still acts.

A student who sent nothing while the number rose several times is judged once, on the last: the
numbers in between belonged to an imbalance that student was not part of. A record that has
applied no number reads as 0, the number of a pool the scaler has never raised, so such a pool
moves no one. While the gateway holds no note for the pool it knows no number: the request is not
judged, and its settlement leaves the record's number as it is. The gateway cannot aim: the cookie
is opaque, so a student dropped from the emptier instance simply returns to it.

Two tasks can hold different numbers for as long as a readiness cycle after the scaler raises one
(5 seconds). A student whose requests reach both in that time is judged at each change of number
the record sees, so the new number can act on them more than once, and each time they go to the
emptiest instance. Of a student's requests in flight at once, each is judged on the record as its
reservation found it, and the last settlement's number and cookie stay.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Self

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


def selects(student: int, number: int, share: float) -> bool:
    """Whether a rebalance number moves a student: for `share` of the students, chosen by a hash.

    `student` is the student's GitHub ID. The hash is of the two numbers only, so the answer is
    the same on every gateway task and at every request.
    """
    digest = hashlib.sha256(f"{student}/{number}".encode()).digest()
    # The first 8 bytes as a fraction of their range: below 1, so a share of 1 selects everyone.
    return int.from_bytes(digest[:8], "big") < share * 2**64


class Affinity:
    """One request's cookie: the one to send, and the one the request ends with.

    `number` is the rebalance number the request was judged on, `None` for a request that was
    not judged, and `dropped` whether the judgement dropped the record's cookie.
    """

    def __init__(self, cookie: str | None = None) -> None:
        self._sent = cookie
        self._set: str | None = None
        self.number: int | None = None
        self.dropped = False

    @classmethod
    def judged(cls, student: int, cookie: str | None, *, applied: int | None, number: int | None, share: float) -> Self:
        """One request's affinity, judged on the pool's rebalance number (see "Rebalancing").

        `student` is the student's GitHub ID. `cookie` and `applied` are what the reservation
        returned for the pool: the cookie, and the last rebalance number applied. `number` is the
        pool's number as the gateway holds it, `None` while it holds no note, and `share` the
        pool's `rebalance_share`. The cookie is dropped when the number is new to the student
        and selects them.
        """
        affinity = cls(cookie)
        affinity.number = number
        if cookie is not None and number is not None and number != (applied or 0) and selects(student, number, share):
            affinity._sent = None
            affinity.dropped = True
        return affinity

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
