"""The scaler's edge: what a run reads from AWS and the tables, and how a decision becomes calls.

`decide` (`rhi_scaler.decide`) is pure. This module is everything around it (system design, section
5.1, the inputs table): `read` turns Auto Scaling, target health and the two tables into `Inputs`,
and `apply` turns the `Decision` into Auto Scaling calls and the writes of the pool-state table.

What is read, for each pool:

| What | From |
| --- | --- |
| Desired capacity, instances by lifecycle state and their scale-in protection, suspended processes | `DescribeAutoScalingGroups` |
| The warm pool's instances | `DescribeWarmPool` |
| Which instances serve | `DescribeTargetHealth` on the pool's target group |
| Launches abandoned in a row, the last launch that found no capacity | `DescribeScalingActivities` |
| Demand and engine load: the fresh heartbeats | the heartbeat table, by pool |
| Mode (the configured mode when no record exists) and the scaler's memory | the pool-state table, by pool |

And once a run, for the session gauge (`rhi_scaler.metrics`), not for the decision: the session
switch, the pool-state item `_session` / `switch` (`rhi_common.records.SessionSwitch`), by
`read_session`. A missing item reads as off.

The supply, from the lifecycle states and target health:

| State | Supply |
| --- | --- |
| `InService`, its target `healthy` | in service |
| `InService`, its target `draining` | draining |
| `InService` otherwise (not registered yet, initial, unhealthy), and `Pending*` | starting |
| `Terminating*`, `Detaching`, and a `Warmed:*` state in the group's own list (going back to the warm pool) | draining |
| In the warm pool, any state but `Warmed:Terminating*` and `Warmed:Terminated` | warm |

`running` is every instance of the group's own list but one `Terminated` or `Detached`, and every
instance of the warm pool that is `Warmed:Running` or `Warmed:Pending*` (being prepared).

Abandoned launches (section 5.4). An abandoned launch is a launch activity, into service or into the
warm pool, that failed or was cancelled because its launch hook was abandoned. A launch that found
no capacity is not one, whatever else it says: it is only the time the note carries (section 5.3).
The count reads the whole history the group returns and goes by the time each activity completed:
Auto Scaling orders activities by the time they started, and launches that run side by side can
complete in the other order. Only an abandon that completed after the later of the last launch that
reached service and `Memory.abandons_counted_from` counts, since the group's history still holds
the abandons that set the mark the admin cleared. A launch that failed for any other reason neither
counts nor ends the row. The time of the last launch that found no capacity is read from the whole
history too, whatever came after it.

`apply` makes, for each pool, these calls and only those that change something, in this order:

1. protect from scale-in every instance `InService` that is unprotected and that the decision does
   not name: whether an instance back from the warm pool keeps its protection is not settled, and
   an unprotected one could be chosen by AWS in place of the one the scaler picked (section 4.3);
2. lift the protection of each instance the decision names that is protected;
3. set the desired capacity, which is absolute and draws from the warm pool (evidence E10);
4. suspend the `Launch` process, or resume `Launch` alone: zone rebalancing, `AZRebalance`, stays
   suspended for good, so no call here names it.

Then it writes the pool's note (`rhi_common.records.ScalerNote`) and its memory record, on every
run, whether or not a call was made. The note carries the decision's rebalance number, raised or
not, which is how the number reaches the gateway (section 5.5). The note is put whole; the memory
record is updated (`memory_update`), so that what the admin CLI writes in it survives: the mark
`launch_suspended` once cleared, and who cleared it and when.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from rhi_common.pool_config import Pool
from rhi_common.records import (
    GATEWAY_SOURCE,
    INSTANCE_SOURCE,
    MODE_RECORD,
    SESSION_POOL,
    SWITCH_RECORD,
    GatewayHeartbeat,
    InstanceHeartbeat,
    Mode,
    PoolState,
    RecordError,
    ScalerNote,
    is_fresh,
    session_is_on,
)
from rhi_common.tables import LAUNCH_SUSPENDED, MEMORY_RECORD
from rhi_scaler.model import Decision, Inputs, Memory, PoolInputs, Supply

# The process a launch loop suspends, and the only one this module ever names.
LAUNCH_PROCESS = "Launch"
# The EC2 error code of a launch that found no capacity, as the group's activity carries it.
NO_CAPACITY = "InsufficientInstanceCapacity"
# How many instances one SetInstanceProtection call may name.
PROTECTION_BATCH = 50

_DESERIALIZER = TypeDeserializer()
_SERIALIZER = TypeSerializer()


@dataclass(frozen=True)
class PoolSettings:
    """One pool as the scaler's settings give it: its configuration, its group, its target group,
    and the mode it is in when the pool-state table holds no mode record."""

    config: Pool
    group: str
    target_group: str
    configured_mode: Mode


@dataclass(frozen=True)
class TableNames:
    """The deployed names of the two tables the scaler reads and writes."""

    heartbeats: str
    pool_state: str


@dataclass(frozen=True)
class GroupState:
    """What `apply` compares a decision with: the group as the run read it.

    `in_service` lists the instances whose lifecycle state is `InService`, `protected` those of the
    group's own list that are protected from scale-in. `no_capacity_at` is the time of the last
    launch that found no capacity, `None` when the history read holds none.
    """

    group: str
    desired: int
    in_service: tuple[str, ...]
    protected: frozenset[str]
    launch_suspended: bool
    no_capacity_at: int | None


@dataclass(frozen=True)
class Run:
    """What one run read: the decision's inputs, and each pool's group by pool name."""

    inputs: Inputs
    groups: Mapping[str, GroupState]


@dataclass(frozen=True)
class Clients:
    """The AWS SDK clients a run uses: `autoscaling`, `elbv2` and `dynamodb` (low-level clients)."""

    autoscaling: Any
    elbv2: Any
    dynamodb: Any


# Reading.


def _pages(client: Any, operation: str, key: str, **parameters: object) -> Iterator[dict[str, Any]]:
    for page in client.get_paginator(operation).paginate(**parameters):
        yield from page.get(key, [])


def _plain(item: Mapping[str, Any]) -> dict[str, Any]:
    """A low-level item as plain values: numbers as `Decimal`, as boto3's table resource gives them."""
    return {name: _DESERIALIZER.deserialize(value) for name, value in item.items()}


