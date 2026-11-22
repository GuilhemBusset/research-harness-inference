"""The scaler's rebalance rule (`rhi_scaler.decide`), and its number in the pool's note (`rhi_scaler.aws_io`)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode
from rhi_scaler.aws_io import (
    Clients,
    PoolSettings,
    TableNames,
    apply,
    memory_from_item,
    memory_to_item,
    memory_update,
    read,
)
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

T = 1_800_000_000
ENTRY = {
    "engine": "mock",
    "instance_types": ["g6e.xlarge"],
    "prices": {"g6e.xlarge": 1.86},
    "max_instances": 3,
    "warm_instances": 1,
    "wake": "on-demand",
    "target_concurrency": 8,
    "context_length": 32_768,
    "largest_output": 4_096,
}
POOL = parse_pools({"coursework": ENTRY})[0]
TWO = Supply(desired=2, in_service=("instance-a", "instance-b"), running=("instance-a", "instance-b"))
# Instance a at its target with two requests waiting, beside an instance b that runs 3 or 4.
WAITS = ("instance-a", 8, 2)
BELOW_HALF, AT_HALF = ("instance-b", 3, 0), ("instance-b", 4, 0)

Report = tuple[str, int, int]


def instance_heartbeat(report: Report, now: int) -> InstanceHeartbeat:
    name, running, waiting = report
    return InstanceHeartbeat(name, POOL.name, running, waiting, "sha256:" + "ab" * 32, "rev-1", "serving", now)


def run(now: int, supply: Supply, *reports: Report, memory: Memory, in_flight: int = 13, pool: Pool = POOL) -> PoolDecision:
    """One run at `now`: one gateway task and the instances' reports (name, running, waiting), written at `now`."""
    inputs = PoolInputs(
        config=pool,
        mode=Mode.ENABLED,
        supply=supply,
        gateway_heartbeats=(GatewayHeartbeat("task-0", pool.name, in_flight, 0, now, now),),
        instance_heartbeats=tuple(instance_heartbeat(report, now) for report in reports),
        memory=memory,
    )
    (decision,) = decide(Inputs(now=now, pools=(inputs,))).pools
    return decision


def numbers(steps: dict[int, tuple[Report, ...]], pool: Pool = POOL) -> dict[int, tuple[int, Reason, int]]:
    """The number, the reason and the desired capacity of a run at each second after T, each run
    given the memory of the one before."""
    out, memory = {}, Memory()
    for seconds, reports in steps.items():
        decision = run(T + seconds, TWO, *reports, memory=memory, pool=pool)
        assert decision.lift_protection == () and decision.memory.rebalance_number == decision.rebalance_number
        out[seconds], memory = (decision.rebalance_number, decision.reason, decision.desired), decision.memory
    return out


