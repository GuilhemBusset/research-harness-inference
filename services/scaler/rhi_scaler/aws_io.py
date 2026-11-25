"""The scaler's edge: what a run reads from AWS and the tables, and how a decision becomes calls.

`decide` (`rhi_scaler.decide`) is pure. This module is everything around it (system design, section
5.1, the inputs table): `read` turns Auto Scaling, target health and the two tables into `Inputs`,
and `apply` turns the `Decision` into Auto Scaling calls and the writes of the pool-state table.

What is read, for each pool:

| What | From |
| --- | --- |
| Desired capacity, instances by lifecycle state and their scale-in protection, suspended processes | `DescribeAutoScalingGroups` |
| Which instances serve | `DescribeTargetHealth` on the pool's target group |
| Launches abandoned in a row, the last launch that found no capacity, the last launch that failed and its cause | `DescribeScalingActivities` |
| Demand and engine load: the fresh heartbeats | the heartbeat table, by pool |
| Mode (the configured mode when no record exists), the scaler's memory, and the pool's session (none when no record exists) | the pool-state table, by pool |

And once for the whole run, the session switch (`rhi_common.records.SessionSwitch`), from its item
in the pool-state table. It only reports (`rhi_scaler.outside_sessions`, and the session gauge of
`rhi_scaler.metrics`) and `decide` never sees it.
A missing item reads as no record. A read that fails, or an item that is not a switch, is not taken
for no record: the run keeps the error (`Run.session_error`), decides and applies from what it read
of demand, and the handler fails it only after that.

The supply, from the lifecycle states and target health:

| State | Supply |
| --- | --- |
| `InService`, its target `healthy` | in service |
| `InService`, its target `draining` | draining |
| `InService` otherwise (not registered yet, initial, unhealthy), and `Pending*` | starting |
| `Terminating*`, `Detaching` | draining |

`running` is every instance of the group's list but one `Terminated` or `Detached`. The group keeps
no stopped instance (section 4.5), so its list is every instance it has.

Abandoned launches (section 5.4). An abandoned launch is a launch activity that failed or was
cancelled because its launch hook was abandoned. A launch that found
no capacity is not one, whatever else it says: it is only the time the note carries (section 5.3).
The count reads the whole history the group returns and goes by the time each activity completed:
Auto Scaling orders activities by the time they started, and launches that run side by side can
complete in the other order. Only an abandon that completed after the later of the last launch that
reached service and `Memory.abandons_counted_from` counts, since the group's history still holds
the abandons that set the mark the admin cleared. A launch that failed for any other reason neither
counts nor ends the row. The time of the last launch that found no capacity is read from the whole
history too, whatever came after it.

The last launch that failed (section 5.4) is the launch activity that failed or was cancelled and
completed last, by the time AWS gives and not by its whole
second; of two that completed at the same instant, the one that started last. Its cause is one of
three words (`rhi_common.records.LaunchFailure`): `insufficient-capacity` for a launch that found
no capacity, `quota` for one the account's quota of running instances, which EC2 counts in vCPUs,
refused, and `launch-failed` for any other, an abandoned launch included. A cause is told from the
EC2 error code the activity's message carries or, since Auto Scaling also reports both without a
code, from the fixed words of its sentence (`NO_CAPACITY_SIGNS`, `QUOTA_SIGNS`); nothing else of
the message is read, and none of it is kept. The time of the last launch that found no capacity is
told the same way.

`apply` makes, for each pool, these calls and only those that change something, in this order:

1. protect from scale-in every instance `InService` that is unprotected and that the decision does
   not name (one whose protection an earlier run lifted and that stayed): an unprotected one could
   be chosen by AWS in place of the one the scaler picked (section 4.3);
2. lift the protection of each instance the decision names that is protected;
3. set the desired capacity, which is absolute;
4. suspend the `Launch` process, or resume `Launch` alone: zone rebalancing, `AZRebalance`, stays
   suspended for good, so no call here names it.

Then it writes the pool's note (`rhi_common.records.ScalerNote`) and its memory record, on every
run, whether or not a call was made. The note carries the decision's rebalance number, raised or
not, which is how the number reaches the gateway (section 5.5). The note is put whole; the memory
record is updated (`memory_update`), so that what the admin CLI writes in it survives: the mark
`launch_suspended` once cleared, and who cleared it and when.

The note also says what the pool was asked and what waits (section 5.4), and `supply_note` is that
rule:

| In the note | From |
| --- | --- |
| `requested` | The decision: what the pool's session asks for now |
| `capacity_asked` | The decision's desired capacity, the "asked of AWS" |
| `starting`, `in_service`, `draining` | The supply the run read |
| `waiting` | `capacity_asked` less the instances starting and in service, never below zero |
| `waiting_since` | The time of the first of the runs in a row that saw `waiting` above zero; absent while nothing waits |
| `launch_failure` | Absent while nothing waits. Otherwise `launches-suspended` when the decision suspends the pool's launches, or else the cause of the last launch that failed, if it failed since `waiting_since` |

The time the wait began is kept from one run to the next in the pool's memory record, under the
attribute `short_since`: the name `waiting_since` is taken there by the instances whose requests
wait. `apply` stores it with the decision's memory, and removes it once nothing waits. It returns
the notes it wrote, by pool, so that the metrics send the same numbers (`rhi_scaler.metrics`).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from rhi_common.pool_config import Pool
from rhi_common.records import (
    GATEWAY_SOURCE,
    INSTANCE_SOURCE,
    MODE_RECORD,
    SESSION_RECORD,
    GatewayHeartbeat,
    InstanceHeartbeat,
    LaunchFailure,
    Mode,
    PoolState,
    RecordError,
    ScalerNote,
    Session,
    SessionSwitch,
    is_fresh,
)
from rhi_common.tables import LAUNCH_SUSPENDED, MEMORY_RECORD, SESSION_SWITCH_KEY
from rhi_scaler.model import SESSION_MAX_HOURS, Decision, Inputs, Memory, PoolDecision, PoolInputs, Supply

# The process a launch loop suspends, and the only one this module ever names.
LAUNCH_PROCESS = "Launch"
# What tells a failed launch's cause in its activity's message, compared in lower case: the EC2
# error codes, and the words Auto Scaling's own sentences have when the message carries no code.
# A launch that found no capacity, On-Demand or Spot:
NO_CAPACITY_SIGNS = (
    "insufficientinstancecapacity",
    "do not have sufficient",
    "insufficient capacity",
    "no spot capacity available",
)
# A launch the account's quota of running instances refused. EC2 counts that quota in vCPUs.
QUOTA_SIGNS = (
    "vcpulimitexceeded",
    "instancelimitexceeded",
    "maxspotinstancecountexceeded",
    "vcpu limit",
    "quota allows for",
    "instance(s) are already running",
)
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
class FailedLaunch:
    """A launch that failed: when it completed, and why, in one of the three words a launch gives."""

    at: int
    cause: LaunchFailure


@dataclass(frozen=True)
class GroupState:
    """What `apply` compares a decision with: the group as the run read it.

    `in_service` lists the instances whose lifecycle state is `InService`, `protected` those of the
    group's own list that are protected from scale-in. `no_capacity_at` is the time of the last
    launch that found no capacity, `None` when the history read holds none. `last_failed_launch` is
    the last launch that failed, `None` when the history read holds none.
    """

    group: str
    desired: int
    in_service: tuple[str, ...]
    protected: frozenset[str]
    launch_suspended: bool
    no_capacity_at: int | None
    last_failed_launch: FailedLaunch | None = None


@dataclass(frozen=True)
class Run:
    """What one run read: the decision's inputs, each pool's group by pool name, and the session
    switch, `None` when it has no record. `session_error` is why the switch could not be read, and
    then `session` is `None` too. `session_read_at` is when its read returned, in milliseconds."""

    inputs: Inputs
    groups: Mapping[str, GroupState]
    session: SessionSwitch | None = None
    session_error: Exception | None = None
    session_read_at: int | None = None


@dataclass(frozen=True)
class Clients:
    """The AWS SDK clients a run uses: `autoscaling`, `elbv2`, `dynamodb` and `cloudwatch`
    (low-level clients)."""

    autoscaling: Any
    elbv2: Any
    dynamodb: Any
    cloudwatch: Any


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


def _supply(group: Mapping[str, Any], health: Mapping[str, str]) -> Supply:
    in_service: list[str] = []
    starting: list[str] = []
    draining: list[str] = []
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
        elif state.startswith(("Terminating", "Detaching")):
            draining.append(name)
    return Supply(
        desired=int(group["DesiredCapacity"]),
        in_service=tuple(sorted(in_service)),
        starting=tuple(sorted(starting)),
        draining=tuple(sorted(draining)),
        running=tuple(sorted(set(running))),
    )


def _seconds(moment: datetime) -> int:
    return int(moment.timestamp())


def _is_launch(description: str) -> bool:
    return description.startswith("launching")


def _failure_cause(message: str) -> LaunchFailure:
    """Why a launch failed, from the signs its status message carries: one of three words."""
    said = message.lower()
    if any(sign in said for sign in NO_CAPACITY_SIGNS):
        return LaunchFailure.INSUFFICIENT_CAPACITY
    if any(sign in said for sign in QUOTA_SIGNS):
        return LaunchFailure.QUOTA
    return LaunchFailure.LAUNCH_FAILED


@dataclass(frozen=True)
class LaunchHistory:
    """What the group's activity history says: the launches abandoned in a row, the time of the
    last launch that found no capacity, and the last launch that failed, whatever its cause (each
    `None` when the history read holds none)."""

    abandoned_in_a_row: int
    no_capacity_at: int | None
    last_failed: FailedLaunch | None = None


def launch_history(activities: Iterable[Mapping[str, Any]], counted_from: int | None) -> LaunchHistory:
    """Reads the activities, in any order, by the time each completed (its start while it runs)."""
    reached_service: int | None = counted_from
    abandons: list[int] = []
    no_capacity_at: int | None = None
    last_failed: FailedLaunch | None = None
    # When the last failed launch completed and started, as AWS gives them: two launches can
    # complete within one second, and the later of them is the last.
    last_failed_at: tuple[datetime, datetime] | None = None
    for activity in activities:
        description = str(activity.get("Description", "")).lower()
        if not _is_launch(description):
            continue
        status = activity.get("StatusCode")
        message = str(activity.get("StatusMessage", ""))
        completed: datetime = activity.get("EndTime") or activity["StartTime"]
        moment = _seconds(completed)
        if status == "Successful":
            reached_service = moment if reached_service is None else max(reached_service, moment)
            continue
        if status not in ("Failed", "Cancelled"):
            continue
        cause = _failure_cause(message)
        failed_at = (completed, activity.get("StartTime") or completed)
        if last_failed_at is None or failed_at > last_failed_at:
            last_failed, last_failed_at = FailedLaunch(moment, cause), failed_at
        if cause is LaunchFailure.INSUFFICIENT_CAPACITY:
            no_capacity_at = moment if no_capacity_at is None else max(no_capacity_at, moment)
        elif "abandon" in message.lower():
            abandons.append(moment)
    # The row starts at the later of the last launch that reached service and the memory's start.
    abandoned = sum(1 for moment in abandons if reached_service is None or moment > reached_service)
    return LaunchHistory(abandoned, no_capacity_at, last_failed)


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
    "outside_sessions_since",
    "short_since",
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
        outside_sessions_since=_time(item, "outside_sessions_since"),
        short_since=_time(item, "short_since"),
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
    targets = clients.elbv2.describe_target_health(TargetGroupArn=settings.target_group)
    health = {
        target["Target"]["Id"]: target["TargetHealth"]["State"] for target in targets.get("TargetHealthDescriptions", [])
    }

    records = {item.get("record"): item for item in _query_pool(clients, tables.pool_state, settings.config.name)}
    mode_item = records.get(MODE_RECORD)
    mode = settings.configured_mode if mode_item is None else PoolState.from_item(mode_item).mode
    memory = memory_from_item(records.get(MEMORY_RECORD))
    # The pool's session, read on every run with the rest of the pool's records.
    session_item = records.get(SESSION_RECORD)
    session = None if session_item is None else Session.from_item(session_item)

    activities = _pages(clients.autoscaling, "describe_scaling_activities", "Activities", AutoScalingGroupName=settings.group)
    history = launch_history(activities, memory.abandons_counted_from)
    gateways, instances = _heartbeats(_query_pool(clients, tables.heartbeats, settings.config.name), now)

    inputs = PoolInputs(
        config=settings.config,
        mode=mode,
        supply=_supply(group, health),
        gateway_heartbeats=gateways,
        instance_heartbeats=instances,
        abandoned_launches=history.abandoned_in_a_row,
        memory=memory,
        session=session,
    )
    own = group.get("Instances", [])
    state = GroupState(
        group=settings.group,
        desired=int(group["DesiredCapacity"]),
        in_service=tuple(sorted(i["InstanceId"] for i in own if i["LifecycleState"] == "InService")),
        protected=frozenset(i["InstanceId"] for i in own if i.get("ProtectedFromScaleIn")),
        launch_suspended=any(p.get("ProcessName") == LAUNCH_PROCESS for p in group.get("SuspendedProcesses", [])),
        no_capacity_at=history.no_capacity_at,
        last_failed_launch=history.last_failed,
    )
    return inputs, state


def read_session(clients: Clients, tables: TableNames) -> SessionSwitch | None:
    """The session switch; `None` when it has no record. An item that is not a switch raises
    `RecordError`."""
    key = {name: {"S": value} for name, value in SESSION_SWITCH_KEY.items()}
    item = clients.dynamodb.get_item(TableName=tables.pool_state, Key=key, ConsistentRead=True).get("Item")
    return None if item is None else SessionSwitch.from_item(_plain(item))


def read_session_at(clients: Clients, tables: TableNames) -> tuple[SessionSwitch | None, int]:
    """The session switch, as `read_session` reads it, and when the read returned, in milliseconds:
    whatever change it reads was written by then (`rhi_scaler.metrics.session_series`)."""
    switch = read_session(clients, tables)
    return switch, int(time.time() * 1000)


def read(
    clients: Clients,
    tables: TableNames,
    pools: Iterable[PoolSettings],
    now: int,
    session_max_hours: int = SESSION_MAX_HOURS,
) -> Run:
    """Everything one run decides from, at the time `now`, for each pool in the order given, with
    the longest a session may last as the settings give it, and the session switch."""
    read_pools = [_read_pool(clients, tables, settings, now) for settings in pools]
    session, session_error, session_read_at = None, None, None
    try:
        session, session_read_at = read_session_at(clients, tables)
    except (ClientError, BotoCoreError, RecordError) as error:
        # It only reports: a wake that demand calls for must not wait on it.
        session_error = error
    return Run(
        inputs=Inputs(
            now=now, pools=tuple(inputs for inputs, _ in read_pools), session_max_hours=session_max_hours
        ),
        groups={inputs.config.name: state for inputs, state in read_pools},
        session=session,
        session_error=session_error,
        session_read_at=session_read_at,
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


def supply_note(pool: PoolDecision, read: PoolInputs, state: GroupState, now: int) -> ScalerNote:
    """The pool's note for a run at the time `now`: what its decision asks, what the run read of
    its supply and of its group, and what waits, since when and why (section 5.4)."""
    supply = read.supply
    waiting = max(0, pool.desired - len(supply.starting) - len(supply.in_service))
    waiting_since: int | None = None
    launch_failure: LaunchFailure | None = None
    if waiting > 0:
        # The first of the runs in a row that saw the pool wait: this one, unless the memory holds one.
        waiting_since = now if read.memory.short_since is None else read.memory.short_since
        failed = state.last_failed_launch
        if pool.suspend_launches:
            launch_failure = LaunchFailure.LAUNCHES_SUSPENDED
        elif failed is not None and failed.at >= waiting_since:
            launch_failure = failed.cause
    return ScalerNote(
        pool=pool.pool,
        capacity_asked=pool.desired,
        no_capacity_at=state.no_capacity_at,
        written_at=now,
        rebalance_number=pool.rebalance_number,
        requested=pool.requested,
        starting=len(supply.starting),
        in_service=len(supply.in_service),
        draining=len(supply.draining),
        waiting=waiting,
        waiting_since=waiting_since,
        launch_failure=launch_failure,
    )


def apply(clients: Clients, tables: TableNames, run: Run, decision: Decision) -> dict[str, ScalerNote]:
    """Makes each pool's decision true, calling Auto Scaling only for what differs, then writes the
    pool's note and memory. Returns the notes it wrote, by pool."""
    read_pools = {read.config.name: read for read in run.inputs.pools}
    notes: dict[str, ScalerNote] = {}
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

        before = read_pools[pool.pool]
        note = supply_note(pool, before, state, run.inputs.now)
        _put(clients, tables.pool_state, note.to_item())
        # The time the wait began goes on in the memory, and leaves it once nothing waits.
        memory = dataclasses.replace(pool.memory, short_since=note.waiting_since)
        clients.dynamodb.update_item(**memory_update(tables.pool_state, pool.pool, before.memory, memory))
        notes[pool.pool] = note
    return notes