def _query_pool(clients: Clients, table: str, pool: str) -> Iterator[dict[str, Any]]:
    for item in _pages(
        clients.dynamodb,
        "query",
        "Items",
        TableName=table,
        KeyConditionExpression="#pool = :pool",
        ExpressionAttributeNames={"#pool": "pool"},
        ExpressionAttributeValues={":pool": {"S": pool}},
        ConsistentRead=True,
    ):
        yield _plain(item)


def _supply(group: Mapping[str, Any], warm_pool: Iterable[Mapping[str, Any]], health: Mapping[str, str]) -> Supply:
    in_service: list[str] = []
    starting: list[str] = []
    draining: list[str] = []
    warm: list[str] = []
    running: list[str] = []
    for instance in group.get("Instances", []):
        name, state = instance["InstanceId"], instance["LifecycleState"]
        if state in ("Terminated", "Detached"):
            continue
        running.append(name)
        if state == "InService":
            target = health.get(name)
            (in_service if target == "healthy" else draining if target == "draining" else starting).append(name)
        elif state.startswith("Pending"):
            starting.append(name)
        elif state.startswith(("Terminating", "Detaching", "Warmed:")):
            draining.append(name)
    for instance in warm_pool:
        name, state = instance["InstanceId"], instance["LifecycleState"]
        if state.startswith(("Warmed:Terminating", "Warmed:Terminated")):
            continue
        warm.append(name)
        if state == "Warmed:Running" or state.startswith("Warmed:Pending"):
            running.append(name)
    return Supply(
        desired=int(group["DesiredCapacity"]),
        in_service=tuple(sorted(in_service)),
        starting=tuple(sorted(starting)),
        draining=tuple(sorted(draining)),
        warm=tuple(sorted(warm)),
        running=tuple(sorted(set(running))),
    )


def _seconds(moment: datetime) -> int:
    return int(moment.timestamp())


def _is_launch(description: str) -> bool:
    return description.startswith("launching") or (
        "warm pool" in description and ("from warm pool" in description or "out of warm pool" in description)
    )