def test_one_instance_queues_while_another_has_room() -> None:
    assert POOL.target_concurrency == 8 and POOL.rebalance_interval_seconds == 60
    lopsided = (WAITS, BELOW_HALF)
    lasting = numbers(dict.fromkeys((0, 30, 59, 60, 90, 119, 120), lopsided))
    assert lasting == {
        **dict.fromkeys((0, 30, 59), (0, Reason.UNCHANGED, 2)),
        60: (1, Reason.REBALANCE, 2),
        **dict.fromkeys((90, 119), (1, Reason.UNCHANGED, 2)),
        120: (2, Reason.REBALANCE, 2),
    }
    # A neighbour running 4, half of the target, raises nothing; and the capacity is the same.
    level = numbers(dict.fromkeys(lasting, (WAITS, AT_HALF)))
    assert level == dict.fromkeys(lasting, (0, Reason.UNCHANGED, 2))
    assert [desired for _, _, desired in lasting.values()] == [desired for _, _, desired in level.values()]

    # Only while the imbalance lasts: a minute after the first number, the room or the waiting is gone.
    for gone in ((WAITS, AT_HALF), (("instance-a", 8, 0), BELOW_HALF)):
        assert numbers({0: lopsided, 30: lopsided, 60: lopsided, 120: gone})[120] == (1, Reason.UNCHANGED, 2)
    # A run that sees no waiting starts the 60 seconds again.
    paused = numbers({0: lopsided, 30: (("instance-a", 8, 0), BELOW_HALF), 60: lopsided, 119: lopsided, 120: lopsided})
    assert [paused[seconds][0] for seconds in (60, 119, 120)] == [0, 0, 1]
    # The other instance is another one, and what it reports waiting does not matter. The pool is
    # at its largest here, so that no other rule gives these runs a reason.
    largest = parse_pools({"coursework": {**ENTRY, "max_instances": 2}})[0]
    alone, both_wait = (("instance-a", 3, 1),), (WAITS, ("instance-b", 3, 1))
    assert numbers({0: alone, 60: alone}, largest)[60] == (0, Reason.UNCHANGED, 2)
    assert numbers({0: both_wait, 60: both_wait}, largest)[60] == (1, Reason.REBALANCE, 2)

    # The least time between two numbers is the pool's setting.
    slow = parse_pools({"coursework": {**ENTRY, "rebalance_interval_seconds": 90}})[0]
    spaced = numbers(dict.fromkeys((0, 60, 120, 149, 150), lopsided), slow)
    assert [number for number, _, _ in spaced.values()] == [0, 1, 1, 1, 2]


def test_the_other_rules_decide_the_capacity() -> None:
    # 17 in flight on two instances is above the target, and instance a waits beside one below
    # half: after 60 seconds the pool grows as it would without the rule, and the number goes up.
    crowded, roomy = ("instance-a", 8, 6), BELOW_HALF
    first = run(T, TWO, crowded, roomy, memory=Memory(), in_flight=17)
    both = run(T + 60, TWO, crowded, roomy, memory=first.memory, in_flight=17)
    assert (both.desired, both.reason, both.rebalance_number) == (3, Reason.SCALE_OUT, 1)
    first = run(T, TWO, crowded, AT_HALF, memory=Memory(), in_flight=17)
    without = run(T + 60, TWO, crowded, AT_HALF, memory=first.memory, in_flight=17)
    assert (without.desired, without.reason, without.rebalance_number) == (3, Reason.SCALE_OUT, 0)


def test_a_pool_that_has_just_grown() -> None:
    one = Supply(desired=1, in_service=("instance-a",), running=("instance-a",))
    first = run(T, one, WAITS, memory=Memory(), in_flight=10)
    grown = run(T + 60, one, WAITS, memory=first.memory, in_flight=10)
    assert (grown.desired, grown.reason, grown.rebalance_number) == (2, Reason.SCALE_OUT, 0)
    # The new instance starts: nothing to move students to yet.
    starting = Supply(desired=2, in_service=("instance-a",), starting=("instance-b",), running=("instance-a", "instance-b"))
    pending = run(T + 120, starting, WAITS, memory=grown.memory, in_flight=10)
    assert (pending.desired, pending.reason, pending.rebalance_number) == (2, Reason.UNCHANGED, 0)
    # The first run that sees it in service with nothing running: instance a has waited all along.
    joined = run(T + 180, TWO, WAITS, ("instance-b", 0, 0), memory=pending.memory, in_flight=10)
    assert (joined.desired, joined.lift_protection, joined.reason, joined.rebalance_number) == (2, (), Reason.REBALANCE, 1)


def serialize(item: dict[str, Any]) -> dict[str, Any]:
    return {name: TypeSerializer().serialize(value) for name, value in item.items()}


