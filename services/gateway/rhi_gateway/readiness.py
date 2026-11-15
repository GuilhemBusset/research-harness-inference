"""What a gateway task knows of each pool, read every 5 seconds (system design, sections 5.1, 5.3, 11.3).

A pool is ready when its target group has at least one healthy target: the internal load balancer
establishes that by checking each engine directly, and the gateway reads it through the AWS API.
The gateway never probes through the load balancer, which forwards the four inference routes and
nothing else (section 5.3).

On the same cycle it reads the pool's two records in the pool-state table: `mode`, the admin's
(`rhi_common.records.PoolState`, section 11.3), and `note`, the scaler's (`ScalerNote`: whether
it asked for capacity, and when a launch last found none).

`Readiness.run` repeats the cycle every 5 seconds for every pool, whether or not anyone asks, and
keeps the last values. Callers read only the kept values (`Readiness.pool`): no request causes a
read of the AWS API or of the table. A failed health read keeps the pool not ready; a failed read
of a record keeps the value the previous cycle read. Nothing here sends a request through the
load balancer: the only calls are `describe_target_health` and the table's `get_item`.

The clients are those of the AWS SDK, given by whoever builds the gateway: `elbv2` is
`boto3.client("elbv2")`, `table` is `boto3.resource("dynamodb").Table(...)` on the pool-state
table. The tests give doubles.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from rhi_common.records import MODE_RECORD, NOTE_RECORD, PoolState, ScalerNote
from rhi_common.tables import POOL_STATE

# How often every pool is read (section 5.3).
INTERVAL_SECONDS = 5.0

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolView:
    """What the last cycle left for one pool.

    `ready` is whether the target group had a healthy target, and `observed_at` when that answer
    came back (the moment the gateway learnt it), `None` before the first read. `mode` and `note`
    are the last records read, `None` when the table holds none (or nothing was read yet).
    """

    ready: bool = False
    mode: PoolState | None = None
    note: ScalerNote | None = None
    observed_at: float | None = None


# Called after each cycle with the new views.
Subscriber = Callable[[Mapping[str, PoolView]], None]


class Readiness:
    """The kept view of every pool, refreshed by `run`.

    `target_groups` maps each pool's name to its target group (the identifier
    `describe_target_health` takes).
    """

    def __init__(
        self,
        target_groups: Mapping[str, str],
        *,
        elbv2: Any,
        table: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._target_groups = dict(target_groups)
        self._elbv2 = elbv2
        self._table = table
        self._clock = clock
        self._views: Mapping[str, PoolView] = MappingProxyType({pool: PoolView() for pool in self._target_groups})
        self._subscribers: list[Subscriber] = []

    @property
    def pools(self) -> tuple[str, ...]:
        return tuple(self._target_groups)

    def pool(self, name: str) -> PoolView:
        """The kept view of one pool. Reads nothing; a pool the gateway was not given raises `KeyError`."""
        return self._views[name]

    def views(self) -> Mapping[str, PoolView]:
        """The kept view of every pool, as one consistent snapshot."""
        return self._views

    def subscribe(self, subscriber: Subscriber) -> None:
        """Call `subscriber` after every cycle, with the new views."""
        self._subscribers.append(subscriber)

    def refresh(self) -> None:
        """One cycle: read every pool, then keep the values and tell the subscribers."""
        self._apply(self._read_all())

    async def run(self, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
        """Repeat the cycle every 5 seconds until cancelled. The blocking reads run in a worker thread."""
        next_at = self._clock()
        while True:
            self._apply(await asyncio.to_thread(self._read_all))
            # A fixed cadence: the time a cycle takes is not added to the interval.
            next_at = max(next_at + INTERVAL_SECONDS, self._clock())
            await sleep(next_at - self._clock())

    def _read_all(self) -> dict[str, PoolView]:
        previous = self._views
        views = {}
        for pool, target_group in self._target_groups.items():
            before = previous[pool]
            ready = self._read_health(pool, target_group)
            observed_at = self._clock()
            views[pool] = PoolView(
                ready=ready,
                mode=self._read_record(pool, MODE_RECORD, PoolState.from_item, before.mode),
                note=self._read_record(pool, NOTE_RECORD, ScalerNote.from_item, before.note),
                observed_at=observed_at,
            )
        return views

    def _apply(self, views: dict[str, PoolView]) -> None:
        # One assignment: a caller sees either the previous cycle or this one, never a mix.
        self._views = MappingProxyType(views)
        for subscriber in self._subscribers:
            try:
                subscriber(self._views)
            except Exception:  # noqa: BLE001 - a failing subscriber must not stop the reads
                _LOG.exception("a readiness subscriber failed")

    def _read_health(self, pool: str, target_group: str) -> bool:
        try:
            answer = self._elbv2.describe_target_health(TargetGroupArn=target_group)
            return any(
                description.get("TargetHealth", {}).get("State") == "healthy"
                for description in answer.get("TargetHealthDescriptions", [])
            )
        except Exception as error:  # noqa: BLE001 - any failed read leaves the pool not ready
            _LOG.warning("health read failed for pool %s (%s)", pool, type(error).__name__)
            return False

    def _read_record[R](
        self, pool: str, record: str, parse: Callable[[Mapping[str, object]], R], kept: R | None
    ) -> R | None:
        # The pool-state table's key: `pool`, then `record` (rhi_common.records).
        key = {POOL_STATE.partition_key.name: pool, "record": record}
        try:
            item = self._table.get_item(Key=key).get("Item")
            # No item is an answer: the record does not exist. An item that is not the record is not.
            return None if item is None else parse(item)
        except Exception as error:  # noqa: BLE001 - any failed read keeps the previous value
            _LOG.warning("read of record %s failed for pool %s (%s)", record, pool, type(error).__name__)
            return kept
