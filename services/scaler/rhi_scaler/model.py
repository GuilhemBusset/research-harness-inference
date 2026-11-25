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
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode, Session

# The longest session, in hours, for a run whose settings give none: the configuration's default.
SESSION_MAX_HOURS = 12


@dataclass(frozen=True)
class Supply:
    """What a pool's group holds, from Auto Scaling and target health.

    `desired` is the group's desired capacity. The three states each list their instances: in
    service, starting (launched and not yet in service) and draining (leaving service). `running`
    lists every running instance of the group in any state.
    """

    desired: int
    in_service: tuple[str, ...] = ()
    starting: tuple[str, ...] = ()
    draining: tuple[str, ...] = ()
    running: tuple[str, ...] = ()


@dataclass(frozen=True)
class Memory:
    """What the scaler keeps about a pool from one run to the next, in the pool-state table.

    Three attributes serve the launch loop (section 5.1). `launch_suspended` is its mark, the
    attribute `rhi_common.tables.LAUNCH_SUSPENDED` of the pool's memory item: a run sets it when
    three launches were abandoned in a row, and only the admin sets it back to false.
    `suspended_at` is the time of the run that set it, and `None` while no mark stands: a mark that
    is false while `suspended_at` is set is a mark the admin has cleared. `abandons_counted_from`
    is the time of the run that found the mark cleared, and `None` before any did: the edge counts
    no abandoned launch before it, since the group's activity history still holds the ones that set
    the mark.

    Three serve the rebalance (sections 5.1 and 5.5). `queuing_since` pairs, in sorted order, each
    instance in service that reports waiting requests with the time of the first of the runs in a
    row that saw it so, whether or not another instance has room. `rebalance_number` is the pool's
    rebalance number, 0 until a run first raises it. `last_rebalance_at` is the time of the run
    that last raised it, and `None` when none has.

    Seven are from the rules that sized a pool by its load, by its idleness and by what the gateway
    reported: `last_request_at`, `last_in_flight_at`, `above_target_since`, `waiting_since`,
    `last_scale_out_at`, `low_load_since` and `last_gateway_at`. No rule reads demand any more:
    `decide` reads none of them and leaves each at its empty value, so a run removes what an
    earlier one stored.

    `outside_sessions_since` only reports (`rhi_scaler.outside_sessions`, sections 11.3 and 17): it
    is the time of the first of the runs in a row that saw an instance of the pool in service while
    no session was on, and `None` when the last run did not. `decide` neither reads nor sets it.

    `short_since` only reports too (section 5.4): it is the time of the first of the runs in a row
    that saw the pool wait for capacity, with fewer instances starting and in service than were
    asked of AWS, and `None` when the last run did not. The edge sets it when it writes the pool's
    note (`rhi_scaler.aws_io.apply`), and `decide` neither reads nor sets it. It is not
    `waiting_since`.
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
    short_since: int | None = None


@dataclass(frozen=True)
class PoolInputs:
    """One pool, as a run sees it.

    `gateway_heartbeats` and `instance_heartbeats` are the pool's fresh rows of the heartbeats
    table (`rhi_common.records.is_fresh`). The decision reads the instances' rows, to order a
    release and for the rebalance, and counts no other. It reads neither `gateway_heartbeats` nor
    `mode`: the edge still fills them, and no rule sizes a pool by them. `abandoned_launches` is
    the number of launches abandoned in a row, counted by the edge from the group's activity
    history from `Memory.abandons_counted_from`: a launch that reaches service ends a row, and one
    that found no capacity is not abandoned. `session` is the pool's session as its record gives
    it (`rhi_common.records.Session`), and `None` for a pool with no such record: whether it is
    active at the time of the run is for `rhi_common.sessions` to say, not for the edge.
    """

    config: Pool
    mode: Mode
    supply: Supply
    gateway_heartbeats: tuple[GatewayHeartbeat, ...] = ()
    instance_heartbeats: tuple[InstanceHeartbeat, ...] = ()
    abandoned_launches: int = 0
    memory: Memory = Memory()
    session: Session | None = None


@dataclass(frozen=True)
class Inputs:
    """Everything one run decides from: the time, every pool, and the longest a session may last,
    in hours, which is the bound every pool's session is read under (`rhi_common.sessions`)."""

    now: int
    pools: tuple[PoolInputs, ...]
    session_max_hours: int = SESSION_MAX_HOURS


class Reason(StrEnum):
    """Why a pool's decision is what it is. A decision names one."""

    # The pool's session asks for more instances than the group was asked for: the pool goes to
    # the session's number, all at once.
    PROVISION = "provision"
    # The pool's session asks for fewer instances than the pool holds, or for none: the capacity
    # goes down, and the instances in service beyond the session's number are named for release.
    RELEASE = "release"
    # Three launches abandoned in a row: the group's launches are suspended and the pool is not
    # raised until the admin clears the mark.
    LAUNCH_SUSPENDED = "launch-suspended"
    # An instance has waited for 60 seconds while another runs below half of `target_concurrency`:
    # the pool's rebalance number goes up by one, in a run where no other rule acts.
    REBALANCE = "rebalance"
    # The pool is at the size its session asks for, and its rebalance number stays.
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
    same number in every other run. `requested` is what the pool's session asks for at the time of
    the run (`rhi_common.sessions.requested`): its number of instances, at most the pool's
    `max_instances`, while it is active, and 0 before its start, from its end on, and for a pool
    with no session.
    """

    pool: str
    desired: int
    lift_protection: tuple[str, ...]
    suspend_launches: bool
    memory: Memory
    reason: Reason
    rebalance_number: int = 0
    requested: int = 0


@dataclass(frozen=True)
class Decision:
    """A run's decision: one `PoolDecision` per pool, in the order of the inputs."""

    pools: tuple[PoolDecision, ...]
