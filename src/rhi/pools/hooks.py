"""A pool's lifecycle hooks, with explicit timeouts (system design, sections 4.3 and 17).

A hook holds an instance in a wait state until the lifecycle agent on it completes the hook, or
the hook's timeout passes. Left to its default, a hook waits an hour, and a dead agent bills an
hour of GPU per attempt: both timeouts are set here.

**The launch hook**, `rhi-launch`, holds an instance that enters the warm pool and one that is
promoted from it to service. Entry to the warm pool has 15 minutes and promotion has twice the
measured start. One hook serves both, so it carries the longer of the two, and the agent keeps
each transition's own deadline (`rhi_lifecycle`). Until a start has been measured for the pool
(`measured_start_seconds`, absent from the configuration until then), the timeout is the 15
minutes. The design gives its figure for the coursework pool and none for the mock pool, which
follows the same rule. When the timeout passes the instance is abandoned: an instance whose agent
never answered does not enter service, and the group replaces it.

**The termination hook**, `rhi-termination`, holds an instance that leaves service, for the warm
pool or for good, while its engine drains. Its timeout is the pool's `drain_seconds` plus a margin
of 60 seconds. When it passes the instance goes on its way: a dead agent does not keep an instance
that the group has already chosen to remove.

The hooks have no notification target: the agent on the instance sees the wait state itself and
completes the hook by its name, which the pool's release parameter gives it (`rhi.pools.release`),
with the right the pool's instance role holds (`rhi.pools.role`). This module therefore creates no
role and no policy.

The multi-GPU pools will extend their launch hook by heartbeats (section 4.3), which is the
agent's part and not a property of the hook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_autoscaling as autoscaling
from constructs import Construct

from rhi.pools.release import LAUNCH_HOOK, TERMINATION_HOOK

if TYPE_CHECKING:
    from rhi_common.pool_config import Pool

# The launch hook's timeout while no start has been measured, and its floor afterwards: the time
# an instance has to enter the warm pool.
LAUNCH_TIMEOUT_SECONDS = 15 * 60
# What the termination hook adds to the pool's drain.
DRAIN_MARGIN_SECONDS = 60

LAUNCHING = "autoscaling:EC2_INSTANCE_LAUNCHING"
TERMINATING = "autoscaling:EC2_INSTANCE_TERMINATING"


def launch_timeout(pool: Pool) -> int:
    """The seconds the launch hook waits: 15 minutes, or twice the measured start when that is longer."""
    if pool.measured_start_seconds is None:
        return LAUNCH_TIMEOUT_SECONDS
    return max(LAUNCH_TIMEOUT_SECONDS, 2 * pool.measured_start_seconds)


def termination_timeout(pool: Pool) -> int:
    """The seconds the termination hook waits: the pool's drain and the margin."""
    return pool.drain_seconds + DRAIN_MARGIN_SECONDS


class PoolHooks(Construct):
    """One pool's lifecycle hooks on its group: `launch` and `termination`."""

    def __init__(
        self, scope: Construct, construct_id: str, *, pool: Pool, group: autoscaling.CfnAutoScalingGroup
    ) -> None:
        super().__init__(scope, construct_id)
        self.launch = autoscaling.CfnLifecycleHook(
            self,
            "Launch",
            auto_scaling_group_name=group.ref,
            lifecycle_hook_name=LAUNCH_HOOK,
            lifecycle_transition=LAUNCHING,
            heartbeat_timeout=launch_timeout(pool),
            default_result="ABANDON",
        )
        self.termination = autoscaling.CfnLifecycleHook(
            self,
            "Termination",
            auto_scaling_group_name=group.ref,
            lifecycle_hook_name=TERMINATION_HOOK,
            lifecycle_transition=TERMINATING,
            heartbeat_timeout=termination_timeout(pool),
            default_result="CONTINUE",
        )