def _into_warm_pool(description: str) -> bool:
    return "into warm pool" in description or "to warm pool" in description


@dataclass(frozen=True)
class LaunchHistory:
    """What the group's activity history says: the launches abandoned in a row, and the time of the
    last launch that found no capacity (`None` when the history read holds none)."""

    abandoned_in_a_row: int
    no_capacity_at: int | None


def launch_history(activities: Iterable[Mapping[str, Any]], counted_from: int | None) -> LaunchHistory:
    """Reads the activities, in any order, by the time each completed (its start while it runs)."""
    reached_service: int | None = counted_from
    abandons: list[int] = []
    no_capacity_at: int | None = None
    for activity in activities:
        description = str(activity.get("Description", "")).lower()
        if not _is_launch(description):
            continue
        status = activity.get("StatusCode")
        message = str(activity.get("StatusMessage", ""))
        moment = _seconds(activity.get("EndTime") or activity["StartTime"])
        if status == "Successful":
            if not _into_warm_pool(description):
                reached_service = moment if reached_service is None else max(reached_service, moment)
            continue
        if status not in ("Failed", "Cancelled"):
            continue
        if NO_CAPACITY in message:
            no_capacity_at = moment if no_capacity_at is None else max(no_capacity_at, moment)
        elif "abandon" in message.lower():
            abandons.append(moment)
    # The row starts at the later of the last launch that reached service and the memory's start.
    abandoned = sum(1 for moment in abandons if reached_service is None or moment > reached_service)
    return LaunchHistory(abandoned, no_capacity_at)


def _heartbeats(
    items: Iterable[Mapping[str, Any]], now: int
) -> tuple[tuple[GatewayHeartbeat, ...], tuple[InstanceHeartbeat, ...]]:
    gateways: list[GatewayHeartbeat] = []
    instances: list[InstanceHeartbeat] = []
    for item in items:
        source = item.get("source")
        try:
            if isinstance(source, str) and source.startswith(GATEWAY_SOURCE):
                gateways.append(GatewayHeartbeat.from_item(item))
            elif isinstance(source, str) and source.startswith(INSTANCE_SOURCE):
                instances.append(InstanceHeartbeat.from_item(item))
        except RecordError:
            # A row nobody can read is no heartbeat.
            continue
    return (
        tuple(heartbeat for heartbeat in gateways if is_fresh(heartbeat, now)),
        tuple(heartbeat for heartbeat in instances if is_fresh(heartbeat, now)),
    )


def _time(item: Mapping[str, Any], name: str) -> int | None:
    value = item.get(name)
    if value is None:
        return None
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise RecordError(f"`{name}` of the memory record is not a time")


# The times of the memory record, each left out of it while it is `None`.
_MEMORY_TIMES = (
    "last_request_at",
    "last_in_flight_at",
    "above_target_since",
    "last_scale_out_at",
    "low_load_since",
    "last_gateway_at",
    "suspended_at",
    "abandons_counted_from",
    "last_rebalance_at",
)
# The attributes of the memory record that the scaler alone writes: all of them but the keys and
# `launch_suspended`, which the admin clears.
MEMORY_ATTRIBUTES = ("waiting_since", "queuing_since", "rebalance_number", *_MEMORY_TIMES)


def memory_from_item(item: Mapping[str, Any] | None) -> Memory:
    """The memory record as `Memory`; an empty one when the pool has no record yet."""
    if item is None:
        return Memory()
    waiting = item.get("waiting_since") or {}
    if not isinstance(waiting, Mapping):
        raise RecordError("`waiting_since` of the memory record is not a map")
    queuing = item.get("queuing_since") or {}
    if not isinstance(queuing, Mapping):
        raise RecordError("`queuing_since` of the memory record is not a map")
    suspended = item.get(LAUNCH_SUSPENDED, False)
    if not isinstance(suspended, bool):
        raise RecordError(f"`{LAUNCH_SUSPENDED}` of the memory record is not a boolean")
    return Memory(
        last_request_at=_time(item, "last_request_at"),
        last_in_flight_at=_time(item, "last_in_flight_at"),
        above_target_since=_time(item, "above_target_since"),
        waiting_since=tuple(sorted((str(name), _time(waiting, name) or 0) for name in waiting)),
        last_scale_out_at=_time(item, "last_scale_out_at"),
        low_load_since=_time(item, "low_load_since"),
        last_gateway_at=_time(item, "last_gateway_at"),
        launch_suspended=suspended,
        suspended_at=_time(item, "suspended_at"),
        abandons_counted_from=_time(item, "abandons_counted_from"),
        queuing_since=tuple(sorted((str(name), _time(queuing, name) or 0) for name in queuing)),
        # A record stored before the scaler kept a number holds none.
        rebalance_number=_time(item, "rebalance_number") or 0,
        last_rebalance_at=_time(item, "last_rebalance_at"),
    )


