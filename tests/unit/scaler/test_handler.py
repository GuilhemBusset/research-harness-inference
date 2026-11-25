"""One run of the scaler (`rhi_scaler.handler`), against stubbed AWS clients and a captured push."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from moto import mock_aws

from rhi_common.records import Session
from rhi_scaler import handler as scaler
from rhi_scaler.aws_io import Clients, memory_from_item, memory_to_item
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
    "prices": {"t4g.small": 0.0168},
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


def session_record() -> dict[str, Any]:
    """The mock pool's session record: one instance, from an hour before `NOW` to an hour after."""
    return Session("mock", 1, NOW - 3600, NOW + 3600, set_by="instructor", set_at=NOW - 7200).to_item()


def note(asked: int, now: int = NOW, **supply: int) -> dict[str, Any]:
    """The pool's note of a run at `now` that asks for `asked`: zeros but for the `supply` given."""
    counts = {"requested": 0, "starting": 0, "in_service": 0, "draining": 0, "waiting": 0}
    return {"pool": "mock", "record": "note", "capacity_asked": asked, "written_at": now, **counts, **supply}


def serialize(item: dict[str, Any]) -> dict[str, Any]:
    return {name: TypeSerializer().serialize(value) for name, value in item.items()}


def stored_memory(update: dict[str, Any]) -> Memory:
    """The memory that an UpdateItem call leaves for a pool that had no memory record: the call
    made by moto's DynamoDB, so the expectation owes nothing to how the scaler built it."""
    keys = [("pool", "HASH"), ("record", "RANGE")]
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="stub", aws_secret_access_key="stub")
        client.create_table(
            TableName=update["TableName"],
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in keys],
            KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in keys],
        )
        client.update_item(**update)
        item = client.get_item(TableName=update["TableName"], Key=update["Key"])["Item"]
    return memory_from_item({name: TypeDeserializer().deserialize(value) for name, value in item.items()})


class Edge:
    """Stubbed clients, every call queued in order, and the pushes the writer sent."""

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
        self.cloudwatch = Stubber(self.clients.cloudwatch)
        # Each UpdateItem call made, as the scaler made it.
        self.updates: list[dict[str, Any]] = []
        self.clients.dynamodb.meta.events.register(
            "provide-client-params.dynamodb.UpdateItem", lambda params, **_: self.updates.append(dict(params))
        )
        self.pushes: list[tuple[str, dict[str, str], bytes]] = []
        self.writer = RemoteWriter(
            WORKSPACE, "us-east-1", Credentials("stub", "stub"), send=lambda url, headers, body: self.pushes.append((url, dict(headers), body))
        )

    def queue_read(
        self,
        *,
        desired: int,
        instances: Sequence[dict[str, Any]],
        records: Sequence[dict[str, Any]],
        heartbeats: Sequence[dict[str, Any]],
    ) -> None:
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
        self.autoscaling.add_response("describe_auto_scaling_groups", {"AutoScalingGroups": [group]}, {"AutoScalingGroupNames": [GROUP]})
        self.autoscaling.add_response("describe_scaling_activities", {"Activities": []}, {"AutoScalingGroupName": GROUP})
        self.elbv2.add_response("describe_target_health", {"TargetHealthDescriptions": []}, {"TargetGroupArn": TARGET_GROUP})
        # The pool's memory and session by their keys, then its instances' heartbeats: the Stubber
        # refuses any other read of the tables.
        keys = [{"pool": {"S": "mock"}, "record": {"S": record}} for record in ("memory", "session")]
        self.dynamodb.add_response(
            "batch_get_item",
            {"Responses": {"pool-state-table": [serialize(item) for item in records]}},
            {"RequestItems": {"pool-state-table": {"Keys": keys, "ConsistentRead": True}}},
        )
        query = {
            "TableName": "heartbeats-table",
            "KeyConditionExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": {":pool": {"S": "mock"}, ":instance": {"S": "instance#"}},
            "ConsistentRead": True,
        }
        self.dynamodb.add_response("query", {"Items": [serialize(item) for item in heartbeats]}, query)

    def expect_writes(self, note: dict[str, Any]) -> None:
        """The note put whole, then the memory updated: what it stores is in `updates`. Then the
        run's data points published to CloudWatch (`rhi_scaler.cloudwatch`)."""
        self.dynamodb.add_response("put_item", {}, {"TableName": "pool-state-table", "Item": serialize(note)})
        self.dynamodb.add_response("update_item", {})
        self.cloudwatch.add_response("put_metric_data", {}, {"Namespace": "rhi/scaler", "MetricData": ANY})

    def queue_first_run(self) -> None:
        """A pool at zero whose session asks for one instance: it is asked for, and the memory
        stored."""
        self.queue_read(desired=0, instances=[], records=[session_record()], heartbeats=[])
        self.autoscaling.add_response(
            "set_desired_capacity", {}, {"AutoScalingGroupName": GROUP, "DesiredCapacity": 1, "HonorCooldown": False}
        )
        # The instance asked for has not come: it waits, from this run on.
        self.expect_writes(note(1, requested=1, waiting=1, waiting_since=NOW))


@pytest.fixture
def edge() -> Iterator[Edge]:
    edge = Edge()
    with edge.autoscaling, edge.elbv2, edge.dynamodb, edge.cloudwatch:
        yield edge
        edge.autoscaling.assert_no_pending_responses()
        edge.elbv2.assert_no_pending_responses()
        edge.dynamodb.assert_no_pending_responses()
        edge.cloudwatch.assert_no_pending_responses()


def series(body: bytes) -> dict[tuple[str, tuple[tuple[str, str], ...]], list[float]]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
    for timeseries in decode(body).timeseries:
        labels = {label.name: label.value for label in timeseries.labels}
        name = labels.pop("__name__")
        out[(name, tuple(sorted(labels.items())))] = [sample.value for sample in timeseries.samples]
    return out


