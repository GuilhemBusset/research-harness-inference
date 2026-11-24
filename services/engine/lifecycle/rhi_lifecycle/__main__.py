"""The lifecycle agent's program: it runs the agent's transitions and duties, in order, on one
instance (system design, section 4.3). The agent's unit runs it as `python -m rhi_lifecycle`.

**Its settings** come from its environment, which the agent's unit fills from
`/run/rhi/release.env`, as the boot program writes it: `RHI_RELEASE_PARAMETER`, `RHI_REGION` and
one variable for each setting of `rhi_lifecycle.release.Settings`, `RHI_POOL` to `RHI_SECRET_ID`.
A variable that is missing ends the program at once, with a message that names it. `RHI_ENGINE`
names the engine's row of metric names (`rhi_common.engine_metrics`), under which the heartbeat,
the check of liveness and the termination handling read the engine's `/metrics`. The boot program
writes it from the setting `engine` of the release parameter. An instance whose boot program
predates that setting has no such variable: the program then reads the engine from the instance's
tag `rhi-engine`, which the launch template gives every instance of the pool. Without either, the
row is the mock engine's, and a vLLM engine read under the mock's names gives no load.

**What it does.** It starts the metrics endpoint (`rhi_lifecycle.metrics`), then reads the
instance's target lifecycle state from the instance metadata every `TICK_SECONDS`. The design gives
no interval for it: 5 seconds is the heartbeat's own. A reading that fails is left for the next
one. What a reading calls for:

| The instance | Target state | What the program does |
| --- | --- | --- |
| not in service | `InService` or a warm-pool state, not yet acted on | runs the launch transition (`rhi_lifecycle.launch`), once |
| in service | `InService` | runs the serving duties (`rhi_lifecycle.serving`) |
| in service | any other | hands the instance to the termination handling (`rhi_lifecycle.termination`) |
| not in service | none: the metadata says the instance has no such item | runs the canary (`rhi_lifecycle.canary`), once |

Any other reading calls for nothing. An instance that was never in service and whose target state
is a terminated one is among them: no engine runs there to drain, and the program leaves it alone.

The instance is in service once a launch into service, a promotion or a launch straight there, has
completed its hook: the serving duties start then. They run until the target state leaves service,
and on while the termination handling waits for the engine to drain. A hook that could not be
completed leaves the instance in service, and the next reading hands it over again.

**An instance in no group.** The metadata of an instance that belongs to no Auto Scaling group has
no target lifecycle state, and says so: that is an answer, not a reading that failed. Such an
instance is a canary, which the promotion job started from the pool's launch template to try a
machine image. The program hands it to `rhi_lifecycle.canary`, which starts the engine as a launch
straight into service does and writes its outcome on the instance's console. That run completes no
hook and writes no heartbeat, and the program starts no serving duty after it, whatever its
outcome. It is run once in the life of the program. It decides nothing about later readings: one
that gives a target state is acted on as on any instance.

**One transition at a time, beside the readings.** A transition can take minutes, and the readings
go on every `TICK_SECONDS` while it runs. A reading that finds a transition under way calls for
nothing: the transition ends first, and the reading after it decides what comes next. That reading
is made at once when the transition has lasted beyond a reading, or has ended while one was made,
and at its own time otherwise, so a transition that fails at once is not run again before the next
one. The wait for a reading's moment starts in the turn of the event loop that read the clock for
it: the 5 seconds are counted from that moment.

**The duties at rest before the engine stops.** A duty does its work in a worker thread, and
cancelling the duty does not end a call that is under way there: a check of the engine that was
waiting for the pool's secret would go on, find the engine stopped, and restart it. So the
termination handling stops the engine's unit through a host that first brings the duties to rest
(`_Host`): once the termination hook is completed they are cancelled, and what they have in a
worker thread is waited for. Only then is the unit stopped, and nothing of the duties runs after.

After an entry to the warm pool the program runs no serving duty: the group stops the instance. An
instance that has left service is not taken for one that enters the pool: the state it left for
counts as acted on.

**A restart of its unit.** The launch transition writes a file before it completes a hook into
service (`launch.SERVED_FILE`, in `/run/rhi`, which is emptied at every stop). A program that
starts and finds the file is on an instance that already serves: it runs no launch transition, and
takes the instance as in service. The serving duties end only by failing, and the program then
ends with them, so that its unit restarts it and they resume.

Every call that waits (the instance metadata, a transition) runs in a worker thread, so the
readings and the serving duties keep their own cadence beside it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Self

import boto3

from rhi_common.engine_metrics import ENGINES, MOCK
from rhi_lifecycle import launch, metrics, release
from rhi_lifecycle.canary import Canary
from rhi_lifecycle.host import Host
from rhi_lifecycle.launch import Launch, Outcome
from rhi_lifecycle.serving import Serving
from rhi_lifecycle.termination import Termination

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping
    from pathlib import Path
    from wsgiref.simple_server import WSGIServer

    from prometheus_client import CollectorRegistry

# The time between two readings of the target lifecycle state.
TICK_SECONDS = 5.0

PARAMETER_VARIABLE = "RHI_RELEASE_PARAMETER"
REGION_VARIABLE = "RHI_REGION"
ENGINE_VARIABLE = "RHI_ENGINE"
# The instance's tag that names its pool's engine (`rhi.pools.group`).
ENGINE_TAG = "rhi-engine"

# Set in the tasks of the serving duties, and so wherever one of them hands a call to a worker thread.
_SERVING: contextvars.ContextVar[bool] = contextvars.ContextVar("serving", default=False)

_LOG = logging.getLogger(__name__)


class SettingsError(Exception):
    """The environment does not give the program its settings. The message names variables, never
    a value."""


@dataclass(frozen=True)
class Environment:
    """What the program reads from its environment. `parameter` is the name of the pool's release
    parameter, `region` the instance's, and `engine` names the engine's row of metric names."""

    parameter: str
    region: str
    engine: str
    settings: release.Settings

    @classmethod
    def read(cls, environ: Mapping[str, str], engine_tag: Callable[[], str | None] = lambda: None) -> Self:
        """The settings in `environ`. Raises `SettingsError` when a variable is missing or empty.
        `engine_tag` gives the instance's tag `rhi-engine`, asked only when `RHI_ENGINE` is missing."""
        setting_variables = {field.name: "RHI_" + field.name.upper() for field in fields(release.Settings)}
        missing = [
            variable
            for variable in (PARAMETER_VARIABLE, REGION_VARIABLE, *setting_variables.values())
            if not environ.get(variable)
        ]
        if missing:
            raise SettingsError("the environment lacks " + ", ".join(missing))
        engine = environ.get(ENGINE_VARIABLE) or engine_tag() or MOCK
        if engine not in ENGINES:
            raise SettingsError(f"{ENGINE_VARIABLE} names an engine with no metric names")
        settings = release.Settings(**{name: environ[variable] for name, variable in setting_variables.items()})
        return cls(environ[PARAMETER_VARIABLE], environ[REGION_VARIABLE], engine, settings)


