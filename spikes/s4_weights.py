"""Spike S4: how long the coursework model's weights take to load from S3, tuned (system design,
sections 4.4, 5.2 and 12).

vLLM's Run:ai streamer reads the weights from S3 straight into GPU memory. AWS measured 15 GB in
about 5 seconds and 200 GB in about 25 seconds with the streamer tuned; defaults can be ten times
slower, one model was reported slower than the plain loader, and those timings come from an
instance with far more network bandwidth than a single-GPU type has (evidence E11). The cold-start
budget estimates 20 to 100 seconds for the coursework model's 31 GB (section 5.2). **Pass:** the
median weights stage is under 100 seconds. If it is not, the pool's loader becomes download, then
load: the pool's `loader` (`rhi_common.pool_config`).

**One run is one loader setting.** The loader is in the pool's configuration, so each setting the
spike compares is a configuration the owner deploys before starting the job, and gives the job the
setting's name (`RHI_S4_LOADER`). The name is the run's label: each setting is posted as its own
run, `measure/S4/<setting>/<part>`, and labels its rows in the record `docs/measurements/s4.md`.

**What it does**, through the gateway only, as a canary student (`common.canary_keys`), and reading
the pool only from the workspace. It never changes the pool: the wake goes to the gateway, and the
scaler alone starts an instance (section 5.1). For each of the starts it is given:

1. **Cold.** It waits until the gateway's status says the pool is `cold`, and then until no
   instance of the pool writes the lifecycle agent's stage metric: an instance draining out of
   service still exposes its last launch's stages, the weights among them, and has to be gone
   before the wake (`Pool.quiet`).
2. **The wake.** It notes the time and sends `POST /wake` for the pool, as a student's `rhi wait`
   does, then reads the status every `POLL_SECONDS` until the pool is `ready`.
3. **The weights stage of that start.** The lifecycle agent sets `rhi_lifecycle_stage_seconds`
   (`rhi_lifecycle.stages`) for each stage of its launch, and the node's collector writes it to the
   workspace with the instance's ID as the label `instance`. The job reads the weights and the hook
   stages of the pool, with the time each sample was taken (`timestamp()`), from the wake on, and
   keeps only the samples taken after the wake: no instance was writing when it was sent, so these
   come from a launch that began after it. **The launch that became ready** is the one, an instance
   and a transition into service (a promotion from the warm pool, or a cold launch), that has both
   a weights and a hook sample after the wake. The hook stage ends only when Auto Scaling has taken
   the launch's completion with `CONTINUE`: a launch abandoned after its weights loaded has a
   weights sample and no hook, and is not counted; its replacement is. A start with no such launch,
   or with two, is not counted, and stops the run (`weights_of_launch`).

**The numbers** (`Number`), posted with the verdict through `common.run_spike`: `starts`, the
starts made, and `weights_seconds_median`, the median of their weights stages in seconds. Each
start's seconds, instance transition and the setting are printed, for the record.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s4_weights`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_S4_LOADER` | The name of the loader setting the pool runs: lower-case letters, digits and hyphens |
| `RHI_S4_STARTS` | The starts to make, one or more |
| `RHI_SPIKE_POOL` | The pool, `coursework` unless given |

`RHI_RUN_LABEL`, if given, must be the loader setting's name. The region comes from CodeBuild's
`AWS_REGION`. Run it while nothing else sends to the pool: the pool has to go cold between starts.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import boto3

import common
from rhi_lifecycle.stages import METRIC, Stage, Transition

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

JOB = "s4_weights"
MEASUREMENT = "S4"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
LOADER_VARIABLE = "RHI_S4_LOADER"
STARTS_VARIABLE = "RHI_S4_STARTS"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "coursework"

READY, COLD = "ready", "cold"
# The pass condition (section 12): the median weights stage under this many seconds.
PASS_SECONDS = 100.0

# Seconds. The status is read this often while the pool wakes.
POLL_SECONDS = 5
# Seconds. While the pool is not yet cold, or an instance still writes: the scaler takes an idle
# pool to zero after 10 idle minutes (section 5.1), and an instance then drains and stops.
COLD_POLL_SECONDS = 30
COLD_WAIT_SECONDS = 90 * 60
QUIET_WAIT_SECONDS = 30 * 60
# Seconds. The collector scrapes every 15 seconds and writes to the workspace after: an instance
# that runs has a sample this recent.
SCRAPE_SECONDS = 15
QUIET_SECONDS = 90
# Seconds. A cold launch pulls the engine image before it loads the weights.
READY_WAIT_SECONDS = 40 * 60
# Seconds. How long after readiness the launch's samples may take to reach the workspace, read
# every `READ_POLL_SECONDS`.
READ_WAIT_SECONDS = 5 * 60
READ_POLL_SECONDS = 30
STATUS_TIMEOUT = 30

# The transitions that bring an instance into service, and so load the weights.
INTO_SERVICE = frozenset({Transition.PROMOTION.value, Transition.COLD_LAUNCH.value})


class Number(StrEnum):
    """The names of the numbers a run posts."""

    STARTS = "starts"
    WEIGHTS_SECONDS_MEDIAN = "weights_seconds_median"


def numbers_of(weights: Sequence[float]) -> dict[str, int | float]:
    """The numbers of a run whose starts' weights stages took `weights` seconds."""
    return {Number.STARTS: len(weights), Number.WEIGHTS_SECONDS_MEDIAN: common.median(weights)}


