"""The transition out of service: the termination hook, completed once the engine is idle (system
design, section 4.3).

When an instance leaves service, for the warm pool or for good, a request may still be running on
it. So the agent asks the engine's `/metrics` on localhost every `POLL_SECONDS`, under the names of
the engine's row in `rhi_common.engine_metrics`, until one answer gives nothing running and nothing
waiting. Then it completes the termination hook the settings of the release parameter name, with
`CONTINUE`, and the instance may leave: Auto Scaling terminates it, or stops it into the warm pool.

**Only what the engine reports ends the wait.** An engine that does not answer, or whose answer
does not give both counts, has not reported that it is idle, and the wait goes on. The agent sets
no deadline of its own: the hook's timeout, `drain_seconds` plus a margin of 60 seconds (section
17), is the bound, as it is when the agent is gone.

**The engine's unit** is stopped through `rhi_lifecycle.host` only after the hook is completed,
never while the engine may hold a request. A hook that could not be completed leaves the engine
running.

**Which instance drains.** `Termination.run` reads the target lifecycle state in the instance
metadata and drains an instance whose target is `Terminated`. An instance that returns from service
to the warm pool arrives in the wait state a new instance enters the pool by (evidence E10), and
only the file that says it has served tells the two apart: `rhi_lifecycle.launch` reads it, and
hands the instance that has served to `Termination.drain`.

**What cannot be completed.** The hook is named in the settings of the release parameter, and the
instance by its metadata. Without one of them there is nothing to call: the result is
`NOT_COMPLETED`, at once, and the hook's own timeout lets the instance leave.

Nothing here repeats what AWS or the engine answered.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from rhi_common.engine_metrics import metric_names
from rhi_lifecycle import launch, release
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.launch import Outcome
from rhi_lifecycle.serving import METRICS_SECONDS, read_load

# The wait between two questions to the engine's metrics. The design gives no figure: an idle
# instance bills until its hook is completed, and a question to localhost costs nothing.
POLL_SECONDS = 2.0

# The target lifecycle state of an instance that leaves service for good.
TERMINATED = "Terminated"


@dataclass(frozen=True)
class Termination:
    """One instance's way out of service. `parameter` is the name of its pool's release parameter,
    `autoscaling` and `ssm` are the AWS SDK's clients for Auto Scaling and Systems Manager, and
    `engine` names the engine's row of metric names. `fetch` and `sleep` are arguments so that a
    test needs no network and waits for nothing."""

    parameter: str
    metadata: launch.Metadata
    autoscaling: Any
    ssm: Any
    host: Host
    engine: str
    fetch: launch.Fetch = launch.fetch
    sleep: Callable[[float], object] = time.sleep

    def __post_init__(self) -> None:
        metric_names(self.engine)  # an engine with no row is refused here, not at every question

    def run(self) -> Outcome:
        """Drain the instance if it is terminated. An instance that returns to the warm pool is
        `Launch.run`'s to tell from one that enters it."""
        try:
            target = self.metadata.target_state()
            instance = self.metadata.instance_id()
        except launch.MetadataError:
            return Outcome.NOT_COMPLETED
        if target != TERMINATED:
            return Outcome.NOT_A_TERMINATION
        return self.drain(instance)

    def drain(self, instance: str) -> Outcome:
        """Wait until the engine reports nothing running and nothing waiting, complete the
        termination hook of `instance`, then stop the engine's unit."""
        current = release.read_current(self.ssm, self.parameter)
        if current is None:
            return Outcome.NOT_COMPLETED
        while not self.idle():
            self.sleep(POLL_SECONDS)
        try:
            self.autoscaling.complete_lifecycle_action(
                AutoScalingGroupName=current.settings.group,
                LifecycleHookName=current.settings.termination_hook,
                InstanceId=instance,
                LifecycleActionResult=launch.CONTINUE,
            )
        except (BotoCoreError, ClientError):
            return Outcome.NOT_COMPLETED
        try:
            self.host.stop_engine()
        except HostError:
            # The instance leaves all the same, and its engine with it.
            pass
        return Outcome.DRAINED

    def idle(self) -> bool:
        """Whether the engine reports, now, nothing running and nothing waiting."""
        answer = self.fetch(urllib.request.Request(f"{launch.ENGINE_URL}/metrics"), METRICS_SECONDS)
        return answer is not None and answer[0] == 200 and read_load(answer[1], self.engine) == (0, 0)
