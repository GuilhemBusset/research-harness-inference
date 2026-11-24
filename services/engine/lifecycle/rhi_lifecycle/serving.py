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
answer may take `PROBE_SECONDS`. An engine that answers is alive, and is never restarted.

**A completion with no answer restarts the engine only when the engine has counted no token for a
minute.** A busy engine can take longer than the completion's limit to answer it: under long
contexts with requests queued, the completion waits its turn behind the queue. Restarting that
engine kills every request in flight and empties its cache, so the next completion is slower
still. Neither the health route nor the running and waiting counts tell a busy engine from a hung
one: a hung engine goes on reporting the requests it holds. What a hung or dead engine cannot do
is compute or generate a token. So the criterion is progress: the sum of the engine's input and
output token counters (section 4.1), read under the names of the engine's row from the answer of
`/metrics` that each heartbeat fetches. A heartbeat shows progress when it reads a sum greater
than the heartbeat before it read. One that reads no sum shows none, and neither does the one
after it, which has no sum to compare its own with: a metrics route that does not answer shows no
progress. When a completion fails and no heartbeat showed progress in the
`PROBE_INTERVAL_SECONDS` before, the agent restarts the engine's unit through `rhi_lifecycle.host`,
counts the restart in `rhi_lifecycle_engine_restarts_total` and logs it. When one did, the engine
is busy: nothing is restarted, and that is logged. The window is the minute between two
completions, not the completion's limit: an engine may count a request's input tokens only once
its prefill ends, so one long prefill can hold the counters still for tens of seconds while the
engine works. A hung engine is so restarted at most about a minute later than at its first
unanswered completion, which costs less than killing every request of a busy one.

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

from rhi_common.engine_metrics import Kind, Name, Quantity, metric_names
from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import launch, metrics
from rhi_lifecycle.cache import Capacity
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.release import Release, Settings

# How often the row is rewritten (section 17).
HEARTBEAT_SECONDS = 5.0
# How long the engine's metrics may take to answer: less than the time between two rows.
METRICS_SECONDS = 2.0
# How often the engine is sent a completion (section 4.3). It is also how long the engine's token
# counters may stand still before a completion with no answer restarts it.
PROBE_INTERVAL_SECONDS = 60.0
# How long its answer may take. The design gives no figure: short enough that the restart of a
# hung engine comes within the minute. It is not long enough for every busy engine: one whose
# requests queue behind long prefills can take longer for one token, which is why a completion
# with no answer restarts only an engine that counts no token.
PROBE_SECONDS = 30.0

# The stage an instance in service has reached, as its heartbeat names it.
STAGE = "serving"
# The model revision of a pool that serves no model, as the row of a launch names it too.
NO_MODEL = launch.NO_MODEL

RELEASE_METRIC = "rhi_lifecycle_release_info"
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


def read_tokens(exposition: bytes, engine: str) -> float | None:
    """The tokens the engine has counted, input and output together, in an answer of its
    `/metrics`, under the names of `engine`'s row. None when the answer does not give both
    counters. Samples of several label sets are summed, as in `read_load`."""
    names = metric_names(engine)
    wanted = [_sample(names[quantity]) for quantity in (Quantity.INPUT_TOKENS, Quantity.OUTPUT_TOKENS)]
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
    return sum(count for count in counts if count is not None)


def _sample(name: Name) -> str:
    """The name of the sample that carries a metric's value: a counter's has `_total` after the
    family's name (`rhi_common.engine_metrics`)."""
    return f"{name}_total" if name.kind is Kind.COUNTER else str(name)


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
        # The sum of the engine's token counters the last heartbeat read, None when it read none,
        # and when a heartbeat last read a sum greater than the heartbeat before it. The heartbeat
        # writes both from its thread, and the check reads the second from its own.
        self._tokens: float | None = None
        self._progressed_at: float | None = None
        Gauge(
            RELEASE_METRIC,
            "The release the instance serves: the engine image digest and the model revision.",
            ("image_digest", "model_revision"),
            registry=registry,
        ).labels(image_digest=self._image_digest, model_revision=self._model_revision).set(1)
        self._restarts = Counter(
            RESTARTS_METRIC,
            "Restarts of the engine after a completion it did not answer while it counted no token.",
            registry=registry,
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
        The same answer of the engine gives the cache capacity the agent reports, and says whether
        the engine has counted a token since the heartbeat before."""
        answer = self._fetch(urllib.request.Request(f"{launch.ENGINE_URL}/metrics"), METRICS_SECONDS)
        self._capacity.read(answer)
        exposition = answer[1] if answer is not None and answer[0] == 200 else None
        self._note_progress(None if exposition is None else read_tokens(exposition, self._engine_name))
        load = None if exposition is None else read_load(exposition, self._engine_name)
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

    def _note_progress(self, tokens: float | None) -> None:
        """Take the sum of the engine's token counters one heartbeat read, None when it read none,
        and note the time when it is greater than the sum the heartbeat before it read. After a
        heartbeat that read none there is nothing to compare with: tokens counted at some time
        while the metrics gave no answer are not tokens counted now."""
        if tokens is not None and self._tokens is not None and tokens > self._tokens:
            self._progressed_at = self._clock()
        self._tokens = tokens

    def _progressing(self) -> bool:
        """Whether a heartbeat read more tokens than the one before it in the last
        `PROBE_INTERVAL_SECONDS`: the engine works, however late it answers."""
        progressed_at = self._progressed_at
        return progressed_at is not None and self._clock() - progressed_at <= PROBE_INTERVAL_SECONDS

    def check(self) -> bool:
        """Send the engine a completion of one token. When none comes back and the engine has
        counted no token for a minute, restart its unit. Says whether the engine was restarted."""
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
        if self._progressing():
            # Busy, not hung: a restart would kill every request it runs (section 4.3).
            _LOG.warning("the engine did not answer a completion, and it counts tokens: it is not restarted")
            return False
        try:
            self._host.restart_engine()
        except HostError:
            _LOG.error("the engine did not answer a completion and counts no token: its unit could not be restarted")
            return False
        self._starting = True
        self._restarts.inc()
        _LOG.warning("the engine did not answer a completion and counts no token: its unit was restarted")
        return True

    def _secret(self) -> str | None:
        """The pool's bearer secret, as it is now, or None when it cannot be read."""
        try:
            secret = self._secrets.get_secret_value(SecretId=self._settings.secret_id)["SecretString"]
        except (BotoCoreError, ClientError, KeyError, TypeError):
            return None
        return secret if isinstance(secret, str) and secret else None
