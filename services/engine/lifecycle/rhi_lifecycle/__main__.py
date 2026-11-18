"""The lifecycle agent's program: it runs the agent's transitions and duties, in order, on one
instance (system design, section 4.3). The agent's unit runs it as `python -m rhi_lifecycle`.

**Its settings** come from its environment, which the agent's unit fills from
`/run/rhi/release.env`, as the boot program writes it: `RHI_RELEASE_PARAMETER`, `RHI_REGION` and
one variable for each setting of `rhi_lifecycle.release.Settings`, `RHI_POOL` to `RHI_SECRET_ID`.
A variable that is missing ends the program at once, with a message that names it. `RHI_ENGINE`
names the engine's row of metric names (`rhi_common.engine_metrics`); the boot program does not
write it, and without it the row is the mock engine's.

**What it does.** It starts the metrics endpoint (`rhi_lifecycle.metrics`), then reads the
instance's target lifecycle state from the instance metadata every `TICK_SECONDS`. The design gives
no interval for it: 5 seconds is the heartbeat's own. A reading that fails is left for the next
one. What a reading calls for:

| The instance | Target state | What the program does |
| --- | --- | --- |
| not in service | `InService` or a warm-pool state, not yet acted on | runs the launch transition (`rhi_lifecycle.launch`), once |
| in service | `InService` | runs the serving duties (`rhi_lifecycle.serving`) |
| in service | any other | hands the instance to the termination handling (`rhi_lifecycle.termination`) |

The instance is in service once a launch into service, a promotion or a launch straight there, has
completed its hook: the serving duties start then. They run until the target state leaves service,
and on while the termination handling waits for the engine to drain: the program stops them once
the termination hook is completed. A hook that could not be completed leaves the instance in
service, and the next reading hands it over again.

After an entry to the warm pool the program runs no serving duty: the group stops the instance. An
instance that has left service is not taken for one that enters the pool: the state it left for
counts as acted on.

**A restart of its unit.** The launch transition writes a file before it completes a hook into
service (`launch.SERVED_FILE`, in `/run/rhi`, which is emptied at every stop). A program that
starts and finds the file is on an instance that already serves: it runs no launch transition, and
takes the instance as in service. The serving duties end only by failing, and the program then
ends with them, so that its unit restarts it and they resume.

Every call that waits (the instance metadata, a transition) runs in a worker thread, so the
serving duties keep their own cadence beside it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Self

import boto3

from rhi_common.engine_metrics import ENGINES, MOCK
from rhi_lifecycle import launch, metrics, release
from rhi_lifecycle.host import Host
from rhi_lifecycle.launch import Launch, Outcome
from rhi_lifecycle.serving import Serving
from rhi_lifecycle.termination import Termination

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path
    from wsgiref.simple_server import WSGIServer

    from prometheus_client import CollectorRegistry

# The time between two readings of the target lifecycle state.
TICK_SECONDS = 5.0

PARAMETER_VARIABLE = "RHI_RELEASE_PARAMETER"
REGION_VARIABLE = "RHI_REGION"
ENGINE_VARIABLE = "RHI_ENGINE"

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
    def read(cls, environ: Mapping[str, str]) -> Self:
        """The settings in `environ`. Raises `SettingsError` when a variable is missing or empty."""
        setting_variables = {field.name: "RHI_" + field.name.upper() for field in fields(release.Settings)}
        missing = [
            variable
            for variable in (PARAMETER_VARIABLE, REGION_VARIABLE, *setting_variables.values())
            if not environ.get(variable)
        ]
        if missing:
            raise SettingsError("the environment lacks " + ", ".join(missing))
        engine = environ.get(ENGINE_VARIABLE) or MOCK
        if engine not in ENGINES:
            raise SettingsError(f"{ENGINE_VARIABLE} names an engine with no metric names")
        settings = release.Settings(**{name: environ[variable] for name, variable in setting_variables.items()})
        return cls(environ[PARAMETER_VARIABLE], environ[REGION_VARIABLE], engine, settings)


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
            host=host,
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
        # The serving duties: made once, because their metrics are registered once, and running
        # as `_duties` while the instance is in service.
        self._serving: Serving | None = None
        self._duties: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Serve the metrics, then read the target state every `TICK_SECONDS` and do what each
        reading calls for, until cancelled or until the serving duties fail."""
        self.server = server = metrics.serve(self._registry, port=self._port)
        # A unit restarted on an instance that serves: no launch transition is run on it.
        self._in_service = self._served.exists()
        try:
            next_at = self._clock()
            while True:
                await self.tick()
                # A fixed cadence: the time a reading takes is not added to the interval, and a
                # transition that outlasted it is followed by a reading at once.
                next_at = max(next_at + TICK_SECONDS, self._clock())
                await self._sleep(next_at - self._clock())
        finally:
            try:
                await self._stop_duties()
            finally:
                server.shutdown()
                server.server_close()

    async def tick(self) -> None:
        """Read the target state once, and do what it calls for."""
        if self._duties is not None and self._duties.done():
            # Raises what ended the duties: the program ends, and its unit restarts it.
            self._duties.result()
        reading = await asyncio.to_thread(self._read)
        if reading is None:
            return
        target, instance = reading
        if self._in_service:
            if target == launch.IN_SERVICE:
                if self._duties is None:
                    self._serve(instance)
            else:
                await self._leave(target, instance)
        elif target != self._acted and (target == launch.IN_SERVICE or target in launch.WARM_STATES):
            await self._enter(target, instance)

    def _read(self) -> tuple[str, str] | None:
        """The target state and the instance, or None when the instance metadata gives neither."""
        try:
            if self._instance is None:
                self._instance = self._metadata.instance_id()
            return self._metadata.target_state(), self._instance
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

    async def _leave(self, target: str, instance: str) -> None:
        outcome = await asyncio.to_thread(self._termination.drain, instance)
        _LOG.info("the termination handling ended: %s", outcome.value)
        if outcome is Outcome.DRAINED:
            await self._stop_duties()
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
        self._duties = asyncio.create_task(self._serving.run(), name="serving")
        _LOG.info("the serving duties run")

    async def _stop_duties(self) -> None:
        duties, self._duties = self._duties, None
        if duties is not None:
            duties.cancel()
            await asyncio.wait({duties})
            _LOG.info("the serving duties are stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        environment = Environment.read(os.environ)
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
