"""This gateway task's count of requests in flight, per pool (system design, section 7.2).

The gateway once told the scaler its demand in rows of the heartbeats table. A request starts no
machine now, and the scaler reads no demand (sections 5.1 and 5.3), so nothing here writes a row:
this module has no table and no loop. What is left is the count a gauge of the gateway reads
(`rhi_gateway.experience`): the requests this task has forwarded to each pool and that have not
ended.

`Heartbeat.received` opens a request, in flight from that moment, since no request is held; the
returned `Demand` stops counting it at its end. Only the forwarded routes open one, and token
counting does not (`rhi_gateway.app`): status calls, token counting, the models and the hello are
not counted.

`rows` gives the counts in the shape the gauge reads, one `rhi_common.records.GatewayHeartbeat`
per configured pool under this task's ID, with no request held and no time of a last request.
Reading it writes nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from rhi_common.records import GatewayHeartbeat


class _Pool:
    """What this task holds of one pool."""

    def __init__(self) -> None:
        self.in_flight = 0


class Demand:
    """One request's part in the count: in flight, then nothing once it has ended."""

    def __init__(self, pool: _Pool) -> None:
        self._pool = pool
        self._open = True
        pool.in_flight += 1

    def end(self) -> None:
        """The request has ended: it no longer counts. Once, however often called."""
        if not self._open:
            return
        self._open = False
        self._pool.in_flight -= 1


class Heartbeat:
    """The requests in flight of every pool this task serves, under `task`.

    `pools` names every configured pool: each has its count, zero for a pool with no request.
    `clock` gives the seconds since the epoch: a test gives its own.
    """

    def __init__(self, task: str, pools: Iterable[str], *, clock: Callable[[], float] = time.time) -> None:
        if not task:
            raise ValueError("the count needs the task's ID")
        self._task = task
        self._pools = {pool: _Pool() for pool in pools}
        self._clock = clock

    def received(self, pool: str) -> Demand:
        """A request for `pool` was received: it counts as in flight until its `Demand` ends."""
        return Demand(self._pools[pool])

    def rows(self) -> list[GatewayHeartbeat]:
        """The counts now, one row per pool. Nothing is written anywhere."""
        now = int(self._clock())
        return [
            GatewayHeartbeat(
                task=self._task,
                pool=name,
                in_flight=kept.in_flight,
                held=0,
                last_request_at=None,
                written_at=now,
            )
            for name, kept in self._pools.items()
        ]