def memory_to_item(pool: str, memory: Memory) -> dict[str, Any]:
    """The memory record of a pool, in plain values; a time that is `None` is left out, and so
    are the instances that wait when there are none and the rebalance number while it is 0."""
    item: dict[str, Any] = {
        "pool": pool,
        "record": MEMORY_RECORD,
        LAUNCH_SUSPENDED: memory.launch_suspended,
        "waiting_since": dict(memory.waiting_since),
    }
    for name in _MEMORY_TIMES:
        value = getattr(memory, name)
        if value is not None:
            item[name] = value
    if memory.queuing_since:
        item["queuing_since"] = dict(memory.queuing_since)
    if memory.rebalance_number:
        item["rebalance_number"] = memory.rebalance_number
    return item


def _read_pool(clients: Clients, tables: TableNames, settings: PoolSettings, now: int) -> tuple[PoolInputs, GroupState]:
    groups = clients.autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[settings.group])
    group = groups["AutoScalingGroups"][0]
    warm_pool = list(_pages(clients.autoscaling, "describe_warm_pool", "Instances", AutoScalingGroupName=settings.group))
    targets = clients.elbv2.describe_target_health(TargetGroupArn=settings.target_group)
    health = {
        target["Target"]["Id"]: target["TargetHealth"]["State"] for target in targets.get("TargetHealthDescriptions", [])
    }

    records = {item.get("record"): item for item in _query_pool(clients, tables.pool_state, settings.config.name)}
    mode_item = records.get(MODE_RECORD)
    mode = settings.configured_mode if mode_item is None else PoolState.from_item(mode_item).mode
    memory = memory_from_item(records.get(MEMORY_RECORD))

    activities = _pages(clients.autoscaling, "describe_scaling_activities", "Activities", AutoScalingGroupName=settings.group)
    history = launch_history(activities, memory.abandons_counted_from)
    gateways, instances = _heartbeats(_query_pool(clients, tables.heartbeats, settings.config.name), now)

    inputs = PoolInputs(
        config=settings.config,
        mode=mode,
        supply=_supply(group, warm_pool, health),
        gateway_heartbeats=gateways,
        instance_heartbeats=instances,
        abandoned_launches=history.abandoned_in_a_row,
        memory=memory,
    )
    own = group.get("Instances", [])
    state = GroupState(
        group=settings.group,
        desired=int(group["DesiredCapacity"]),
        in_service=tuple(sorted(i["InstanceId"] for i in own if i["LifecycleState"] == "InService")),
        protected=frozenset(i["InstanceId"] for i in own if i.get("ProtectedFromScaleIn")),
        launch_suspended=any(p.get("ProcessName") == LAUNCH_PROCESS for p in group.get("SuspendedProcesses", [])),
        no_capacity_at=history.no_capacity_at,
    )
    return inputs, state


def read_session(clients: Clients, tables: TableNames) -> bool:
    """Whether the session switch reads on. A missing item reads as off; one that is not the
    switch's record raises `RecordError`."""
    found = clients.dynamodb.get_item(
        TableName=tables.pool_state,
        Key={"pool": {"S": SESSION_POOL}, "record": {"S": SWITCH_RECORD}},
        ConsistentRead=True,
    )
    item = found.get("Item")
    return session_is_on(None if item is None else _plain(item))


