"""Independent checks of the scaler's AWS boundary for issue 82."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock, call

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.aws_io import Clients, PoolSettings, TableNames, apply, read
from rhi_scaler.model import Decision, Memory, PoolDecision, Reason, Supply

NOW = 1_800_000_000
TABLES = TableNames("heartbeats", "pool-state")
EMPTY_MEMORY = Memory()


def settings(name: str) -> PoolSettings:
    pool = parse_pools({name: {
        "engine": "mock", "instance_types": ["g6e.xlarge"], "prices": {"g6e.xlarge": 1.86}, "max_instances": 4,
        "warm_instances": 2, "wake": "on-demand", "target_concurrency": 8,
        "context_length": 32768, "largest_output": 4096,
    }})[0]
    return PoolSettings(pool, f"group-{name}", f"targets-{name}", Mode.SCHEDULED_ONLY)


def wire(item: dict[str, Any]) -> dict[str, Any]:
    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def activity(start: int, end: int, status: str, message: str = "", warm: bool = False) -> dict[str, Any]:
    return {
        "Description": "Launching a new EC2 instance" + (" into warm pool" if warm else "") + ": instance-x",
        "StartTime": datetime.fromtimestamp(start, UTC),
        "EndTime": datetime.fromtimestamp(end, UTC),
        "StatusCode": status,
        "StatusMessage": message,
    }


def edge(
    *, instances: list[dict[str, Any]] | None = None, desired: int = 0,
    suspended: tuple[str, ...] = (), activities: list[dict[str, Any]] | None = None,
    records: list[dict[str, Any]] | None = None, warm: list[dict[str, Any]] | None = None,
    heartbeats: list[dict[str, Any]] | None = None, names: tuple[str, ...] = ("alpha",),
) -> tuple[Clients, list[dict[str, Any]]]:
    autoscaling, elbv2, dynamodb = Mock(), Mock(), Mock()
    autoscaling.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [{
        "DesiredCapacity": desired, "Instances": instances or [],
        "SuspendedProcesses": [{"ProcessName": name} for name in suspended],
    }]}

    def scaling_pages(operation: str) -> Mock:
        key, rows = ("Instances", warm or []) if operation == "describe_warm_pool" else ("Activities", activities or [])
        # Deliberately spread both kinds of input across pages.
        return Mock(paginate=Mock(return_value=[{key: rows[:1]}, {key: rows[1:]}]))

    autoscaling.get_paginator.side_effect = scaling_pages
    elbv2.describe_target_health.return_value = {"TargetHealthDescriptions": [
        {"Target": {"Id": "instance-keep"}, "TargetHealth": {"State": "healthy"}},
        {"Target": {"Id": "instance-release"}, "TargetHealth": {"State": "healthy"}},
    ]}

    def query_pages(**parameters: Any) -> list[dict[str, Any]]:
        assert parameters["KeyConditionExpression"] == "#pool = :pool"
        assert parameters["ExpressionAttributeNames"] == {"#pool": "pool"}
        pool_name = parameters["ExpressionAttributeValues"][":pool"]["S"]
        assert pool_name in names
        assert parameters["ConsistentRead"] is True
        rows = (heartbeats or []) if parameters["TableName"] == TABLES.heartbeats else (records or [])
        rows = [row for row in rows if row["pool"] == pool_name]
        return [{"Items": [wire(row) for row in rows[:1]]}, {"Items": [wire(row) for row in rows[1:]]}]

    dynamodb.get_paginator.return_value = Mock(paginate=Mock(side_effect=query_pages))
    writes: list[dict[str, Any]] = []

    def put(**parameters: Any) -> None:
        assert parameters["TableName"] == TABLES.pool_state
        deserializer = TypeDeserializer()
        writes.append({key: deserializer.deserialize(value) for key, value in parameters["Item"].items()})

    dynamodb.put_item.side_effect = put
    return Clients(autoscaling, elbv2, dynamodb), writes


def member(name: str, state: str, protected: bool = True) -> dict[str, Any]:
    return {"InstanceId": name, "LifecycleState": state, "ProtectedFromScaleIn": protected}


def choose(desired: int, *, lift: tuple[str, ...] = (), suspend: bool = False, memory: Memory = EMPTY_MEMORY) -> Decision:
    return Decision((PoolDecision("alpha", desired, lift, suspend, memory, Reason.UNCHANGED),))


def test_reads_supply_fresh_demand_and_each_configured_pool() -> None:
    clients, _ = edge(
        desired=2, names=("alpha", "beta"),
        instances=[member("instance-keep", "InService", False), member("instance-start", "Pending:Wait"),
                   member("instance-leave", "Terminating:Wait")],
        warm=[member("warm-stop", "Warmed:Stopped"), member("warm-run", "Warmed:Running"),
              member("warm-start", "Warmed:Pending:Wait")],
        heartbeats=[GatewayHeartbeat("fresh", "alpha", 2, 3, NOW - 1, NOW).to_item(),
                    GatewayHeartbeat("stale", "alpha", 9, 9, NOW - 30, NOW - 21).to_item(),
                    InstanceHeartbeat("instance-keep", "alpha", 1, 0, "digest", "release", "serving", NOW).to_item()],
        records=[{"pool": "alpha", "record": "memory", "last_request_at": NOW - 40,
                  "launch_suspended": True, "suspended_at": NOW - 60, "abandons_counted_from": NOW - 100}],
    )
    run = read(clients, TABLES, [settings("alpha"), settings("beta")], NOW)
    assert [pool.config.name for pool in run.inputs.pools] == ["alpha", "beta"]
    pool = run.inputs.pools[0]
    assert pool.mode == Mode.SCHEDULED_ONLY
    assert pool.supply == Supply(2, ("instance-keep",), ("instance-start",), ("instance-leave",),
                                 ("warm-run", "warm-start", "warm-stop"),
                                 ("instance-keep", "instance-leave", "instance-start", "warm-run", "warm-start"))
    assert [(hb.task, hb.in_flight, hb.held) for hb in pool.gateway_heartbeats] == [("fresh", 2, 3)]
    assert [hb.instance for hb in pool.instance_heartbeats] == ["instance-keep"]
    assert pool.memory == Memory(last_request_at=NOW - 40, launch_suspended=True,
                                 suspended_at=NOW - 60, abandons_counted_from=NOW - 100)
    assert run.inputs.pools[1].memory == Memory()
    assert run.groups["alpha"].protected == frozenset({"instance-start", "instance-leave"})
    clients.autoscaling.describe_auto_scaling_groups.assert_has_calls([
        call(AutoScalingGroupNames=["group-alpha"]), call(AutoScalingGroupNames=["group-beta"]),
    ])
    clients.elbv2.describe_target_health.assert_has_calls([
        call(TargetGroupArn="targets-alpha"), call(TargetGroupArn="targets-beta"),
    ])


def test_mode_override() -> None:
    clients, _ = edge(records=[{"pool": "alpha", "record": "mode", "mode": "disabled",
                               "set_by": "admin", "set_at": NOW - 1}])
    pool = read(clients, TABLES, [settings("alpha")], NOW).inputs.pools[0]
    assert pool.mode == Mode.DISABLED and pool.memory == Memory()


def test_ordered_mutations_and_note_and_memory() -> None:
    clients, writes = edge(desired=2, instances=[member("instance-keep", "InService", False),
                                               member("instance-release", "InService")],
                          activities=[activity(NOW - 80, NOW - 70, "Failed", "InsufficientInstanceCapacity")])
    run = read(clients, TABLES, [settings("alpha")], NOW)
    clients.autoscaling.reset_mock()
    memory = Memory(last_request_at=NOW - 1, waiting_since=(("instance-keep", NOW - 2),), launch_suspended=True)
    apply(clients, TABLES, run, choose(1, lift=("instance-release",), suspend=True, memory=memory))
    assert clients.autoscaling.method_calls == [
        call.set_instance_protection(AutoScalingGroupName="group-alpha", InstanceIds=["instance-keep"], ProtectedFromScaleIn=True),
        call.set_instance_protection(AutoScalingGroupName="group-alpha", InstanceIds=["instance-release"], ProtectedFromScaleIn=False),
        call.set_desired_capacity(AutoScalingGroupName="group-alpha", DesiredCapacity=1, HonorCooldown=False),
        call.suspend_processes(AutoScalingGroupName="group-alpha", ScalingProcesses=["Launch"]),
    ]
    assert writes == [
        {"pool": "alpha", "record": "note", "capacity_asked": 1, "no_capacity_at": NOW - 70, "written_at": NOW},
        {"pool": "alpha", "record": "memory", "last_request_at": NOW - 1,
         "launch_suspended": True, "waiting_since": {"instance-keep": NOW - 2}},
    ]


def test_unchanged_still_writes_records() -> None:
    clients, writes = edge(desired=1, instances=[member("instance-keep", "InService")], suspended=("Launch", "AZRebalance"))
    run = read(clients, TABLES, [settings("alpha")], NOW)
    clients.autoscaling.reset_mock()
    apply(clients, TABLES, run, choose(1, suspend=True, memory=Memory(launch_suspended=True)))
    assert clients.autoscaling.method_calls == []
    assert writes == [
        {"pool": "alpha", "record": "note", "capacity_asked": 1, "written_at": NOW},
        {"pool": "alpha", "record": "memory", "launch_suspended": True, "waiting_since": {}},
    ]


def test_returning_instance_is_protected_and_only_launch_resumes() -> None:
    clients, writes = edge(desired=1, instances=[member("instance-keep", "InService", False)], suspended=("Launch", "AZRebalance"))
    run = read(clients, TABLES, [settings("alpha")], NOW)
    clients.autoscaling.reset_mock()
    apply(clients, TABLES, run, choose(1))
    assert clients.autoscaling.method_calls == [
        call.set_instance_protection(AutoScalingGroupName="group-alpha", InstanceIds=["instance-keep"], ProtectedFromScaleIn=True),
        call.resume_processes(AutoScalingGroupName="group-alpha", ScalingProcesses=["Launch"]),
    ]
    assert len(writes) == 2


@pytest.mark.parametrize(("message", "boundary", "expected"), [
    ("launch lifecycle action abandoned", None, 3),
    ("InsufficientInstanceCapacity", None, 0),
    ("launch lifecycle action abandoned", NOW - 10, 0),
])
def test_abandon_count_excludes_capacity_and_pre_clear_history(message: str, boundary: int | None, expected: int) -> None:
    records: list[dict[str, Any]] = [{"pool": "alpha", "record": "memory"}]
    if boundary is not None:
        records[0]["abandons_counted_from"] = boundary
    history = [activity(NOW - offset - 10, NOW - offset, "Cancelled", message, warm=offset == 40)
               for offset in (20, 40, 60)]
    history += [activity(NOW - 100, NOW - 90, "Successful"),
                activity(NOW - 130, NOW - 120, "Failed", "launch lifecycle action abandoned")]
    clients, _ = edge(activities=history, records=records)
    assert read(clients, TABLES, [settings("alpha")], NOW).inputs.pools[0].abandoned_launches == expected


def test_note_retains_last_capacity_failure_across_a_success() -> None:
    clients, writes = edge(activities=[
        activity(NOW - 60, NOW - 50, "Successful"),
        activity(NOW - 120, NOW - 110, "Failed", "InsufficientInstanceCapacity"),
    ])
    run = read(clients, TABLES, [settings("alpha")], NOW)
    apply(clients, TABLES, run, choose(0))
    assert writes[0].get("no_capacity_at") == NOW - 110


@pytest.mark.parametrize(("intervals", "expected"), [
    ([(300, 400, "Cancelled"), (200, 500, "Successful")], 0),
    ([(300, 400, "Successful"), (200, 500, "Cancelled")], 1),
])
def test_abandon_count_uses_completion_times(intervals: list[tuple[int, int, str]], expected: int) -> None:
    # AWS sorts by StartTime, not EndTime. Concurrent launches may finish in the opposite order.
    history = [activity(NOW - 1000 + start, NOW - 1000 + end, status, "launch lifecycle action abandoned")
               for start, end, status in intervals]
    clients, _ = edge(activities=history)
    assert read(clients, TABLES, [settings("alpha")], NOW).inputs.pools[0].abandoned_launches == expected
