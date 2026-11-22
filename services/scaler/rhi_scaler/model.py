"""What a run of the scaler decides from, and what it decides (system design, section 5.1).

One scaler is the only writer of a pool's size. Each run reads the same inputs and `decide`
(`rhi_scaler.decide`) computes, from them alone, the size every pool should have. This module holds
the two shapes and nothing that acts: the edge of the scaler reads AWS and the tables into `Inputs`
and carries a `Decision` out.

A time is a whole number of seconds since the epoch, as in `rhi_common.records`, and `None` when
it is unknown. An instance is named by its ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rhi_common.pool_config import Pool
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode


@dataclass(frozen=True)
class Supply:
    """What a pool's group holds, from Auto Scaling and target health.

    `desired` is the group's desired capacity. The four states each list their instances: in
    service, starting (launched, or leaving the warm pool, and not yet in service), draining
    (leaving service) and warm (in the warm pool). `running` lists every running instance of the
    group in any state, one being prepared for the warm pool included.
    """

    desired: int
    in_service: tuple[str, ...] = ()
    starting: tuple[str, ...] = ()
    draining: tuple[str, ...] = ()
    warm: tuple[str, ...] = ()
    running: tuple[str, ...] = ()


@dataclass(frozen=True)
class Memory:
    """What the scaler keeps about a pool from one run to the next, in the pool-state table.

    `last_request_at` is the time of the last request received. A gateway task that is replaced
    starts a heartbeat row with no request in it, so the heartbeats alone would make a gateway
    deployment look like an idle pool. `last_in_flight_at` is the time of the last run that saw a
    request in flight or held: a request can outlast `idle_minutes`, and the pool is not idle the
    moment it ends.

    The other two time how long a pool has been measurably full, which is what makes it grow.
    `above_target_since` is the time of the first of the runs in a row that saw the requests in
    flight, averaged over the instances in service, above `target_concurrency`, and `None` when the
    last run did not. `waiting_since` pairs, in sorted order, each instance in service that reports
    waiting requests while no instance of the pool has room with the time of the first of the runs
    in a row that saw it so. It is empty as soon as an instance has room.

    The last two time how long a pool of two instances or more has been light, which is what makes
    it shrink. `last_scale_out_at` is the time of the last run that grew the pool, and `None` when
    no run is known to have. `low_load_since` is the time of the first of the runs in a row that
    saw two instances or more in service and the requests in flight at most (n - 1) times half of
    `target_concurrency`, and `None` when the last run did not, or when it removed an instance.

    The next four serve the fail-safes (section 5.1). `last_gateway_at` is the time of the last fresh
    gateway heartbeat seen (when it was written), and `None` when no run has seen one: nobody can
    reach the fleet without the gateway, so a pool none has reported on for 10 minutes goes to
    zero. `launch_suspended` is the mark of the launch loop, the attribute
    `rhi_common.tables.LAUNCH_SUSPENDED` of the pool's memory item: a run sets it when three
    launches were abandoned in a row, and only the admin sets it back to false. `suspended_at` is
    the time of the run that set it, and `None` while no mark stands: a mark that is false while
    `suspended_at` is set is a mark the admin has cleared. `abandons_counted_from` is the time of the
    run that found the mark cleared, and `None` before any did: the edge counts no abandoned launch
    before it, since the group's activity history still holds the ones that set the mark.

    The last three serve the rebalance (sections 5.1 and 5.5). `queuing_since` pairs, in sorted
    order, each instance in service that reports waiting requests with the time of the first of
    the runs in a row that saw it so, whether or not another instance has room: unlike
    `waiting_since`, it goes on while the pool grows. `rebalance_number` is the pool's rebalance
    number, 0 until a run first raises it. `last_rebalance_at` is the time of the run that last
    raised it, and `None` when none has.

    `outside_sessions_since` only reports (`rhi_scaler.outside_sessions`, sections 11.3 and 17): it
    is the time of the first of the runs in a row that saw an instance of the pool in service while
    no session was on, and `None` when the last run did not. `decide` neither reads nor sets it.
    """

    last_request_at: int | None = None
    last_in_flight_at: int | None = None
    above_target_since: int | None = None
    waiting_since: tuple[tuple[str, int], ...] = ()
    last_scale_out_at: int | None = None
    low_load_since: int | None = None
    last_gateway_at: int | None = None
    launch_suspended: bool = False
    suspended_at: int | None = None
    abandons_counted_from: int | None = None
    queuing_since: tuple[tuple[str, int], ...] = ()
    rebalance_number: int = 0
    last_rebalance_at: int | None = None
    outside_sessions_since: int | None = None


@dataclass(frozen=True)
class PoolInputs:
    """One pool, as a run sees it.

    `gateway_heartbeats` and `instance_heartbeats` are the pool's fresh rows of the heartbeats
    table (`rhi_common.records.is_fresh`); the decision counts no other. `abandoned_launches` is
    the number of launches abandoned in a row, counted by the edge from the group's activity
    history from `Memory.abandons_counted_from`: a launch that reaches service ends a row, and one
    that found no capacity is not abandoned.
    """

    config: Pool
    mode: Mode
    supply: Supply
    gateway_heartbeats: tuple[GatewayHeartbeat, ...] = ()
    instance_heartbeats: tuple[InstanceHeartbeat, ...] = ()
    abandoned_launches: int = 0
    memory: Memory = Memory()


@dataclass(frozen=True)
class Inputs:
    """Everything one run decides from: the time, and every pool."""

    now: int
    pools: tuple[PoolInputs, ...]


class Reason(StrEnum):
    """Why a pool's decision is what it is. A decision names one."""

    # A held request asked a pool at zero for one instance.
    WAKE = "wake"
    # No request received and none in flight for `idle_minutes`: the pool goes to zero.
    IDLE = "idle"
    # The pool is stopped (`disabled`) and releases its idle instances.
    STOPPED = "stopped"
    # The instances in service have been full for 60 seconds: the pool grows by one instance.
    SCALE_OUT = "scale-out"
    # The load has fit in one instance fewer at half of `target_concurrency` for 10 minutes, 15
    # minutes after the last scale-out: one instance that reports nothing running leaves.
    SCALE_IN = "scale-in"
    # Requests are held for a pool at zero whose mode does not let them wake it.
    NOT_WOKEN = "not-woken"
    # No fresh gateway heartbeat for 10 minutes: nobody can reach the fleet, the pool goes to zero.
    GATEWAY_SILENT = "gateway-silent"
    # Three launches abandoned in a row: the group's launches are suspended and nothing wakes or
    # grows the pool until the admin clears the mark.
    LAUNCH_SUSPENDED = "launch-suspended"
    # An instance has waited for 60 seconds while another runs below half of `target_concurrency`:
    # the pool's rebalance number goes up by one, in a run where no other rule acts.
    REBALANCE = "rebalance"
    # No rule asks for another size, or for another rebalance number.
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class PoolDecision:
    """What a run decides for one pool.

    `desired` is the desired capacity to set, an absolute number: setting it again changes
    nothing. `lift_protection` names, in sorted order, every instance whose scale-in protection to
    lift before the capacity is lowered: Auto Scaling lowers the desired capacity of a group whose
    instances are all protected and terminates none of them. `suspend_launches` is whether the
    group's launches are to be suspended (true) or resumed (false); setting it again changes
    nothing. `memory` is what to store for the next run. `rebalance_number` is the pool's rebalance
    number, for the pool's note: one more than the memory held in a run that raises it, and the
    same number in every other run.
    """

    pool: str
    desired: int
    lift_protection: tuple[str, ...]
    suspend_launches: bool
    memory: Memory
    reason: Reason
    rebalance_number: int = 0


@dataclass(frozen=True)
class Decision:
    """A run's decision: one `PoolDecision` per pool, in the order of the inputs."""

    pools: tuple[PoolDecision, ...]