def read(clients: Clients, tables: TableNames, pools: Iterable[PoolSettings], now: int) -> Run:
    """Everything one run decides from, at the time `now`, for each pool in the order given."""
    read_pools = [_read_pool(clients, tables, settings, now) for settings in pools]
    return Run(
        inputs=Inputs(now=now, pools=tuple(inputs for inputs, _ in read_pools)),
        groups={inputs.config.name: state for inputs, state in read_pools},
    )


# Acting.


def _set_protection(clients: Clients, group: str, instances: list[str], protected: bool) -> None:
    for start in range(0, len(instances), PROTECTION_BATCH):
        clients.autoscaling.set_instance_protection(
            AutoScalingGroupName=group,
            InstanceIds=instances[start : start + PROTECTION_BATCH],
            ProtectedFromScaleIn=protected,
        )


def _put(clients: Clients, table: str, item: Mapping[str, Any]) -> None:
    clients.dynamodb.put_item(
        TableName=table, Item={name: _SERIALIZER.serialize(value) for name, value in item.items()}
    )


def memory_update(table: str, pool: str, before: Memory, after: Memory) -> dict[str, Any]:
    """The UpdateItem call, as the low-level client takes it, that stores `after` as the pool's
    memory record, given the memory `before` that the run read.

    Each of the scaler's own attributes is set, or removed while it has no value. The mark
    `launch_suspended` is the admin's to clear: the call sets it only in the run that raises it, and
    otherwise leaves it as it stands. An admin who clears it while a run that read it set is under
    way therefore keeps it cleared, and the next run finds it so. Any other attribute that is not
    the scaler's, such as who cleared the mark and when, is left as it is.
    """
    item = memory_to_item(pool, after)
    raised = after.launch_suspended and not before.launch_suspended
    present = [name for name in MEMORY_ATTRIBUTES if name in item] + ([LAUNCH_SUSPENDED] if raised else [])
    absent = [name for name in MEMORY_ATTRIBUTES if name not in item]
    names = {f"#s{n}": name for n, name in enumerate(present)} | {f"#r{n}": name for n, name in enumerate(absent)}
    expression = "SET " + ", ".join(f"#s{n} = :s{n}" for n in range(len(present)))
    if absent:
        expression += " REMOVE " + ", ".join(f"#r{n}" for n in range(len(absent)))
    return {
        "TableName": table,
        "Key": {"pool": {"S": pool}, "record": {"S": MEMORY_RECORD}},
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": {f":s{n}": _SERIALIZER.serialize(item[name]) for n, name in enumerate(present)},
    }


def apply(clients: Clients, tables: TableNames, run: Run, decision: Decision) -> None:
    """Makes each pool's decision true, calling Auto Scaling only for what differs, then writes the
    pool's note and memory."""
    for pool in decision.pools:
        state = run.groups[pool.pool]
        named = set(pool.lift_protection)
        protect = [name for name in state.in_service if name not in state.protected and name not in named]
        lift = sorted(name for name in named if name in state.protected)
        if protect:
            _set_protection(clients, state.group, protect, True)
        if lift:
            _set_protection(clients, state.group, lift, False)
        if pool.desired != state.desired:
            clients.autoscaling.set_desired_capacity(
                AutoScalingGroupName=state.group, DesiredCapacity=pool.desired, HonorCooldown=False
            )
        if pool.suspend_launches and not state.launch_suspended:
            clients.autoscaling.suspend_processes(AutoScalingGroupName=state.group, ScalingProcesses=[LAUNCH_PROCESS])
        elif not pool.suspend_launches and state.launch_suspended:
            clients.autoscaling.resume_processes(AutoScalingGroupName=state.group, ScalingProcesses=[LAUNCH_PROCESS])

        note = ScalerNote(
            pool=pool.pool,
            capacity_asked=pool.desired,
            no_capacity_at=state.no_capacity_at,
            written_at=run.inputs.now,
            rebalance_number=pool.rebalance_number,
        )
        _put(clients, tables.pool_state, note.to_item())
        before = next(read.memory for read in run.inputs.pools if read.config.name == pool.pool)
        clients.dynamodb.update_item(**memory_update(tables.pool_state, pool.pool, before, pool.memory))