def test_a_session_raises_the_pool_stores_the_memory_and_emits_its_metrics(edge: Edge) -> None:
    edge.queue_first_run()
    decision = scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, DecisionCounts())
    assert [(pool.desired, pool.reason) for pool in decision.pools] == [(1, Reason.PROVISION)]
    (update,) = edge.updates
    assert update["Key"] == {"pool": {"S": "mock"}, "record": {"S": "memory"}}
    # The memory stored: the run from which the pool waits for the instance it asked for.
    assert stored_memory(update) == Memory(short_since=NOW)

    ((url, headers, body),) = edge.pushes
    assert url == WORKSPACE
    assert headers["Content-Encoding"] == "snappy"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ") and "/us-east-1/aps/aws4_request" in headers["Authorization"]
    sent = series(body)
    pool = (("pool", "mock"),)
    assert {name: sent[(name, pool)][-1] for name in GAUGES} == {
        "rhi_scaler_desired_capacity": 1.0,
        "rhi_scaler_instances_in_service": 0.0,
    }
    assert sent[(DECISIONS, (("pool", "mock"), ("reason", "provision")))][-1] == 1.0
    # Every series is a pool's: the run sends none for the whole system.
    assert all(dict(labels).get("pool") == "mock" for _, labels in sent)
    # What the note holds goes out too: one instance asked of AWS that has not come.
    assert sent[("rhi_scaler_instances_waiting", pool)] == [1.0]
    # The pool, the reason and the cause are the only labels: nothing names a student.
    assert {name for _, labels in sent for name, _ in labels} == {"pool", "reason", "cause"}


def test_a_run_reads_the_pool_s_session_under_the_longest_session_of_its_settings(edge: Edge) -> None:
    # Nobody asks for the pool but its session, written two hours ago to end in an hour.
    session = Session("mock", 1, NOW - 7200, NOW + 3600, set_by="instructor", set_at=NOW - 7200)
    edge.queue_read(desired=0, instances=[], records=[session.to_item()], heartbeats=[])
    edge.autoscaling.add_response(
        "set_desired_capacity", {}, {"AutoScalingGroupName": GROUP, "DesiredCapacity": 1, "HonorCooldown": False}
    )
    edge.expect_writes(note(1, requested=1, waiting=1, waiting_since=NOW))
    decision = scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, DecisionCounts())
    assert [(pool.desired, pool.reason, pool.requested) for pool in decision.pools] == [(1, Reason.PROVISION, 1)]

    # Settings whose longest session is one hour: the same record ended an hour ago, and no call
    # to Auto Scaling is queued.
    shorter = read_settings({**ENVIRON, "RHI_SCALER_SETTINGS": json.dumps({**json.loads(ENVIRON["RHI_SCALER_SETTINGS"]), "session_max_hours": 1})})
    edge.queue_read(desired=0, instances=[], records=[session.to_item()], heartbeats=[])
    edge.expect_writes(note(0))
    decision = scaler.run_cycle(shorter, edge.clients, edge.writer, NOW, DecisionCounts())
    assert [(pool.desired, pool.requested) for pool in decision.pools] == [(0, 0)]


def test_a_run_that_cannot_read_the_pools_fails_on_that_error_and_sends_nothing(edge: Edge) -> None:
    # Auto Scaling refuses the first read. No read of the table is queued: the run makes none.
    edge.autoscaling.add_client_error("describe_auto_scaling_groups", "Throttling")
    with pytest.raises(ClientError, match="Throttling"):
        scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, DecisionCounts())
    assert edge.pushes == []


def test_a_run_whose_pool_write_fails_fails_on_that_error_and_sends_nothing(edge: Edge) -> None:
    # The capacity is set, then DynamoDB refuses the pool's note.
    edge.queue_read(desired=0, instances=[], records=[session_record()], heartbeats=[])
    edge.autoscaling.add_response(
        "set_desired_capacity", {}, {"AutoScalingGroupName": GROUP, "DesiredCapacity": 1, "HonorCooldown": False}
    )
    edge.dynamodb.add_client_error("put_item", "ProvisionedThroughputExceededException")
    with pytest.raises(ClientError, match="ProvisionedThroughputExceededException"):
        scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, DecisionCounts())
    assert edge.pushes == []


def test_a_second_run_is_harmless(edge: Edge) -> None:
    edge.queue_first_run()
    counts = DecisionCounts()
    first = scaler.run_cycle(SETTINGS, edge.clients, edge.writer, NOW, counts)
    # The same session, read a minute later from the world the first run left: the group at the
    # capacity it set, its instance starting, and the memory it stored.
    later = NOW + 60
    edge.queue_read(
        desired=1,
        instances=[{"InstanceId": "instance-a", "LifecycleState": "Pending:Wait"}],
        records=[memory_to_item("mock", first.pools[0].memory), session_record()],
        heartbeats=[],
    )
    # No Auto Scaling call is queued: one would fail the test. Only the note and the memory.
    edge.expect_writes(note(1, later, requested=1, starting=1))
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
    assert answers == [{"pools": [{"pool": "mock", "desired": 1, "reason": "provision"}]}] * 3

    assert marker not in caplog.text
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "rhi_scaler"]
    decisions = [record for record in records if record["event"] == "decision"]
    assert [(record["pool"], record["reason"]) for record in decisions] == [("mock", "provision")] * 3
    # The supply a record gives is the group's own: the instances in service and those starting.
    counts = {"desired_before": 0, "desired": 1, "in_service": 0, "starting": 0, "launches_suspended": False}
    assert [{key: record[key] for key in record if key not in ("event", "pool", "reason")} for record in decisions] == [counts] * 3
