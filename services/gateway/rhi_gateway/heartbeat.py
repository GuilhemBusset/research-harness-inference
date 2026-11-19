"""This gateway task's demand, reported to the scaler every 5 seconds (system design, sections 5.1 and 17).

Each gateway task rewrites its own row per pool in the heartbeats table, under its task's ID
(`rhi_common.records.GatewayHeartbeat`): summing gauges across tasks would give wrong values, so
each task reports only what it holds, and the scaler sums the fresh rows.

Demand, per pool, is the task's requests in flight and requests held, and the time the last
request was received, which the rule that takes a pool to zero reads. `Heartbeat.received` opens
a request, as held or as in flight; the returned `Demand` moves it from held to in flight when
it is forwarded and stops counting it at its end. Only the forwarded routes open one, and token
counting does not (`rhi_gateway.app`): status calls, token counting, the models and the hello are
not demand (section 5.3).

An explicit wake is demand too (`rhi_gateway.waker`): each one pending counts as a request held,
and the moment its pool was found ready counts as a request received. One whose 10 minutes ran out
first stops counting and records nothing.

`run` writes the rows every 5 seconds, on a fixed cadence, until it is cancelled; a failed write
is logged and the next one is made on time. `stop` writes the last rows, with both counts at zero,
and no other row is written after them. `table` is boto3's table resource on the heartbeats table
(`boto3.resource("dynamodb").Table(...)`); the tests give a double.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from typing import Any

from rhi_common.records import GatewayHeartbeat
from rhi_gateway.waker import Waker

# How often the rows are rewritten (section 17).
INTERVAL_SECONDS = 5.0

_LOG = logging.getLogger(__name__)


class _Pool:
    """What this task holds of one pool."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.held = 0
        self.last_request_at: float | None = None


class Demand:
    """One request's part in the demand: held, then in flight, then nothing once it has ended."""

    def __init__(self, pool: _Pool, *, held: bool) -> None:
        self._pool = pool
        self._held = held
        self._open = True
        if held:
            pool.held += 1
        else:
            pool.in_flight += 1

    def forwarded(self) -> None:
        """The request is sent to its pool: from now on it is in flight. Nothing once it has ended or already is."""
        if self._open and self._held:
            self._held = False
            self._pool.held -= 1
            self._pool.in_flight += 1

    def end(self) -> None:
        """The request has ended: it no longer counts. Once, however often called."""
        if not self._open:
            return
        self._open = False
        if self._held:
            self._pool.held -= 1
        else:
            self._pool.in_flight -= 1


class Heartbeat:
    """The demand of every pool this task serves, and the loop that writes it under `task`.

    `pools` names every configured pool: each gets its row, a pool with no demand a row of zeros.
    `waker`, when given, adds its explicit wakes. `clock` gives the seconds since the epoch and
    `sleep` waits on it: a test gives its own.
    """

    def __init__(
        self,
        table: Any,
        task: str,
        pools: Iterable[str],
        *,
        waker: Waker | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if not task:
            raise ValueError("the heartbeat needs the task's ID")
        self._table = table
        self._task = task
        self._pools = {pool: _Pool() for pool in pools}
        self._waker = waker
        self._clock = clock
        self._sleep = sleep
        # Taken by every write: once `stop` has written its rows, nothing else is written.
        self._writing = threading.Lock()
        self._stopped = False

    def received(self, pool: str, *, held: bool) -> Demand:
        """A request for `pool` was received: it counts as held, or in flight, until its `Demand` ends."""
        kept = self._pools[pool]
        kept.last_request_at = self._clock()
        return Demand(kept, held=held)

    def rows(self) -> list[GatewayHeartbeat]:
        """The rows as they would be written now."""
        now = self._clock()
        return [self._row(name, kept, now) for name, kept in self._pools.items()]

    def _row(self, name: str, kept: _Pool, now: float) -> GatewayHeartbeat:
        held, last = kept.held, kept.last_request_at
        if self._waker is not None:
            held += self._waker.pending(name)
            woken = self._waker.ended_ready_at(name)
            if woken is not None and (last is None or woken > last):
                last = woken
        return GatewayHeartbeat(
            task=self._task,
            pool=name,
            in_flight=kept.in_flight,
            held=held,
            last_request_at=None if last is None else int(last),
            written_at=int(now),
        )

    async def run(self) -> None:
        """Write the rows every 5 seconds until cancelled. The writes run in a worker thread."""
        next_at = self._clock()
        while True:
            await asyncio.to_thread(self._write, self.rows(), last=False)
            # A fixed cadence: the time a write takes is not added to the interval.
            next_at = max(next_at + INTERVAL_SECONDS, self._clock())
            await self._sleep(next_at - self._clock())

    async def stop(self) -> None:
        """Write the last rows, with both counts at zero for every pool; nothing is written after them."""
        zeros = [replace(row, in_flight=0, held=0) for row in self.rows()]
        await asyncio.to_thread(self._write, zeros, last=True)

    def _write(self, rows: list[GatewayHeartbeat], *, last: bool) -> None:
        with self._writing:
            if self._stopped:
                return
            self._stopped = last
            for row in rows:
                try:
                    self._table.put_item(Item=row.to_item())
                except Exception as error:  # noqa: BLE001 - a failed write must not stop the next ones
                    _LOG.warning("heartbeat write failed for pool %s (%s)", row.pool, type(error).__name__)