def passes(numbers: common.Numbers) -> bool:
    """Whether the median weights stage is under the pass condition's seconds."""
    return numbers[Number.WEIGHTS_SECONDS_MEDIAN] < PASS_SECONDS


SPIKE = common.Spike(MEASUREMENT, Number, passes)


# The launch of one start.


class NoLaunch(RuntimeError):
    """No single launch into service completed after the wake: the start is not counted."""


@dataclass(frozen=True)
class Launch:
    """One launch into service, seen in the workspace: its instance, its transition, and the
    seconds its weights stage took."""

    instance: str
    transition: str
    weights_seconds: float


def _identity(labels: Mapping[str, str]) -> frozenset[tuple[str, str]]:
    # `timestamp()` drops the metric's name, and keeps every other label.
    return frozenset((name, value) for name, value in labels.items() if name != "__name__")


def taken_after(values: Sequence[common.Series], stamps: Sequence[common.Series], woke_at: float) -> list[common.Series]:
    """The samples of `values` taken after `woke_at`: each series of `values` paired with the series
    of `stamps` of the same labels, which gives at each step the time its sample was taken."""
    taken = {_identity(one.labels): dict(one.samples) for one in stamps}
    kept = []
    for one in values:
        at = taken.get(_identity(one.labels), {})
        samples = tuple((step, value) for step, value in one.samples if at.get(step, woke_at) > woke_at)
        if samples:
            kept.append(common.Series(one.labels, samples))
    return kept


def weights_of_launch(values: Sequence[common.Series], stamps: Sequence[common.Series], woke_at: float) -> Launch:
    """The launch that became ready for the wake at `woke_at`, from the weights and hook stages of
    the pool (`values`) and the times their samples were taken (`stamps`): the one launch into
    service with a weights and a hook sample taken after the wake. NoLaunch when there is none, or
    more than one."""
    weights: dict[tuple[str, str], float] = {}
    completed: set[tuple[str, str]] = set()
    for one in taken_after(values, stamps, woke_at):
        launch = (one.labels.get("instance", ""), one.labels.get("transition", ""))
        if not launch[0] or launch[1] not in INTO_SERVICE:
            continue
        if one.labels.get("stage") == Stage.WEIGHTS.value:
            weights[launch] = one.samples[-1][1]
        elif one.labels.get("stage") == Stage.HOOK.value:
            completed.add(launch)
    ready = sorted(launch for launch in weights if launch in completed)
    if len(ready) != 1:
        raise NoLaunch(f"{len(ready)} launches into service completed after the wake, not one")
    ((instance, transition),) = ready
    return Launch(instance, transition, weights[(instance, transition)])


# The pool, through the gateway and the workspace.


class Reader(Protocol):
    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]: ...


