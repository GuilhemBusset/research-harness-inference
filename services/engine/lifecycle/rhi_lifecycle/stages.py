"""The stages of an instance's start, and the seconds each took (system design, section 5.2).

Cold-start time is the central number of the design. The lifecycle agent sees every stage of a
start, so it measures them: `Stage` names the stages, and a `Timings` holds what each took in one
launch, under the one `Transition` a launch has. `rhi_lifecycle.launch` marks the stages where they
happen.

**What a stage covers**, as the agent sees it, in the order a launch goes through them:

| Stage | Starts | Ends |
| --- | --- | --- |
| boot | when the kernel starts | when the launch starts |
| image | when the launch starts: it reads the instance metadata and the pool's parameter, and asks the host for the image the parameter names | when the disk holds the image: at once, or after a pull |
| release check | then | when the release on disk is known to be the pool's current one |
| container start | then: the pool's secret is read, and the host starts the engine's unit | when the unit is started |
| weights | then | when the engine's health route first answers 200: the model can serve |
| warm-up | then | when the engine has answered the warm-up request |
| hook | then | when Auto Scaling has answered the completion of the launch hook |

Each stage ends where the next starts, so the stages of a launch sum to the time from the kernel's
start to the completed hook.

Two limits of what the agent can see. The time between Auto Scaling's decision and the kernel's
start is in no stage. And the agent cannot tell the load of the weights from the engine's own
initialization: both end when the health route answers.

**The metric.** `rhi_lifecycle_stage_seconds`, with the labels `transition` and `stage`, is what a
stage took in the agent's last launch, set when the stage ends. A stage that did not end has no
series. A launch that was abandoned keeps the stages it went through, the one it was abandoned in
included. The node's collector scrapes the metric from the agent's endpoint
(`rhi_lifecycle.metrics`), with the instance's own labels.

**The label `transition`.** An instance has one way into service, a launch, so the label has one
value, `cold-launch`: the fleet dashboard selects its series by it, and the label stays so that the
dashboard reads the launches of this agent as it read the ones before.

**The launches completed.** `rhi_lifecycle_launch_seconds`, a histogram with the label
`transition`, observes once each launch whose hook Auto Scaling took with `CONTINUE`: the seconds
its stages took in all, from the kernel's start to the completed hook. A launch that was abandoned,
or whose completion did not reach Auto Scaling, is not observed. So its count is the launches
completed, two of them two even when they took the same seconds. 300 seconds is a boundary, and a
launch is observed one step of the float above its seconds, so the bucket of 300 counts the
launches strictly under 5 minutes.

**The heartbeat.** A launch also names the stage it has reached in the instance's row of the
heartbeats table (`rhi_lifecycle.launch`), by the stage's value here. Once the instance serves,
`rhi_lifecycle.serving` writes the row, under its own name.
"""

from __future__ import annotations

import enum
import math
from pathlib import Path
from typing import TYPE_CHECKING

from prometheus_client import Gauge, Histogram

from rhi_lifecycle import metrics

if TYPE_CHECKING:
    from collections.abc import Callable

METRIC = "rhi_lifecycle_stage_seconds"
LAUNCH_METRIC = "rhi_lifecycle_launch_seconds"
# The seconds of a launch completed. 300 is the objective's 5 minutes (section 7.3).
LAUNCH_BUCKETS = (30, 60, 90, 120, 150, 180, 240, 300, 420, 600, 900, 1200, 1800, 3600)

# The seconds since the kernel started, first on the file's one line. The agent's container reads
# the host's own.
UPTIME_FILE = Path("/proc/uptime")


class Stage(enum.Enum):
    """A stage of a start. The value is the stage's name in the metric and in the heartbeat."""

    BOOT = "boot"
    RELEASE_CHECK = "release-check"
    IMAGE = "image"
    CONTAINER_START = "container-start"
    WEIGHTS = "weights"
    WARM_UP = "warm-up"
    HOOK = "hook"


class Transition(enum.Enum):
    """The transition of `rhi_lifecycle.launch`. The value is its name in the metrics' label."""

    # An instance is launched into service: the cold launch of section 5.2.
    LAUNCH = "cold-launch"


SECONDS = Gauge(
    METRIC,
    "The seconds a stage of the instance's last launch took.",
    ("transition", "stage"),
    registry=metrics.REGISTRY,
)

LAUNCHES = Histogram(
    LAUNCH_METRIC,
    "The seconds each launch completed with CONTINUE took, from the kernel's start to the completed hook.",
    ("transition",),
    buckets=LAUNCH_BUCKETS,
    registry=metrics.REGISTRY,
)


def uptime(path: Path = UPTIME_FILE) -> float | None:
    """The seconds since the kernel started, or None when the host does not say."""
    try:
        return float(path.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


class Timings:
    """The seconds each stage of one launch took. `clock` gives the moments the stages are marked
    at, in seconds, and `gauge` is where the durations are exposed: a new launch empties it."""

    def __init__(
        self,
        clock: Callable[[], float],
        gauge: Gauge = SECONDS,
        launches: Histogram = LAUNCHES,
    ) -> None:
        self.seconds: dict[Stage, float] = {}
        self._clock = clock
        self._gauge = gauge
        self._launches = launches
        # The stage under way, and when it started.
        self._under_way: tuple[Stage, float] | None = None
        gauge.clear()

    def start(self, stage: Stage, at: float | None = None) -> None:
        """Mark the start of `stage`, now or at the moment `at` of the clock. The stage under way
        ends at the same moment."""
        moment = self._clock() if at is None else at
        self._end(moment)
        self._under_way = (stage, moment)

    def end(self) -> None:
        """Mark the end of the stage under way, if there is one."""
        self._end(self._clock())

    def completed(self) -> None:
        """The launch's hook was completed with `CONTINUE`: observe, once, the seconds its stages
        took, strictly above themselves so that a boundary's bucket holds what is under it."""
        total = math.nextafter(sum(self.seconds.values()), math.inf)
        self._launches.labels(transition=Transition.LAUNCH.value).observe(total)

    def _end(self, moment: float) -> None:
        if self._under_way is None:
            return
        stage, since = self._under_way
        self._under_way = None
        self.seconds[stage] = self.seconds.get(stage, 0.0) + moment - since
        self._expose()

    def _expose(self) -> None:
        for stage, seconds in self.seconds.items():
            self._gauge.labels(transition=Transition.LAUNCH.value, stage=stage.value).set(seconds)