def test_the_number_reaches_the_note() -> None:
    tables = TableNames(heartbeats="heartbeats-table", pool_state="pool-state-table")
    settings = PoolSettings(config=POOL, group="group", target_group="targets", configured_mode=Mode.ENABLED)
    options = {"region_name": "us-east-1", "aws_access_key_id": "stub", "aws_secret_access_key": "stub"}
    clients = Clients(*(boto3.client(name, **options) for name in ("autoscaling", "elbv2", "dynamodb", "cloudwatch")))
    autoscaling, elbv2, dynamodb = Stubber(clients.autoscaling), Stubber(clients.elbv2), Stubber(clients.dynamodb)
    name = {"AutoScalingGroupName": "group"}
    placed = {"AvailabilityZone": "us-east-1a", "HealthStatus": "Healthy", "ProtectedFromScaleIn": True}
    group = {
        **name,
        "MinSize": 0,
        "MaxSize": 3,
        "DesiredCapacity": 2,
        "DefaultCooldown": 300,
        "AvailabilityZones": ["us-east-1a"],
        "HealthCheckType": "ELB",
        "CreatedTime": datetime(2026, 1, 1, tzinfo=UTC),
        "Instances": [{"InstanceId": instance, "LifecycleState": "InService", **placed} for instance in TWO.in_service],
    }
    health = [{"Target": {"Id": instance}, "TargetHealth": {"State": "healthy"}} for instance in TWO.in_service]
    query = {"KeyConditionExpression": ANY, "ExpressionAttributeNames": ANY, "ExpressionAttributeValues": ANY, "ConsistentRead": True}
    # The same lopsided pool run after run, but at 120 seconds, where instance b runs 4: what
    # instance b reports, and the number the note must hold (none at all before the first).
    steps = {0: (BELOW_HALF, None), 60: (BELOW_HALF, 1), 90: (BELOW_HALF, 1), 120: (AT_HALF, 1), 150: (BELOW_HALF, 2)}
    stored: list[dict[str, Any]] = []
    reasons = []
    with autoscaling, elbv2, dynamodb:
        for seconds, (neighbour, number) in steps.items():
            now = T + seconds
            heartbeats = [
                GatewayHeartbeat("task-0", POOL.name, 13, 0, now, now).to_item(),
                *(instance_heartbeat(report, now).to_item() for report in (WAITS, neighbour)),
            ]
            autoscaling.add_response("describe_auto_scaling_groups", {"AutoScalingGroups": [group]}, {"AutoScalingGroupNames": ["group"]})
            autoscaling.add_response("describe_warm_pool", {"Instances": []}, name)
            autoscaling.add_response("describe_scaling_activities", {"Activities": []}, name)
            elbv2.add_response("describe_target_health", {"TargetHealthDescriptions": health}, {"TargetGroupArn": "targets"})
            # The pool-state table holds what the run before stored.
            for table, items in ((tables.pool_state, stored), (tables.heartbeats, heartbeats)):
                dynamodb.add_response("query", {"Items": [serialize(item) for item in items]}, {"TableName": table, **query})
            # No session switch.
            dynamodb.add_response("get_item", {}, {"TableName": tables.pool_state, "Key": ANY, "ConsistentRead": True})
            read_run = read(clients, tables, [settings], now)
            decision = decide(read_run.inputs)
            # The two items the table must receive, exactly. No Auto Scaling call is queued: one would fail.
            note = {"pool": "coursework", "record": "note", "capacity_asked": 2, "written_at": now}
            note.update({} if number is None else {"rebalance_number": number})
            before = read_run.inputs.pools[0].memory
            stored = [note, memory_to_item("coursework", decision.pools[0].memory)]
            dynamodb.add_response("put_item", {}, {"TableName": tables.pool_state, "Item": serialize(note)})
            update = memory_update(tables.pool_state, "coursework", before, decision.pools[0].memory)
            dynamodb.add_response("update_item", {}, update)
            apply(clients, tables, read_run, decision)
            assert memory_from_item(stored[1]) == decision.pools[0].memory
            reasons.append(decision.pools[0].reason)
        dynamodb.assert_no_pending_responses()
    rebalance, unchanged = Reason.REBALANCE, Reason.UNCHANGED
    assert reasons == [unchanged, rebalance, unchanged, unchanged, rebalance]