class _Workers(ThreadPoolExecutor):
    """The event loop's worker threads, where `asyncio.to_thread` runs a call. They keep the calls
    of the serving duties that have not returned: cancelling a duty does not end the call it waits
    for, and the program waits for it before the engine is stopped."""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix="rhi-lifecycle")
        self._lock = threading.Lock()
        self._serving: set[Future[Any]] = set()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        # Called in the event loop, by the task that hands the call over: the context is its own.
        call = super().submit(fn, *args, **kwargs)
        if _SERVING.get():
            with self._lock:
                self._serving.add(call)
            call.add_done_callback(self._returned)
        return call

    def _returned(self, call: Future[Any]) -> None:
        with self._lock:
            self._serving.discard(call)

    def serving(self) -> list[Future[Any]]:
        """The calls of the serving duties that are under way."""
        with self._lock:
            return list(self._serving)


class _Host(Host):
    """The host as the termination handling acts on it. Before the engine's unit is stopped, `rest`
    is called: it returns once the serving duties are at rest. Every other action is `host`'s."""

    def __init__(self, host: Host, rest: Callable[[], object]) -> None:
        super().__init__()
        self._host = host
        self._rest = rest

    def holds(self, image: str, timeout: float | None = None) -> bool:
        return self._host.holds(image, timeout=timeout)

    def pull(self, image: str, timeout: float | None = None) -> None:
        self._host.pull(image, timeout=timeout)

    def start_engine(self, timeout: float | None = None) -> None:
        self._host.start_engine(timeout=timeout)

    def restart_engine(self) -> None:
        self._host.restart_engine()

    def stop_engine(self) -> None:
        self._rest()
        self._host.stop_engine()


