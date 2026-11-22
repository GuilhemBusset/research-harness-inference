"""What the agent does while its instance serves: the heartbeat, the release and the cache capacity
it reports, and liveness by inference (system design, sections 4.1, 4.3, 5.1 and 7.2).

**The heartbeat.** Every 5 seconds the agent reads the engine's running and waiting requests from
the engine's `/metrics` on localhost, under the names of the engine's row in
`rhi_common.engine_metrics`, and rewrites the instance's row in the heartbeats table that the
settings of the release parameter name (`rhi_common.records.InstanceHeartbeat`). The row is the
scaler's view of the engine's load. It also carries the release the instance serves, the digest of
the engine image and the model revision, because the engine does not know its own image digest
(section 4.1, the identity row). A pool that serves no model has no revision, and its row says
`NO_MODEL`.

The counts are the engine's or nothing: while its metrics cannot be read, as during its restart, no
row is written. The scaler ignores a row older than 20 seconds, and does not remove an instance it
does not know to be idle.

**The release, as a metric.** `rhi_lifecycle_release_info` is 1, and its labels `image_digest` and
`model_revision` carry what the heartbeat carries. The pipeline's verification and the deployments
dashboard read it.

**The cache capacity, as a metric.** The answer of the engine's `/metrics` that each heartbeat
fetches also goes to `rhi_lifecycle.cache`, which reads the engine's cache capacity in tokens where
the engine's row says and exports it as a number. The reading starts with the heartbeat, so once
the engine serves, and an answer that gives no capacity takes the metric away: the agent never
reports a capacity it did not just read.

**Liveness by inference.** An engine can hang with its health route still answering (evidence
E23). So every minute, and first when the duty starts, the agent reads the pool's secret from
Secrets Manager, by the ID the settings give, and sends the engine a completion of one token. An
answer may take `PROBE_SECONDS`. Without one, the agent restarts the engine's unit through
`rhi_lifecycle.host`, counts the restart in `rhi_lifecycle_engine_restarts_total` and logs it.

Two cases send no request, and so restart nothing. After a restart the engine loads its model,
which can take longer than a minute, and a request sent then would restart it again: the agent
first waits for the health route to answer. And when the secret cannot be read, nothing has shown
the engine to hang: the minute is skipped and logged.

`Serving.run` does both duties until it is cancelled, each on its own cadence: a completion that
waits for its answer does not delay a heartbeat. Every call that waits (the engine, AWS, the host)
runs in a worker thread. Nothing here repeats what AWS or the engine answered.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import CollectorRegistry, Counter, Gauge
from prometheus_client.parser import text_string_to_metric_families

from rhi_common.engine_metrics import Quantity, metric_names
from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import launch, metrics
from rhi_lifecycle.cache import Capacity
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.release import Release, Settings

# How often the row is rewritten (section 17).
HEARTBEAT_SECONDS = 5.0
# How long the engine's metrics may take to answer: less than the time between two rows.
METRICS_SECONDS = 2.0
# How often the engine is sent a completion (section 4.3).
PROBE_INTERVAL_SECONDS = 60.0
# How long its answer may take. The design gives no figure: long enough for one token from an
# engine that is busy, and short enough that the restart comes within the minute.
PROBE_SECONDS = 30.0

# The stage an instance in service has reached, as its heartbeat names it.
STAGE = "serving"
# The model revision of a pool that serves no model, as the row of a launch names it too.
NO_MODEL = launch.NO_MODEL

RELEASE_METRIC = "rhi_lifecycle_release_info"
# Its labels: the release the instance serves, as the heartbeat names it.
RELEASE_LABELS = ("image_digest", "model_revision")
RESTARTS_METRIC = "rhi_lifecycle_engine_restarts"

_LOG = logging.getLogger(__name__)


def read_load(exposition: bytes, engine: str) -> tuple[int, int] | None:
    """The running and the waiting requests in an answer of the engine's `/metrics`, under the
    names of `engine`'s row. None when the answer does not give both. An engine that labels its
    series, as vLLM does by model, has one sample per label set: they are summed."""
    names = metric_names(engine)
    wanted = (names[Quantity.RUNNING_REQUESTS], names[Quantity.WAITING_REQUESTS])
    totals: dict[str, float] = {}
    try:
        for family in text_string_to_metric_families(exposition.decode("utf-8")):
            for sample in family.samples:
                if sample.name in wanted:
                    totals[sample.name] = totals.get(sample.name, 0.0) + sample.value
    except ValueError:  # a text that is not UTF-8, or not in Prometheus' format
        return None
    counts = [totals.get(name) for name in wanted]
    if any(count is None or not math.isfinite(count) or count < 0 for count in counts):
        return None
    running, waiting = (round(count) for count in counts if count is not None)
    return running, waiting


class Serving:
    """One instance's duties while it serves. `served` is the release the instance holds, and
    `settings` the settings of its pool's release parameter. `engine` names the engine's row of
    metric names. `dynamodb` is boto3's DynamoDB resource and `secrets` its Secrets Manager
    client. The agent's metrics go to `registry`. `clock` gives the seconds since the epoch and
    `sleep` waits on it: a test gives its own, and its own `fetch` in place of the network."""

    def __init__(
        self,
        served: Release,
        settings: Settings,
        instance: str,
        engine: str,
        *,
        dynamodb: Any,
        secrets: Any,
        host: Host,
        fetch: launch.Fetch = launch.fetch,
        registry: CollectorRegistry = metrics.REGISTRY,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        metric_names(engine)  # an engine with no row is refused here, not at every heartbeat
        self._settings = settings
        self._instance = instance
        self._engine_name = engine
        self._image_digest, self._model_revision = launch.identity(served)
        self._table = dynamodb.Table(settings.heartbeat_table)
        self._secrets = secrets
        self._host = host
        self._fetch = fetch
        self._engine = launch.Engine(fetch)
        self._clock = clock
        self._sleep = sleep
        # Whether the engine's load could be read at the last heartbeat: a change is logged once.
        self._readable = True
        # Whether the engine was restarted and has not answered its health route since.
        self._starting = False
        Gauge(
            RELEASE_METRIC,
            "The release the instance serves: the engine image digest and the model revision.",
            RELEASE_LABELS,
            registry=registry,
        ).labels(image_digest=self._image_digest, model_revision=self._model_revision).set(1)
        self._restarts = Counter(
            RESTARTS_METRIC, "Restarts of the engine after a completion it did not answer.", registry=registry
        )
        self._capacity = Capacity(engine, registry)

    async def run(self) -> None:
        """Do both duties until cancelled."""
        async with asyncio.TaskGroup() as duties:
            duties.create_task(self.report())
            duties.create_task(self.watch())

    async def report(self) -> None:
        """Write the heartbeat every 5 seconds until cancelled."""
        await self._every(HEARTBEAT_SECONDS, self.beat)

    async def watch(self) -> None:
        """Check the engine by inference every minute until cancelled."""
        await self._every(PROBE_INTERVAL_SECONDS, self.check)

    async def _every(self, interval: float, duty: Callable[[], object]) -> None:
        next_at = self._clock()
        while True:
            await asyncio.to_thread(duty)
            # A fixed cadence: the time the duty takes is not added to the interval.
            next_at = max(next_at + interval, self._clock())
            await self._sleep(next_at - self._clock())

    def beat(self) -> bool:
        """Write one heartbeat row with the engine's load now, and say whether it was written.
        The same answer of the engine gives the cache capacity the agent reports."""
        answer = self._fetch(urllib.request.Request(f"{launch.ENGINE_URL}/metrics"), METRICS_SECONDS)
        self._capacity.read(answer)
        load = read_load(answer[1], self._engine_name) if answer is not None and answer[0] == 200 else None
        if (load is not None) != self._readable:
            self._readable = load is not None
            _LOG.warning("the engine's load %s", "can be read again" if self._readable else "cannot be read")
        if load is None:
            return False
        row = InstanceHeartbeat(
            instance=self._instance,
            pool=self._settings.pool,
            running=load[0],
            waiting=load[1],
            image_digest=self._image_digest,
            model_revision=self._model_revision,
            stage=STAGE,
            written_at=int(self._clock()),
        )
        try:
            self._table.put_item(Item=row.to_item())
        except (BotoCoreError, ClientError) as error:
            _LOG.warning("the heartbeat was not written (%s)", type(error).__name__)
            return False
        return True

    def check(self) -> bool:
        """Send the engine a completion of one token and restart its unit when none comes back.
        Says whether the engine was restarted."""
        if self._starting:
            if not self._engine.healthy():
                return False
            self._starting = False
        secret = self._secret()
        if secret is None:
            _LOG.warning("the pool's secret cannot be read: the engine is not checked this minute")
            return False
        # The pool's name is the model name the engine serves (section 4.2).
        if self._engine.warm_up(self._settings.pool, secret, timeout=PROBE_SECONDS):
            return False
        try:
            self._host.restart_engine()
        except HostError:
            _LOG.error("the engine did not answer a completion, and its unit could not be restarted")
            return False
        self._starting = True
        self._restarts.inc()
        _LOG.warning("the engine did not answer a completion: its unit was restarted")
        return True

    def _secret(self) -> str | None:
        """The pool's bearer secret, as it is now, or None when it cannot be read."""
        try:
            secret = self._secrets.get_secret_value(SecretId=self._settings.secret_id)["SecretString"]
        except (BotoCoreError, ClientError, KeyError, TypeError):
            return None
        return secret if isinstance(secret, str) and secret else None
