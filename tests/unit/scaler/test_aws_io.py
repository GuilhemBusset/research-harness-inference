"""The scaler's edge (`rhi_scaler.aws_io`): reads and writes against stubbed AWS clients."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.stub import ANY, Stubber
from moto import mock_aws

from rhi_common.pool_config import parse_pools
from rhi_common.records import InstanceHeartbeat, ScalerNote, Session
from rhi_scaler.aws_io import (
    FORMER_MEMORY_ATTRIBUTES,
    RECORD_READS,
    Clients,
    PoolSettings,
    RecordsNotRead,
    Run,
    TableNames,
    apply,
    launch_history,
    memory_from_item,
    memory_to_item,
    memory_update,
    read,
)
from rhi_scaler.decide import decide
from rhi_scaler.model import Decision, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
GROUP = "coursework-group"
TARGETS = "coursework-targets"
TABLES = TableNames(heartbeats="heartbeats-table", pool_state="pool-state-table")
POOL = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["g6e.xlarge"],
            "prices": {"g6e.xlarge": 1.86},
            "max_instances": 4,
            "warm_instances": 2,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]
EMPTY = Memory()
SETTINGS = PoolSettings(config=POOL, group=GROUP, target_group=TARGETS)
# The two records a run asks the pool-state table for, by their keys.
RECORD_KEYS = [{"pool": {"S": "coursework"}, "record": {"S": record}} for record in ("memory", "session")]


class Aws:
    """Three stubbed clients: every call must be one the test queued, in the order queued. So a read
    of the tables is the pool's two records by their keys and its instances' heartbeats, and no other."""

    def __init__(self) -> None:
        options = {"region_name": "us-east-1", "aws_access_key_id": "stub", "aws_secret_access_key": "stub"}
        self.clients = Clients(
            autoscaling=boto3.client("autoscaling", **options),
            elbv2=boto3.client("elbv2", **options),
            dynamodb=boto3.client("dynamodb", **options),
            cloudwatch=boto3.client("cloudwatch", **options),
        )
        self.autoscaling = Stubber(self.clients.autoscaling)
        self.elbv2 = Stubber(self.clients.elbv2)
        self.dynamodb = Stubber(self.clients.dynamodb)

    def queue_read(
        self,
        *,
        instances: Sequence[dict[str, Any]] = (),
        health: Mapping[str, str] | None = None,
        desired: int = 0,
        suspended: tuple[str, ...] = ("AZRebalance",),
        records: Sequence[dict[str, Any]] = (),
        activities: Sequence[dict[str, Any]] = (),
        heartbeats: Sequence[dict[str, Any]] = (),
        tables: bool = True,
    ) -> None:
        group = {
            "AutoScalingGroupName": GROUP,
            "MinSize": 0,
            "MaxSize": 4,
            "DesiredCapacity": desired,
            "DefaultCooldown": 300,
            "AvailabilityZones": ["us-east-1a"],
            "HealthCheckType": "ELB",
            "CreatedTime": datetime(2026, 1, 1, tzinfo=UTC),
            "Instances": [{**instance, "AvailabilityZone": "us-east-1a", "HealthStatus": "Healthy"} for instance in instances],
            "SuspendedProcesses": [{"ProcessName": name, "SuspensionReason": "by the stack"} for name in suspended],
        }
        self.autoscaling.add_response("describe_auto_scaling_groups", {"AutoScalingGroups": [group]}, {"AutoScalingGroupNames": [GROUP]})
        self.autoscaling.add_response(
            "describe_scaling_activities",
            {"Activities": [{"ActivityId": f"activity-{n}", "AutoScalingGroupName": GROUP, "Cause": "the scaler", **a} for n, a in enumerate(activities)]},
            {"AutoScalingGroupName": GROUP},
        )
        descriptions = [{"Target": {"Id": name}, "TargetHealth": {"State": state}} for name, state in (health or {}).items()]
        self.elbv2.add_response("describe_target_health", {"TargetHealthDescriptions": descriptions}, {"TargetGroupArn": TARGETS})
        if tables:
            self.queue_records(records)
            self.dynamodb.add_response("query", {"Items": [serialize(item) for item in heartbeats]}, INSTANCE_ROWS)

    def queue_records(self, found: Sequence[dict[str, Any]], asked: Sequence[str] = ("memory", "session"), left: Sequence[str] = ()) -> None:
        """One answer to a request for the records `asked`: those `found`, and the keys `left` unprocessed."""

        def keys(records: Sequence[str]) -> list[dict[str, Any]]:
            return [key for key in RECORD_KEYS if key["record"]["S"] in records]

        answer: dict[str, Any] = {"Responses": {TABLES.pool_state: [serialize(item) for item in found]}}
        if left:
            answer["UnprocessedKeys"] = {TABLES.pool_state: {"Keys": keys(left), "ConsistentRead": True}}
        expected = {"RequestItems": {TABLES.pool_state: {"Keys": keys(asked), "ConsistentRead": True}}}
        self.dynamodb.add_response("batch_get_item", answer, expected)

    def expect(self, operation: str, **parameters: object) -> None:
        self.autoscaling.add_response(operation, {}, {"AutoScalingGroupName": GROUP, **parameters})

    def expect_writes(self, note: dict[str, Any] | None = None, memory: tuple[Memory, Memory] | None = None) -> None:
        """The note put whole, then the memory updated from what the run read to what it decided."""
        self.dynamodb.add_response(
            "put_item", {}, {"TableName": TABLES.pool_state, "Item": ANY if note is None else serialize(note)}
        )
        update = None if memory is None else memory_update(TABLES.pool_state, "coursework", *memory)
        self.dynamodb.add_response("update_item", {}, update)


def serialize(item: dict[str, Any]) -> dict[str, Any]:
    serializer = TypeSerializer()
    return {name: serializer.serialize(value) for name, value in item.items()}


def plain(item: dict[str, Any]) -> dict[str, Any]:
    deserializer = TypeDeserializer()
    return {name: deserializer.deserialize(value) for name, value in item.items()}


# The one query of a read: the pool's rows of the heartbeat table whose source is an instance.
INSTANCE_ROWS = {
    "TableName": TABLES.heartbeats,
    "KeyConditionExpression": "#pool = :pool AND begins_with(#source, :instance)",
    "ExpressionAttributeNames": {"#pool": "pool", "#source": "source"},
    "ExpressionAttributeValues": {":pool": {"S": "coursework"}, ":instance": {"S": "instance#"}},
    "ConsistentRead": True,
}


def create_tables(client: Any) -> None:
    keys = {TABLES.pool_state: [("pool", "HASH"), ("record", "RANGE")], TABLES.heartbeats: [("pool", "HASH"), ("source", "RANGE")]}
    for table, schema in keys.items():
        client.create_table(
            TableName=table,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in schema],
            KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in schema],
        )


@pytest.fixture
def aws() -> Iterator[Aws]:
    aws = Aws()
    with aws.autoscaling, aws.elbv2, aws.dynamodb:
        yield aws
        aws.autoscaling.assert_no_pending_responses()
        aws.elbv2.assert_no_pending_responses()
        aws.dynamodb.assert_no_pending_responses()


def instance(name: str, state: str, protected: bool = True) -> dict[str, Any]:
    return {"InstanceId": name, "LifecycleState": state, "ProtectedFromScaleIn": protected}


def at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def launch(status: str, ended: int, message: str = "") -> dict[str, Any]:
    activity = {
        "Description": "Launching a new EC2 instance: instance-x",
        "StatusCode": status,
        "StartTime": at(ended - 30),
        "EndTime": at(ended),
    }
    return {**activity, "StatusMessage": message} if message else activity


ABANDONED = "the instance's launch lifecycle action was abandoned"
NO_CAPACITY = "Could not launch On-Demand Instances. InsufficientInstanceCapacity - no capacity in the zone"


def decision(desired: int, lift: tuple[str, ...] = (), suspend: bool = False, memory: Memory = EMPTY) -> Decision:
    return Decision(pools=(PoolDecision("coursework", desired, lift, suspend, memory, Reason.UNCHANGED),))


def read_one(aws: Aws) -> Run:
    return read(aws.clients, TABLES, [SETTINGS], NOW)


def test_a_group_becomes_the_supply_from_its_own_list_and_target_health(aws: Aws) -> None:
    # The Stubber refuses a call that is not queued: the read asks Auto Scaling for the group and its
    # activities, and for no warm pool.
    aws.queue_read(
        desired=4,
        instances=[
            instance("instance-a", "InService"),
            instance("instance-b", "InService"),
            instance("instance-c", "InService"),
            instance("instance-d", "Pending:Wait"),
            instance("instance-e", "Terminating:Wait"),
            instance("instance-f", "Terminated"),
        ],
        health={"instance-a": "healthy", "instance-b": "draining", "instance-c": "initial"},
        heartbeats=[
            InstanceHeartbeat("instance-a", "coursework", 2, 0, "sha256:0", "0", "serving", NOW - 1).to_item(),
            InstanceHeartbeat("instance-c", "coursework", 9, 9, "sha256:0", "0", "serving", NOW - 60).to_item(),
            {"pool": "coursework", "source": "instance#instance-b", "written_at": NOW},
        ],
    )
    run = read_one(aws)
    pool = run.inputs.pools[0]
    assert pool.supply == Supply(
        desired=4,
        in_service=("instance-a",),
        starting=("instance-c", "instance-d"),
        draining=("instance-b", "instance-e"),
        running=("instance-a", "instance-b", "instance-c", "instance-d", "instance-e"),
        # In service for the group, their target not healthy: protected like the one that serves.
        not_serving=("instance-b", "instance-c"),
    )
    # Only the fresh, readable heartbeats.
    assert [heartbeat.instance for heartbeat in pool.instance_heartbeats] == ["instance-a"]
    group = run.groups["coursework"]
    assert group.desired == 4 and group.in_service == ("instance-a", "instance-b", "instance-c")
    assert not group.launch_suspended


def test_a_session_that_ended_releases_a_protected_instance_whose_target_is_not_healthy(aws: Aws) -> None:
    ended = Session("coursework", 2, NOW - 3600, NOW, set_by="instructor", set_at=NOW - 3600)
    aws.queue_read(
        desired=2,
        instances=[instance("instance-a", "InService"), instance("instance-b", "Pending:Wait", protected=False)],
        health={"instance-a": "unhealthy"},
        records=[ended.to_item()],
    )
    run = read_one(aws)
    assert run.inputs.pools[0].supply.starting == ("instance-a", "instance-b")
    # The decision names the one the group protects, and not the one it still launches.
    decided = decide(run.inputs)
    assert [(pool.desired, pool.lift_protection) for pool in decided.pools] == [(0, ("instance-a",))]
    aws.expect("set_instance_protection", InstanceIds=["instance-a"], ProtectedFromScaleIn=False)
    aws.expect("set_desired_capacity", DesiredCapacity=0, HonorCooldown=False)
    aws.expect_writes()
    apply(aws.clients, TABLES, run, decided)


def test_the_memory_record_is_read(aws: Aws) -> None:
    memory = Memory(launch_suspended=True, suspended_at=NOW - 100, abandons_counted_from=NOW - 900)
    aws.queue_read(records=[memory_to_item("coursework", memory)])
    pool = read_one(aws).inputs.pools[0]
    assert pool.memory == memory
    assert memory_from_item(None) == Memory()


def test_a_run_takes_from_the_tables_only_the_memory_the_session_and_the_instances_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    session = Session("coursework", 2, NOW - 600, NOW + 3600, set_by="instructor", set_at=NOW - 900)
    memory = Memory(rebalance_number=4, last_rebalance_at=NOW - 300)
    heartbeat = InstanceHeartbeat("instance-a", "coursework", 2, 0, "sha256:0", "0", "serving", NOW - 1)
    with mock_aws():
        aws = Aws()
        create_tables(aws.clients.dynamodb)
        # What the tables may hold beside: the pool's note, and what an earlier release left, the
        # pool's mode record, the session switch and a gateway's heartbeat row.
        for item in (
            memory_to_item("coursework", memory),
            session.to_item(),
            {"pool": "coursework", "record": "mode", "mode": "disabled", "set_by": "admin", "set_at": NOW - 5},
            ScalerNote(pool="coursework", capacity_asked=2, no_capacity_at=None, written_at=NOW - 60).to_item(),
            {"pool": "_session", "record": "switch", "on": True, "set_by": "admin", "set_at": NOW - 5},
        ):
            aws.clients.dynamodb.put_item(TableName=TABLES.pool_state, Item=serialize(item))
        gateway_row = {
            "pool": "coursework",
            "source": "gateway#task-1",
            "in_flight": 2,
            "held": 1,
            "last_request_at": NOW - 3,
            "written_at": NOW - 2,
            "expires_at": NOW - 2 + 3600,
        }
        for item in (heartbeat.to_item(), gateway_row):
            aws.clients.dynamodb.put_item(TableName=TABLES.heartbeats, Item=serialize(item))
        # Every item DynamoDB returns to the run, whatever the call that asked for it.
        returned: list[dict[str, Any]] = []

        def keep(parsed: dict[str, Any], **_: object) -> None:
            returned.extend(parsed.get("Items", []))
            returned.extend(item for items in parsed.get("Responses", {}).values() for item in items)

        aws.clients.dynamodb.meta.events.register("after-call.dynamodb", keep)
        aws.queue_read(desired=1, instances=[instance("instance-a", "InService")], health={"instance-a": "healthy"}, tables=False)
        with aws.autoscaling, aws.elbv2:
            run = read_one(aws)
    assert sorted(item.get("record", item.get("source"))["S"] for item in returned) == ["instance#instance-a", "memory", "session"]
    # The pool's inputs hold what was read, and neither a mode nor a gateway's heartbeat.
    names = ("instance-a",)
    assert run.inputs.pools == (
        PoolInputs(
            config=POOL,
            supply=Supply(desired=1, in_service=names, running=names),
            instance_heartbeats=(heartbeat,),
            memory=memory,
            session=session,
        ),
    )


def test_a_record_dynamodb_left_unprocessed_is_asked_for_again(aws: Aws) -> None:
    session = Session("coursework", 2, NOW - 600, NOW + 3600, set_by="instructor", set_at=NOW - 900)
    aws.queue_read(tables=False)
    aws.queue_records([memory_to_item("coursework", EMPTY)], left=["session"])
    aws.queue_records([session.to_item()], asked=["session"])
    aws.dynamodb.add_response("query", {"Items": []}, INSTANCE_ROWS)
    assert read_one(aws).inputs.pools[0].session == session


def test_a_record_left_unprocessed_on_every_request_fails_the_read() -> None:
    # The run fails, rather than read a pool with no session, which would give its instances back.
    aws = Aws()
    aws.queue_read(tables=False)
    for request in range(RECORD_READS):
        aws.queue_records([], asked=["session"] if request else ["memory", "session"], left=["session"])
    with aws.autoscaling, aws.elbv2, aws.dynamodb, pytest.raises(RecordsNotRead):
        read_one(aws)
    aws.dynamodb.assert_no_pending_responses()


def test_the_pool_s_session_is_read_with_its_records_and_a_pool_with_no_item_has_none(aws: Aws) -> None:
    session = Session("coursework", 2, NOW - 600, NOW + 3600, set_by="instructor", set_at=NOW - 900)
    aws.queue_read(records=[memory_to_item("coursework", EMPTY), session.to_item()])
    run = read(aws.clients, TABLES, [SETTINGS], NOW, 6)
    assert run.inputs.pools[0].session == session
    # The longest session of the settings goes with it, for the decision to read the session under.
    assert run.inputs.session_max_hours == 6

    aws.queue_read(records=[memory_to_item("coursework", EMPTY)])
    assert read_one(aws).inputs.pools[0].session is None


def test_a_decision_becomes_calls_in_order_and_the_note_carries_no_capacity(aws: Aws) -> None:
    aws.queue_read(
        desired=3,
        instances=[instance("instance-a", "InService", protected=False), instance("instance-b", "InService"), instance("instance-c", "InService")],
        health=dict.fromkeys(("instance-a", "instance-b", "instance-c"), "healthy"),
        activities=[launch("Failed", NOW - 120, NO_CAPACITY), launch("Successful", NOW - 4000)],
    )
    run = read_one(aws)
    memory = Memory(abandons_counted_from=NOW - 30)
    # The Stubber refuses a call out of the queued order.
    aws.expect("set_instance_protection", InstanceIds=["instance-a"], ProtectedFromScaleIn=True)
    aws.expect("set_instance_protection", InstanceIds=["instance-b", "instance-c"], ProtectedFromScaleIn=False)
    aws.expect("set_desired_capacity", DesiredCapacity=1, HonorCooldown=False)
    aws.expect("suspend_processes", ScalingProcesses=["Launch"])
    note = {"pool": "coursework", "record": "note", "capacity_asked": 1, "no_capacity_at": NOW - 120, "written_at": NOW}
    # The three instances the run read in service, and nothing waits: one is asked for.
    note |= {"requested": 0, "starting": 0, "in_service": 3, "draining": 0, "waiting": 0}
    aws.expect_writes(note, (EMPTY, memory))
    apply(aws.clients, TABLES, run, decision(1, ("instance-b", "instance-c"), suspend=True, memory=memory))


def test_no_change_no_call(aws: Aws) -> None:
    aws.queue_read(
        desired=1,
        instances=[instance("instance-a", "InService")],
        health={"instance-a": "healthy"},
        suspended=("AZRebalance", "Launch"),
    )
    run = read_one(aws)
    memory = Memory(launch_suspended=True, suspended_at=NOW - 60)
    # Nothing queued on Auto Scaling: any call there fails the test.
    note = {"pool": "coursework", "record": "note", "capacity_asked": 1, "written_at": NOW}
    note |= {"requested": 0, "starting": 0, "in_service": 1, "draining": 0, "waiting": 0}
    aws.expect_writes(note, (EMPTY, memory))
    apply(aws.clients, TABLES, run, decision(1, suspend=True, memory=memory))


def abandoned(aws: Aws, activities: list[dict[str, Any]], memory: Memory = EMPTY) -> int:
    aws.queue_read(records=[memory_to_item("coursework", memory)], activities=activities)
    return read_one(aws).inputs.pools[0].abandoned_launches


def test_what_counts_as_abandoned(aws: Aws) -> None:
    # Three abandons since the last launch that reached service; the abandon before that launch
    # ends nothing.
    history = [
        launch("Cancelled", NOW - 100, ABANDONED),
        launch("Cancelled", NOW - 200, ABANDONED),
        launch("Failed", NOW - 300, ABANDONED),
        launch("Successful", NOW - 400),
        launch("Cancelled", NOW - 500, ABANDONED),
    ]
    assert abandoned(aws, history) == 3
    no_capacity = [launch("Failed", NOW - 100 * n, NO_CAPACITY) for n in (1, 2, 3)]
    assert abandoned(aws, no_capacity) == 0
    # The admin cleared the mark at NOW - 50: the abandons the history still holds do not count.
    cleared = Memory(abandons_counted_from=NOW - 50)
    assert abandoned(aws, [launch("Cancelled", NOW - 100 * n, ABANDONED) for n in (1, 2, 3)], cleared) == 0
    assert abandoned(aws, [launch("Cancelled", NOW - 10, ABANDONED), *history], cleared) == 1


def test_retained_activities_of_a_warm_pool_end_the_row_only_on_reaching_service() -> None:
    # The group has no warm pool, but the history Auto Scaling retains still holds its activities:
    # a launch that prepared a stopped instance reached no service, a move out of the pool did.
    def activity(ended: int, description: str, status: str) -> dict[str, Any]:
        return launch(status, ended, ABANDONED if status == "Cancelled" else "") | {"Description": description}

    abandons = [launch("Cancelled", NOW - n, ABANDONED) for n in (300, 200, 100)]
    prepared = activity(NOW - 150, "Launching a new EC2 instance into warm pool: instance-x", "Successful")
    promoted = activity(NOW - 50, "Launching a new EC2 instance from warm pool: instance-x", "Successful")
    moved = activity(NOW - 50, "Moving instance-x out of warm pool", "Successful")
    abandoned_on_the_way = activity(NOW - 20, "Launching a new EC2 instance from warm pool: instance-x", "Cancelled")
    assert launch_history([*abandons, prepared], None).abandoned_in_a_row == 3
    assert launch_history([*abandons, promoted], None).abandoned_in_a_row == 0
    assert launch_history([*abandons, moved], None).abandoned_in_a_row == 0
    assert launch_history([*abandons, moved, abandoned_on_the_way], None).abandoned_in_a_row == 1


def test_protection_and_resume(aws: Aws) -> None:
    aws.queue_read(
        desired=2,
        instances=[instance("instance-a", "InService", protected=False), instance("instance-b", "InService")],
        health={"instance-a": "healthy", "instance-b": "healthy"},
        suspended=("AZRebalance", "Launch"),
    )
    run = read_one(aws)
    aws.expect("set_instance_protection", InstanceIds=["instance-a"], ProtectedFromScaleIn=True)
    aws.expect("resume_processes", ScalingProcesses=["Launch"])
    aws.expect_writes()
    apply(aws.clients, TABLES, run, decision(2, memory=Memory(abandons_counted_from=NOW)))


def test_the_memory_update_leaves_the_admins_clear_and_what_is_not_the_scalers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("dynamodb")
        create_tables(client)
        suspended = Memory(last_rebalance_at=NOW - 60, outside_sessions_since=NOW - 90, launch_suspended=True, suspended_at=NOW - 30)
        # The admin clears the mark after a run read it set, and before that run writes.
        cleared = {**memory_to_item("coursework", suspended), "launch_suspended": False, "cleared_by": "user/admin"}
        client.put_item(TableName=TABLES.pool_state, Item=serialize(cleared))
        after = Memory(last_rebalance_at=NOW, launch_suspended=True, suspended_at=NOW - 30)
        client.update_item(**memory_update(TABLES.pool_state, "coursework", suspended, after))
        key = {"pool": {"S": "coursework"}, "record": {"S": "memory"}}
        stored = memory_from_item(plain(client.get_item(TableName=TABLES.pool_state, Key=key)["Item"]))
        # Still cleared, what the scaler holds no more is gone, and the admin's own attribute stays.
        assert stored == Memory(last_rebalance_at=NOW, suspended_at=NOW - 30)
        assert client.get_item(TableName=TABLES.pool_state, Key=key)["Item"]["cleared_by"] == {"S": "user/admin"}

        # A run that raises the mark sets it, and every attribute of the memory is stored.
        full = Memory(
            launch_suspended=True,
            suspended_at=NOW,
            abandons_counted_from=NOW - 8,
            queuing_since=(("instance-b", NOW - 9),),
            rebalance_number=3,
            last_rebalance_at=NOW - 10,
            outside_sessions_since=NOW - 11,
            short_since=NOW - 12,
        )
        assert all(value not in (None, (), 0, False) for value in dataclasses.astuple(full))
        client.update_item(**memory_update(TABLES.pool_state, "coursework", stored, full))
        assert memory_from_item(plain(client.get_item(TableName=TABLES.pool_state, Key=key)["Item"])) == full

        # And what it holds no more is removed.
        client.update_item(**memory_update(TABLES.pool_state, "coursework", full, Memory(launch_suspended=True, suspended_at=NOW)))
        stored = memory_from_item(plain(client.get_item(TableName=TABLES.pool_state, Key=key)["Item"]))
        assert stored == Memory(launch_suspended=True, suspended_at=NOW)


def test_a_memory_item_of_an_earlier_version_is_read_and_the_next_update_removes_its_old_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # The item as an earlier version stored it: the attributes of load, of idleness and of the
    # gateway, beside the launch loop's mark and when it was set.
    old = {
        "pool": "coursework",
        "record": "memory",
        "launch_suspended": True,
        "suspended_at": NOW - 600,
        "last_request_at": NOW - 60,
        "last_in_flight_at": NOW - 61,
        "above_target_since": NOW - 62,
        "waiting_since": {"instance-a": NOW - 63},
        "last_scale_out_at": NOW - 64,
        "low_load_since": NOW - 65,
        "last_gateway_at": NOW - 66,
    }
    assert len(old) == 11 and set(FORMER_MEMORY_ATTRIBUTES) < set(old)
    key = {"pool": {"S": "coursework"}, "record": {"S": "memory"}}
    with mock_aws():
        client = boto3.client("dynamodb")
        create_tables(client)
        client.put_item(TableName=TABLES.pool_state, Item=serialize(old))
        before = memory_from_item(plain(client.get_item(TableName=TABLES.pool_state, Key=key)["Item"]))
        assert before == Memory(launch_suspended=True, suspended_at=NOW - 600)
        # The next run's update: the mark stands, and the pool has begun to wait for capacity.
        after = dataclasses.replace(before, short_since=NOW)
        client.update_item(**memory_update(TABLES.pool_state, "coursework", before, after))
        stored = plain(client.get_item(TableName=TABLES.pool_state, Key=key)["Item"])
    assert stored == {"pool": "coursework", "record": "memory", "launch_suspended": True, "suspended_at": NOW - 600, "short_since": NOW}
