"""One run of the scaler (`rhi_scaler.handler`), against stubbed AWS clients and a captured push."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.credentials import Credentials
from botocore.stub import ANY, Stubber

from rhi_common.records import GatewayHeartbeat
from rhi_scaler import handler as scaler
from rhi_scaler.aws_io import Clients, memory_to_item
from rhi_scaler.metrics import DECISIONS, GAUGES, DecisionCounts, RemoteWriter, decode
from rhi_scaler.model import Memory, Reason
from rhi_scaler.settings import read_settings

NOW = 1_800_000_000
GROUP = "mock-group"
TARGET_GROUP = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/0123456789abcdef"
WORKSPACE = "https://aps.example.com/workspaces/ws-example/api/v1/remote_write"
ENTRY = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "max_instances": 1,
    "warm_instances": 1,
    "wake": "on-demand",
    "target_concurrency": 4,
    "context_length": 32_768,
    "largest_output": 4_096,
}
ENVIRON = {
    "AWS_REGION": "us-east-1",
    "RHI_SCALER_SETTINGS": json.dumps(
        {
            "pools": {"mock": {"entry": ENTRY, "group": GROUP, "target_group": TARGET_GROUP}},
            "heartbeat_table": "heartbeats-table",
            "pool_state_table": "pool-state-table",
            "workspace_url": WORKSPACE,
        }
    ),
}
SETTINGS = read_settings(ENVIRON)
WARM = [{"InstanceId": "warm-a", "LifecycleState": "Warmed:Stopped"}]


def held_request(now: int) -> dict[str, Any]:
    """A gateway task holding one request for the mock pool (a wake: also a request received)."""
    return GatewayHeartbeat("task-1", "mock", 0, 1, NOW - 2, now - 1).to_item()


def serialize(item: dict[str, Any]) -> dict[str, Any]:
    return {name: TypeSerializer().serialize(value) for name, value in item.items()}


class Edge:
    """Stubbed clients, every call queued in order, and the pushes the writer sent."""

    def __init__(self) -> None:
        options = {"region_name": "us-east-1", "aws_access_key_id": "stub", "aws_secret_access_key": "stub"}
        self.clients = Clients(
            autoscaling=boto3.client("autoscaling", **options),
            elbv2=boto3.client("elbv2", **options),
            dynamodb=boto3.client("dynamodb", **options),
        )
        self.autoscaling = Stubber(self.clients.autoscaling)
        self.elbv2 = Stubber(self.clients.elbv2)
        self.dynamodb = Stubber(self.clients.dynamodb)
        self.pushes: list[tuple[str, dict[str, str], bytes]] = []
        self.writer = RemoteWriter(
            WORKSPACE, "us-east-1", Credentials("stub", "stub"), send=lambda url, headers, body: self.pushes.append((url, dict(headers), body))
        )

    def queue_read(self, *, desired: int, instances: Sequence[dict[str, Any]], records: Sequence[dict[str, Any]], heartbeats: Sequence[dict[str, Any]]) -> None:
        group = {
            "AutoScalingGroupName": GROUP,
            "MinSize": 0,
            "MaxSize": 1,
            "DesiredCapacity": desired,
            "DefaultCooldown": 300,
            "AvailabilityZones": ["us-east-1a"],
            "HealthCheckType": "ELB",
            "CreatedTime": datetime(2026, 1, 1, tzinfo=UTC),
            "Instances": [{**i, "AvailabilityZone": "us-east-1a", "HealthStatus": "Healthy", "ProtectedFromScaleIn": True} for i in instances],
            "SuspendedProcesses": [{"ProcessName": "AZRebalance", "SuspensionReason": "by the stack"}],
        }
        warm = [{**i, "AvailabilityZone": "us-east-1a", "HealthStatus": "Healthy", "ProtectedFromScaleIn": False} for i in WARM]
        self.autoscaling.add_response("describe_auto_scaling_groups", {"AutoScalingGroups": [group]}, {"AutoScalingGroupNames": [GROUP]})
        self.autoscaling.add_response("describe_warm_pool", {"Instances": warm}, {"AutoScalingGroupName": GROUP})
        self.autoscaling.add_response("describe_scaling_activities", {"Activities": []}, {"AutoScalingGroupName": GROUP})
        self.elbv2.add_response("describe_target_health", {"TargetHealthDescriptions": []}, {"TargetGroupArn": TARGET_GROUP})
        for table, items in (("pool-state-table", records), ("heartbeats-table", heartbeats)):
            query = {
                "TableName": table,
                "KeyConditionExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": {":pool": {"S": "mock"}},
                "ConsistentRead": True,
            }
            self.dynamodb.add_response("query", {"Items": [serialize(item) for item in items]}, query)

    def expect_writes(self, note: dict[str, Any], memory: dict[str, Any] | None) -> None:
        for item in (note, memory):
            self.dynamodb.add_response("put_item", {}, {"TableName": "pool-state-table", "Item": ANY if item is None else serialize(item)})

    def queue_first_run(self) -> None:
        """A pool at zero, a request held: one instance asked for, and the memory stored."""
        self.queue_read(desired=0, instances=[], records=[], heartbeats=[held_request(NOW)])
        self.autoscaling.add_response(
            "set_desired_capacity", {}, {"AutoScalingGroupName": GROUP, "DesiredCapacity": 1, "HonorCooldown": False}
        )
        stored = Memory(last_request_at=NOW - 2, last_in_flight_at=NOW, last_gateway_at=NOW - 1)
        self.expect_writes({"pool": "mock", "record": "note", "capacity_asked": 1, "written_at": NOW}, memory_to_item("mock", stored))


@pytest.fixture
def edge() -> Iterator[Edge]:
    edge = Edge()
    with edge.autoscaling, edge.elbv2, edge.dynamodb:
        yield edge
        edge.autoscaling.assert_no_pending_responses()
        edge.elbv2.assert_no_pending_responses()
        edge.dynamodb.assert_no_pending_responses()


def series(body: bytes) -> dict[tuple[str, tuple[tuple[str, str], ...]], list[float]]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
    for timeseries in decode(body).timeseries:
        labels = {label.name: label.value for label in timeseries.labels}
        name = labels.pop("__name__")
        out[(name, tuple(sorted(labels.items())))] = [sample.value for sample in timeseries.samples]
    return out


def test_a_held_request_wakes_the_pool_stores_the_memory_and_emits_its_metrics(edge: Edge) -> None:
    edge.queue_first_run()
    decision = scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, DecisionCounts())
    assert [(pool.desired, pool.reason) for pool in decision.pools] == [(1, Reason.WAKE)]

    ((url, headers, body),) = edge.pushes
    assert url == WORKSPACE
    assert headers["Content-Encoding"] == "snappy"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ") and "/us-east-1/aps/aws4_request" in headers["Authorization"]
    sent = series(body)
    pool = (("pool", "mock"),)
    assert {name: sent[(name, pool)][-1] for name in GAUGES} == {
        "rhi_scaler_desired_capacity": 1.0,
        "rhi_scaler_instances_in_service": 0.0,
        "rhi_scaler_warm_instances": 1.0,
    }
    assert sent[(DECISIONS, (("pool", "mock"), ("reason", "wake")))][-1] == 1.0
    # The pool and the reason are the only labels: nothing names a student.
    assert {name for _, labels in sent for name, _ in labels} == {"pool", "reason"}


def test_a_second_run_is_harmless(edge: Edge) -> None:
    edge.queue_first_run()
    counts = DecisionCounts()
    first = scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, counts)
    # The same request still held, read a minute later from the world the first run left: the
    # group at the capacity it set, its instance starting, and the memory it stored.
    later = NOW + 60
    edge.queue_read(
        desired=1,
        instances=[{"InstanceId": "instance-a", "LifecycleState": "Pending:Wait"}],
        records=[memory_to_item("mock", first.pools[0].memory)],
        heartbeats=[held_request(later)],
    )
    # No Auto Scaling call is queued: one would fail the test. Only the note and the memory.
    edge.expect_writes({"pool": "mock", "record": "note", "capacity_asked": 1, "written_at": later}, None)
    second = scaler.run_cycle(SETTINGS, edge.clients, edge.writer, later, counts)
    assert [pool.desired for pool in second.pools] == [pool.desired for pool in first.pools] == [1]


def test_the_caller_changes_nothing_and_nothing_from_the_request_is_logged(
    edge: Edge, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scaler, "connect", lambda settings: (edge.clients, edge.writer))
    monkeypatch.setattr(scaler.time, "time", lambda: float(NOW))
    marker = "student-marker-7f3a"
    events = [
        {"source": "gateway", "pool": "mock", "student": marker, "headers": {"authorization": marker}},
        {"source": "aws.events", "detail-type": "Scheduled Event"},
        {"source": "admin", "caller": marker},
    ]
    answers = []
    with caplog.at_level(logging.INFO, logger="rhi_scaler"):
        for event in events:
            edge.queue_first_run()
            answers.append(scaler.handler(event, None, environ=ENVIRON))
    assert answers == [{"pools": [{"pool": "mock", "desired": 1, "reason": "wake"}]}] * 3

    assert marker not in caplog.text
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "rhi_scaler"]
    assert [(record["pool"], record["reason"]) for record in records if record["event"] == "decision"] == [("mock", "wake")] * 3
