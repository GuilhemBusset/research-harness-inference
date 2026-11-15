"""The wakes a gateway task asks for (system design, sections 5.1 and 5.3).

`Waker.ask` asks the scaler to run for a pool: an asynchronous invocation of the scaler function
(`InvocationType="Event"`, which Lambda queues and answers without running it), made from a worker
thread so that the caller does not wait even for that. A failed invocation is only logged: whoever
holds a request repeats the wake every 15 seconds (section 5.1).

The same module keeps the explicit wakes, those a student asked for with `POST /wake`. One counts
as demand until its pool is ready or 10 minutes have passed, whichever comes first (section 5.3).
The waker learns readiness from `rhi_gateway.readiness`: after every cycle that finds a pool ready,
the pool's pending wakes end, at the time of that cycle. `pending` says how many are still pending
for a pool and `ended_ready_at` when the last one ended with its pool ready. The hold, the local
routes and the demand report all read these, and none of them reads AWS.

`lambda_client` is the AWS SDK's (`boto3.client("lambda")`); the tests give a double.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from rhi_gateway.readiness import PoolView, Readiness

# How long an explicit wake counts as demand when its pool never turns ready (section 5.3).
EXPLICIT_WAKE_SECONDS = 600.0

_LOG = logging.getLogger(__name__)


class Waker:
    """Asks the scaler to run, and keeps the explicit wakes of every pool `readiness` knows."""

    def __init__(
        self,
        readiness: Readiness,
        *,
        lambda_client: Any,
        function_name: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._readiness = readiness
        self._lambda = lambda_client
        self._function_name = function_name
        self._clock = clock
        self._lock = threading.Lock()
        # The time each pending explicit wake was recorded, per pool.
        self._pending: dict[str, list[float]] = {pool: [] for pool in readiness.pools}
        self._ended_ready_at: dict[str, float | None] = dict.fromkeys(readiness.pools)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rhi-waker")
        readiness.subscribe(self._settle)

    def ask(self, pool: str) -> Future[None]:
        """Ask the scaler to run for `pool`, and return at once. The future is only for whoever wants to wait."""
        return self._executor.submit(self._invoke, pool)

    def record(self, pool: str) -> None:
        """Record an explicit wake for `pool`. On a pool already ready it ends at once."""
        now = self._clock()
        with self._lock:
            if self._readiness.pool(pool).ready:
                self._ended_ready_at[pool] = now
            else:
                self._pending[pool].append(now)

    def pending(self, pool: str) -> int:
        """How many explicit wakes for `pool` are still pending."""
        with self._lock:
            return len(self._unexpired(pool, self._clock()))

    def ended_ready_at(self, pool: str) -> float | None:
        """When the last explicit wake for `pool` ended with its pool ready; `None` if none did."""
        with self._lock:
            return self._ended_ready_at[pool]

    def _unexpired(self, pool: str, now: float) -> list[float]:
        kept = [at for at in self._pending[pool] if now - at < EXPLICIT_WAKE_SECONDS]
        self._pending[pool] = kept
        return kept

    def _settle(self, views: Mapping[str, PoolView], started: float) -> None:
        """After a readiness cycle: the wakes of every pool it found ready end, at the time of the cycle."""
        with self._lock:
            for pool, view in views.items():
                if view.ready and self._unexpired(pool, started):
                    self._pending[pool] = []
                    self._ended_ready_at[pool] = started

    def _invoke(self, pool: str) -> None:
        try:
            self._lambda.invoke(
                FunctionName=self._function_name,
                InvocationType="Event",
                Payload=json.dumps({"pool": pool}).encode(),
            )
        except Exception as error:  # noqa: BLE001 - whoever holds repeats the wake; nobody waits for this one
            _LOG.warning("the wake of pool %s was not passed to the scaler (%s)", pool, type(error).__name__)