@dataclass(frozen=True)
class Gateway:
    """The gateway at `endpoint`, asked about the pool `pool` as the canary student of `key`."""

    endpoint: str
    pool: str
    key: str = field(repr=False)

    def _ask(self, method: str, path: str, body: bytes | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        request = urllib.request.Request(self.endpoint.rstrip("/") + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=STATUS_TIMEOUT) as answer:
            return json.loads(answer.read())

    def state(self) -> str:
        """The pool's state as the status gives it; empty when the status could not be read."""
        try:
            pools = self._ask("GET", "/status")["pools"]
            return next(str(record["state"]) for record in pools if record["model"] == self.pool)
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return ""

    def wake(self) -> None:
        """Wakes the pool, as a student's `rhi wait` does."""
        self._ask("POST", "/wake", json.dumps({"model": self.pool}).encode())


@dataclass(frozen=True)
class Pool:
    """The lifecycle agent's stage metric of the pool `name`, in the workspace `workspace`."""

    name: str
    workspace: Reader

    def _selector(self, stages: str | None = None) -> str:
        matchers = f'pool="{self.name}"' + ("" if stages is None else f',stage=~"{stages}"')
        return f"{METRIC}{{{matchers}}}"

    def quiet(self, now: float) -> bool:
        """Whether no instance of the pool has written a stage sample in the last `QUIET_SECONDS`."""
        stamps = self.workspace.read_series(
            f"timestamp({self._selector()})", start=now - QUIET_SECONDS, end=now, step=SCRAPE_SECONDS
        )
        return all(taken <= now - QUIET_SECONDS for one in stamps for taken in one.values())

    def launch(self, woke_at: float, now: float) -> Launch:
        """The launch that became ready for the wake at `woke_at` (`weights_of_launch`)."""
        selector = self._selector(f"{Stage.WEIGHTS.value}|{Stage.HOOK.value}")
        span = {"start": woke_at, "end": now, "step": SCRAPE_SECONDS}
        values = self.workspace.read_series(selector, **span)
        stamps = self.workspace.read_series(f"timestamp({selector})", **span)
        return weights_of_launch(values, stamps, woke_at)


class Run:
    """One run of the spike: `starts` starts of the pool, by a wake through `gateway`, each one's
    weights stage read from `pool`."""

    def __init__(
        self,
        gateway: Any,
        pool: Pool,
        starts: int,
        setting: str,
        *,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        if starts < 1:
            raise ValueError("spike S4 makes one start or more")
        self.gateway, self.pool, self.starts, self.setting = gateway, pool, starts, setting
        self.wall, self.sleep, self.log = wall, sleep, log

    def _wait(self, done: Callable[[], bool], limit: float, every: float, what: str) -> None:
        started = self.wall()
        while not done():
            if self.wall() - started >= limit:
                raise TimeoutError(f"the pool {what} in time")
            self.sleep(every)

    def start(self) -> Launch:
        """One start: a cold pool, the wake, readiness, and the weights stage of the launch it brought."""
        self._wait(lambda: self.gateway.state() == COLD, COLD_WAIT_SECONDS, COLD_POLL_SECONDS, "did not go cold")
        self._wait(lambda: self.pool.quiet(self.wall()), QUIET_WAIT_SECONDS, COLD_POLL_SECONDS, "kept writing")
        woke_at = self.wall()
        self.gateway.wake()
        self._wait(lambda: self.gateway.state() == READY, READY_WAIT_SECONDS, POLL_SECONDS, "was not ready")
        ready_at = self.wall()
        while True:
            try:
                return self.pool.launch(woke_at, self.wall())
            except NoLaunch:
                if self.wall() - ready_at >= READ_WAIT_SECONDS:
                    raise
            self.sleep(READ_POLL_SECONDS)

    def measure(self) -> dict[str, int | float]:
        weights = []
        for number in range(1, self.starts + 1):
            launch = self.start()
            weights.append(launch.weights_seconds)
            self.log(
                f"start {number}: loader {self.setting}, {launch.transition}, weights {launch.weights_seconds:.1f} s"
            )
        return numbers_of(weights)


# The job.


def run_label(environ: Mapping[str, str]) -> str:
    """The run's label: the name of the loader setting. ValueError when it is missing, of another
    shape than a run label, or when `RHI_RUN_LABEL` gives another one."""
    setting = environ.get(LOADER_VARIABLE, "")
    if not setting:
        raise ValueError("spike S4 needs the name of the loader setting the pool runs")
    if environ.get(common.RUN_LABEL_VARIABLE, setting) != setting:
        raise ValueError("the run label of spike S4 is the name of the loader setting")
    return common.run_label(JOB, {common.RUN_LABEL_VARIABLE: setting})


def starts_of(environ: Mapping[str, str]) -> int:
    """The starts to make. ValueError for a count that is not a whole number of one or more."""
    starts = int(environ.get(STARTS_VARIABLE, ""))
    if starts < 1:
        raise ValueError("spike S4 makes one start or more")
    return starts


def main(environ: Mapping[str, str] = os.environ) -> common.Verdict:
    """Runs the spike with the job's environment and credentials, and posts its numbers under the
    loader setting's name."""
    label = run_label(environ)
    starts = starts_of(environ)

    def measure() -> dict[str, int | float]:
        name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        gateway = Gateway(environ[ENDPOINT_VARIABLE], name, common.canary_keys().students[0])
        print(f"pool {name}, loader setting {label}, {starts} starts")
        return Run(gateway, Pool(name, workspace), starts, label).measure()

    return common.run_spike(SPIKE, JOB, measure, environ={**environ, common.RUN_LABEL_VARIABLE: label})


if __name__ == "__main__":
    main()
