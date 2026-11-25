"""The run of a canary: an instance started from a pool's launch template, outside the pool's group
(system design, sections 4.3 and 5.1).

The promotion job tries a candidate machine image by starting one instance from the pool's launch
template, in no group, so the scaler never counts it. Every instance runs the same boot unit and
the same agent, and the agent knows such an instance by its metadata: an instance of a group finds
its target lifecycle state there, and one that belongs to no group finds none
(`launch.NoSuchItemError`). The agent's program hands that instance to `Canary.run`.

**What the run does** is what a launch does, by the same steps (`launch.Steps`): pull the engine
image, check the release, start the engine, wait for its health route, send the real warm-up
request.

**What it leaves out.** No lifecycle hook exists for the instance, so none is completed: a canary
is given no Auto Scaling client. It is given no heartbeats table either, and keeps the stage it has
reached in memory only: a row there would be read as one of the pool's instances. The program
starts no serving duty after it.

**The outcome** is one line on the instance's serial console, which the promotion job reads with
EC2's console output. Writing it needs no permission of the instance role and leaves the stage's
tables untouched. The line is one of two, its words separated by one space:

    rhi-canary served image_digest=<digest> model_revision=<revision>
    rhi-canary failed stage=<stage>

`<digest>` and `<revision>` are the release's, as a heartbeat's row carries them
(`launch.identity`): `sha256:` and 64 hexadecimal digits, and the model's pinned commit, or `none`
for a pool that serves no model. `<stage>` is the last stage the run reached, by its name in
`rhi_lifecycle.stages`: `image`, `release-check`, `container-start`, `weights` or `warm-up`. The
image stage starts with the run, so a pool's parameter that cannot be read fails there. The line
holds nothing that AWS, docker or the engine answered.

**The deadline.** Only the launch hook's own timeout bounds a launch: 15 minutes, or twice the
pool's measured start when that is longer (`rhi.pools.hooks`). A canary has no hook, so the run
keeps that time itself, and an engine that never becomes healthy fails at it, in the weights stage.
Every step takes what is left of it as its time limit.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rhi_lifecycle import launch, release
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.stages import Stage

# The word every line of a canary starts with: the promotion job finds the line by it.
MARK = "rhi-canary"

# The run's deadline: the launch hook's timeout (`rhi.pools.hooks.launch_timeout`). 15 minutes, or
# this many times the pool's measured start when that is longer.
DEADLINE_SECONDS = 15 * 60
DEADLINE_STARTS = 2
# How long the write of the line may take.
CONSOLE_SECONDS = 10.0

_LOG = logging.getLogger(__name__)


class Outcome(enum.Enum):
    """How a canary's run ended. The value is the outcome's word in the console line."""

    SERVED = "served"
    FAILED = "failed"


def served_line(served: release.Release) -> str:
    """The line of a canary whose engine answered the warm-up request, serving `served`."""
    digest, revision = launch.identity(served)
    return f"{MARK} {Outcome.SERVED.value} image_digest={digest} model_revision={revision}"


def failed_line(stage: Stage) -> str:
    """The line of a canary that failed, `stage` being the last stage it reached."""
    return f"{MARK} {Outcome.FAILED.value} stage={stage.value}"


@dataclass(frozen=True)
class Canary:
    """One canary's run. `parameter` is the name of its pool's release parameter; `ssm` and
    `secrets` are the AWS SDK's clients for Systems Manager and Secrets Manager. `held` is the
    record of the release on disk. `clock` and `sleep` are arguments so that a test waits for
    nothing."""

    parameter: str
    ssm: Any
    secrets: Any
    host: Host
    engine: launch.Engine
    held: Path = release.HELD_RELEASE_FILE
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], object] = time.sleep

    def run(self) -> Outcome:
        """Do the steps of a launch, then write the outcome's line on the console."""
        started = self.clock()
        steps = launch.Steps(
            self.parameter, self.ssm, self.secrets, self.host, self.engine, self.held, self.clock, self.sleep
        )
        # The stages the run has reached, in order.
        reached = [Stage.IMAGE]
        try:
            current = release.read_current(self.ssm, self.parameter)
            if current is None:
                raise launch.Abandon
            measured = current.measured_start_seconds
            deadline = started + max(DEADLINE_SECONDS, DEADLINE_STARTS * (measured or 0))
            steps.pull(current, deadline)
            steps.serve(current, deadline, reached.append)
        except launch.Abandon:
            outcome, line = Outcome.FAILED, failed_line(reached[-1])
        else:
            outcome, line = Outcome.SERVED, served_line(current.release)
        try:
            self.host.write_console(line, timeout=CONSOLE_SECONDS)
        except HostError as error:
            # The promotion job reads no line, and fails the candidate at its own time limit.
            _LOG.error("the canary's line was not written (%s)", error)
        return outcome
