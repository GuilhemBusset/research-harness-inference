"""What a run of the scaler decides from, and what it decides (system design, section 5.1).

One scaler is the only writer of a pool's size. Each run reads the same inputs and `decide`
(`rhi_scaler.decide`) computes, from them alone, the size every pool should have. This module holds
the two shapes and nothing that acts: the edge of the scaler reads AWS and the tables into `Inputs`
and carries a `Decision` out.

A time is a whole number of seconds since the epoch, as in `rhi_common.records`, and `None` when
it is unknown. An instance is named by its ID.

The rules that come later (more than one instance, the fail-safes, the ceilings and the spend, the
schedule) extend these shapes.
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
    """

    last_request_at: int | None = None
    last_in_flight_at: int | None = None


@dataclass(frozen=True)
class PoolInputs:
    """One pool, as a run sees it.

    `gateway_heartbeats` and `instance_heartbeats` are the pool's fresh rows of the heartbeats
    table (`rhi_common.records.is_fresh`); the decision counts no other. `abandoned_launches` is
    the number of launches abandoned in a row, counted from the group's activity history.
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
    # The pool is stopped (`disabled` or `budget-latched`) and releases its idle instances.
    STOPPED = "stopped"
    # Requests are held for a pool at zero whose mode does not let them wake it.
    NOT_WOKEN = "not-woken"
    # No rule asks for another size.
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class PoolDecision:
    """What a run decides for one pool.

    `desired` is the desired capacity to set, an absolute number: setting it again changes
    nothing. `lift_protection` names, in sorted order, every instance whose scale-in protection to
    lift before the capacity is lowered: Auto Scaling lowers the desired capacity of a group whose
    instances are all protected and terminates none of them. `memory` is what to store for the
    next run.
    """

    pool: str
    desired: int
    lift_protection: tuple[str, ...]
    suspend_launches: bool
    memory: Memory
    reason: Reason


@dataclass(frozen=True)
class Decision:
    """A run's decision: one `PoolDecision` per pool, in the order of the inputs."""

    pools: tuple[PoolDecision, ...]