class Agent:
    """The agent of one instance. `autoscaling`, `ssm` and `secrets` are the AWS SDK's clients for
    Auto Scaling, Systems Manager and Secrets Manager, and `dynamodb` is boto3's DynamoDB resource.
    `held` is the record of the release on disk, and `served` the file that says the instance has
    served since its start. The metrics of `registry` are served on `port`. `clock` gives the
    moments of the readings and of a launch, and `wall` the seconds since the epoch; `sleep` waits
    in the event loop, for the readings and the serving duties, and `wait` in a worker thread, for
    the transitions. A test gives its own, and its own `fetch` in place of the network."""

    def __init__(
        self,
        environment: Environment,
        *,
        autoscaling: Any,
        ssm: Any,
        secrets: Any,
        dynamodb: Any,
        host: Host,
        fetch: launch.Fetch = launch.fetch,
        held: Path = release.HELD_RELEASE_FILE,
        served: Path = launch.SERVED_FILE,
        registry: CollectorRegistry = metrics.REGISTRY,
        port: int = metrics.PORT,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        wait: Callable[[float], object] = time.sleep,
    ) -> None:
        self._metadata = launch.Metadata(fetch)
        self._termination = Termination(
            environment.parameter,
            self._metadata,
            autoscaling=autoscaling,
            ssm=ssm,
            host=_Host(host, self._rest),
            engine=environment.engine,
            fetch=fetch,
            sleep=wait,
        )
        self._launch = Launch(
            environment.parameter,
            metadata=self._metadata,
            autoscaling=autoscaling,
            ssm=ssm,
            secrets=secrets,
            dynamodb=dynamodb,
            host=host,
            engine=launch.Engine(fetch),
            termination=self._termination,
            held=held,
            served=served,
            clock=clock,
            sleep=wait,
            wall=wall,
        )

        # Given no Auto Scaling client and no table: a canary completes no hook and writes no row.
        self._canary = Canary(
            environment.parameter,
            ssm=ssm,
            secrets=secrets,
            host=host,
            engine=launch.Engine(fetch),
            held=held,
            clock=clock,
            sleep=wait,
        )

        def serving(held_release: release.Release, instance: str) -> Serving:
            return Serving(
                held_release,
                environment.settings,
                instance,
                environment.engine,
                dynamodb=dynamodb,
                secrets=secrets,
                host=host,
                fetch=fetch,
                registry=registry,
                clock=wall,
                sleep=sleep,
            )

        self._make_serving = serving
        self._held = held
        self._served = served
        self._registry = registry
        self._port = port
        self._clock = clock
        self._sleep = sleep
        # The metrics endpoint, while the program runs.
        self.server: WSGIServer | None = None
        self._instance: str | None = None
        # Whether a hook into service was completed, and no termination hook since.
        self._in_service = False
        # The target state of the last transition: the same state calls for nothing more.
        self._acted: str | None = None
        # Whether the canary was run: it is run once.
        self._tried = False
        # The serving duties: made once, because their metrics are registered once, and running
        # as `_duties` while the instance is in service.
        self._serving: Serving | None = None
        self._duties: asyncio.Task[None] | None = None
        # The transition under way, or the last one until a reading has taken what ended it, and
        # whether a reading found it under way.
        self._transition: asyncio.Task[None] | None = None
        self._outlasted = False
        self._workers = _Workers()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        """Serve the metrics, then read the target state every `TICK_SECONDS` and do what each
        reading calls for, until cancelled or until a transition or the serving duties fail."""
        self._loop = asyncio.get_running_loop()
        self._loop.set_default_executor(self._workers)
        self.server = server = metrics.serve(self._registry, port=self._port)
        # A unit restarted on an instance that serves: no launch transition is run on it.
        self._in_service = self._served.exists()
        try:
            next_at = self._clock()
            while True:
                await self.tick()
                # A fixed cadence: the time a reading takes is not added to the interval.
                next_at = max(next_at + TICK_SECONDS, self._clock())
                if await self._pause(next_at):
                    next_at = self._clock()
        finally:
            try:
                if self._transition is not None:
                    self._transition.cancel()
                    await asyncio.wait({self._transition})
                await self._stop_duties()
            finally:
                server.shutdown()
                server.server_close()

    async def tick(self) -> None:
        """Read the target state once, and do what it calls for."""
        if self._duties is not None and self._duties.done():
            # Raises what ended the duties: the program ends, and its unit restarts it.
            self._duties.result()
        under_way = self._transition is not None and not self._transition.done()
        reading = await asyncio.to_thread(self._read)
        if self._transition is not None:
            if not self._transition.done():
                # One transition at a time: this reading calls for nothing.
                self._outlasted = True
                return
            transition, self._transition = self._transition, None
            transition.result()  # raises what ended it, as for the duties
            if under_way:
                # The transition ended while this reading was made, and the reading may be from
                # before its end: the one that decides what comes next is made after it.
                reading = await asyncio.to_thread(self._read)
        if reading is None:
            return
        target, instance = reading
        if target is None:
            # The instance belongs to no group: a canary, unless the program found it serving.
            if not self._in_service and not self._tried:
                self._tried = True
                self._begin(self._try())
        elif self._in_service:
            if target == launch.IN_SERVICE:
                if self._duties is None:
                    self._serve(instance)
            else:
                self._begin(self._leave(target, instance))
        elif target != self._acted and (target == launch.IN_SERVICE or target in launch.WARM_STATES):
            self._begin(self._enter(target, instance))

    def _begin(self, transition: Coroutine[Any, Any, None]) -> None:
        """Run `transition` beside the readings."""
        self._outlasted = False
        self._transition = asyncio.create_task(transition, name="transition")

    async def _pause(self, until: float) -> bool:
        """Wait for the moment `until` of the next reading. The end of a transition that a reading
        found under way ends the wait before: says whether it did."""

        async def asleep() -> None:
            await self._sleep(until - self._clock())

        # Started here, not at the event loop's next turn: the wait is counted from the moment
        # the clock was read, whatever moves the clock in between.
        sleep = asyncio.Task(asleep(), loop=asyncio.get_running_loop(), eager_start=True)
        waited: set[asyncio.Future[Any]] = {sleep}
        if self._transition is not None and self._outlasted:
            waited.add(self._transition)
        try:
            await asyncio.wait(waited, return_when=asyncio.FIRST_COMPLETED)
            return not sleep.done()
        finally:
            sleep.cancel()

    def _read(self) -> tuple[str | None, str] | None:
        """The target state and the instance, or None when the reading failed. The target state is
        None when the instance metadata says it has none: the instance belongs to no group."""
        try:
            if self._instance is None:
                self._instance = self._metadata.instance_id()
            instance = self._instance
            try:
                return self._metadata.target_state(), instance
            except launch.NoSuchItemError:
                return None, instance
        except launch.MetadataError as error:
            _LOG.warning("the instance metadata did not give %s", error)  # the item's name
            return None

    async def _enter(self, target: str, instance: str) -> None:
        self._acted = target
        outcome = await asyncio.to_thread(self._launch.run)
        _LOG.info("the launch transition ended: %s", outcome.value)
        # The transition reads the target state itself. One that went into service has written
        # the file that says so before it completed its hook; an entry to the warm pool has not.
        if outcome is Outcome.CONTINUED and self._served.exists():
            self._in_service = True
            self._serve(instance)

    async def _try(self) -> None:
        outcome = await asyncio.to_thread(self._canary.run)
        _LOG.info("the canary ended: %s", outcome.value)

    async def _leave(self, target: str, instance: str) -> None:
        outcome = await asyncio.to_thread(self._termination.drain, instance)
        _LOG.info("the termination handling ended: %s", outcome.value)
        if outcome is Outcome.DRAINED:
            # The duties are at rest: the engine's unit was stopped only once they were.
            self._in_service = False
            # A return to the warm pool is not an entry to it.
            self._acted = target

    def _serve(self, instance: str) -> None:
        """Start the serving duties, with the release the instance holds."""
        if self._serving is None:
            held = release.read_held(self._held)
            if held is None:
                _LOG.error("the record of the release on disk cannot be read: no serving duty runs")
                return
            self._serving = self._make_serving(held, instance)
        # The duties' tasks carry the mark by which `_Workers` knows their calls.
        context = contextvars.copy_context()
        context.run(_SERVING.set, True)
        self._duties = asyncio.create_task(self._serving.run(), name="serving", context=context)
        _LOG.info("the serving duties run")

    async def _stop_duties(self) -> None:
        """Bring the serving duties to rest: they are cancelled, and what one of them has in a
        worker thread is waited for, because cancelling the duty does not end it."""
        duties, self._duties = self._duties, None
        if duties is not None:
            duties.cancel()
            await asyncio.wait({duties})
        calls = self._workers.serving()
        if calls:
            await asyncio.wait([asyncio.wrap_future(call) for call in calls])
        if duties is not None:
            _LOG.info("the serving duties are stopped")

    def _rest(self) -> None:
        """Bring the serving duties to rest, for a transition: called in its worker thread, and
        returns once they are."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._stop_duties(), self._loop).result()


def engine_tag(metadata: launch.Metadata) -> str | None:
    """The instance's tag `rhi-engine`, or None when the metadata does not give it."""
    try:
        return metadata.tag(ENGINE_TAG)
    except launch.MetadataError:
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        environment = Environment.read(os.environ, engine_tag=lambda: engine_tag(launch.Metadata()))
    except SettingsError as error:
        sys.exit(f"rhi-lifecycle: {error}")
    # The instance role gives the credentials. The environment names no region for the SDK.
    session = boto3.Session(region_name=environment.region)
    agent = Agent(
        environment,
        autoscaling=session.client("autoscaling"),
        ssm=session.client("ssm"),
        secrets=session.client("secretsmanager"),
        dynamodb=session.resource("dynamodb"),
        host=Host(session.client("ecr")),
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
