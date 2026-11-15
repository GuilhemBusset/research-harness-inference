"""Independent settings, cycle and remote-write witnesses for issue 83."""

from __future__ import annotations

import json
import logging
import struct
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

import cramjam
import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.credentials import Credentials

from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat, Mode
from rhi_scaler import handler, metrics
from rhi_scaler.aws_io import Clients, memory_from_item
from rhi_scaler.settings import SettingsError, read_settings

NOW = 1_800_000_000
URL = "https://aps.example.com/workspaces/ws-example/api/v1/remote_write"
ENTRY = {
    "engine": "mock", "instance_types": ["t4g.small"], "max_instances": 1,
    "warm_instances": 1, "wake": "on-demand", "target_concurrency": 4,
    "context_length": 32768, "largest_output": 4096,
}


def configuration() -> dict[str, Any]:
    return {
        "pools": {
            name: {
                "entry": {**ENTRY, "wake": wake}, "group": name,
                "target_group": f"arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/{name}/0123456789abcdef",
            }
            for name, wake in (("mock", "on-demand"), ("classpool", "scheduled-only"))
        },
        "heartbeat_table": "heartbeats", "pool_state_table": "state", "workspace_url": URL,
    }


def environment(data: dict[str, Any] | None = None) -> dict[str, str]:
    return {"RHI_SCALER_SETTINGS": json.dumps(configuration() if data is None else data), "AWS_REGION": "us-east-1"}


def test_settings_preserve_configuration_and_deployment_fields() -> None:
    data = configuration()
    data["future_option"] = None
    settings = read_settings(environment(data))
    assert (settings.tables.heartbeats, settings.tables.pool_state, settings.workspace_url) == ("heartbeats", "state", URL)
    assert settings.region == "us-east-1"
    for pool in settings.pools:
        entry = data["pools"][pool.config.name]
        assert pool.config == parse_pools({pool.config.name: entry["entry"]})[0]
        assert (pool.group, pool.target_group) == (entry["group"], entry["target_group"])
    assert [pool.configured_mode for pool in settings.pools] == [Mode.ENABLED, Mode.SCHEDULED_ONLY]


@pytest.mark.parametrize("field", ["heartbeat_table", "pool_state_table", "workspace_url"])
def test_missing_settings_are_refused_by_field(field: str) -> None:
    data = configuration()
    del data[field]
    with pytest.raises(SettingsError, match=field):
        read_settings(environment(data))


def test_pool_entries_are_reparsed() -> None:
    data = configuration()
    data["pools"]["mock"]["entry"]["target_concurrency"] = 0
    with pytest.raises(SettingsError, match="target_concurrency"):
        read_settings(environment(data))


class Pages:
    """Only the read pages used by the scaler's real edge."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def paginate(self, **parameters: Any) -> list[dict[str, Any]]:
        del parameters
        return [self.values]


class Edge:
    """Stateful stub of the edge's external operations; no decision or apply code is replaced."""

    def __init__(self) -> None:
        self.desired = {"mock": 0, "classpool": 0}
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.clients = Clients(autoscaling=self, elbv2=self, dynamodb=self)

    def describe_auto_scaling_groups(self, *, AutoScalingGroupNames: list[str]) -> dict[str, Any]:
        name = AutoScalingGroupNames[0]
        return {"AutoScalingGroups": [{"DesiredCapacity": self.desired[name], "Instances": []}]}

    def describe_target_health(self, **parameters: Any) -> dict[str, Any]:
        del parameters
        return {"TargetHealthDescriptions": []}

    def get_paginator(self, operation: str) -> Any:
        if operation == "describe_warm_pool":
            return Pages({"Instances": [{"InstanceId": "warm-example", "LifecycleState": "Warmed:Stopped"}]})
        if operation == "describe_scaling_activities":
            return Pages({"Activities": []})
        assert operation == "query"
        return self

    def paginate(self, **parameters: Any) -> list[dict[str, Any]]:
        name = parameters["ExpressionAttributeValues"][":pool"]["S"]
        if parameters["TableName"] == "heartbeats":
            items = [GatewayHeartbeat("gateway-example", name, 0, 1, NOW - 1, NOW).to_item()]
        else:
            items = [item for (pool, _), item in self.records.items() if pool == name]
        return [{"Items": [{key: TypeSerializer().serialize(value) for key, value in item.items()} for item in items]}]

    def set_desired_capacity(self, **parameters: Any) -> None:
        self.calls.append(parameters)
        self.desired[parameters["AutoScalingGroupName"]] = parameters["DesiredCapacity"]

    def put_item(self, *, TableName: str, Item: dict[str, Any]) -> None:
        assert TableName == "state"
        plain = {key: TypeDeserializer().deserialize(value) for key, value in Item.items()}
        self.records[(plain["pool"], plain["record"])] = plain
        self.writes.append(plain)


def varint(data: bytes, offset: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
        shift += 7


def fields(data: bytes) -> list[tuple[int, Any]]:
    """Read the wire fields independently of the deliverable's dynamic protobuf descriptors."""
    out: list[tuple[int, Any]] = []
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        kind = tag & 7
        if kind == 2:
            size, offset = varint(data, offset)
            value = data[offset:offset + size]
            offset += size
        elif kind == 1:
            value = struct.unpack("<d", data[offset:offset + 8])[0]
            offset += 8
        else:
            assert kind == 0
            value, offset = varint(data, offset)
        out.append((tag >> 3, value))
    return out


def samples(body: bytes) -> dict[tuple[tuple[str, str], ...], list[tuple[float, int]]]:
    out = {}
    for number, series in fields(bytes(cramjam.snappy.decompress_raw(body))):
        assert number == 1
        labels, points = [], []
        for field, value in fields(series):
            parts = dict(fields(value))
            if field == 1:
                labels.append((parts[1].decode(), parts[2].decode()))
            elif field == 2:
                points.append((parts.get(1, 0.0), parts.get(2, 0)))
        assert labels == sorted(labels)
        out[tuple(labels)] = points
    return out


def install(monkeypatch: pytest.MonkeyPatch) -> tuple[Edge, list[bytes]]:
    edge, bodies = Edge(), []

    def receive(url: str, headers: Mapping[str, str], body: bytes) -> None:
        assert url == URL
        assert headers["Content-Type"] == "application/x-protobuf"
        assert headers["Content-Encoding"] == "snappy"
        assert headers["X-Prometheus-Remote-Write-Version"] == "0.1.0"
        assert "/us-east-1/aps/aws4_request" in headers["Authorization"]
        assert headers["X-Amz-Security-Token"] == "stub-session"
        bodies.append(body)

    writer = metrics.RemoteWriter(URL, "us-east-1", Credentials("stub", "stub", "stub-session"), send=receive)
    monkeypatch.setattr(handler, "connect", lambda settings: (edge.clients, writer))
    monkeypatch.setattr(handler, "COUNTS", metrics.DecisionCounts())
    for name, value in environment().items():
        monkeypatch.setenv(name, value)
    return edge, bodies


@pytest.mark.parametrize("source", ["gateway", "schedule", "admin"])
def test_cycle_writes_memory_metrics_and_private_decision_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, source: str,
) -> None:
    edge, bodies = install(monkeypatch)
    monkeypatch.setattr(handler.time, "time", lambda: NOW + 0.25)
    marker = "request-marker-private"
    with caplog.at_level(logging.INFO, logger="rhi_scaler"):
        result = handler.handler({"source": source, "request": marker}, {"caller": marker})
    assert [(pool["pool"], pool["desired"]) for pool in result["pools"]] == [("mock", 1), ("classpool", 0)]
    assert edge.calls == [{"AutoScalingGroupName": "mock", "DesiredCapacity": 1, "HonorCooldown": False}]
    assert memory_from_item(edge.records[("mock", "memory")]).last_request_at == NOW - 1
    assert {item["record"] for item in edge.writes} == {"memory", "note"}
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "rhi_scaler"]
    assert [(record["pool"], record["reason"]) for record in records] == [("mock", "wake"), ("classpool", "not-woken")]
    assert marker not in caplog.text
    (body,) = bodies
    sent = samples(body)
    for pool, desired in (("mock", 1.0), ("classpool", 0.0)):
        gauges = {"rhi_scaler_desired_capacity": desired, "rhi_scaler_instances_in_service": 0.0, "rhi_scaler_warm_instances": 1.0}
        for metric, expected in gauges.items():
            assert sent[(("__name__", metric), ("pool", pool))][-1][0] == expected
        counter = [points for labels, points in sent.items() if dict(labels).get("__name__") == "rhi_scaler_decisions_total" and dict(labels)["pool"] == pool]
        assert len(counter) == 1 and counter[0][-1][0] == 1.0
        reason = "wake" if pool == "mock" else "not-woken"
        assert (("__name__", "rhi_scaler_decisions_total"), ("pool", pool), ("reason", reason)) in sent
    assert {key for labels in sent for key, _ in labels} == {"__name__", "pool", "reason"}


def test_repeated_demand_does_not_repeat_autoscaling_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    edge, bodies = install(monkeypatch)
    monkeypatch.setattr(handler.time, "time", lambda: NOW + 0.25)
    first = handler.handler({}, None)
    calls = list(edge.calls)
    monkeypatch.setattr(handler.time, "time", lambda: NOW + 1.25)
    second = handler.handler({}, None)
    assert [pool["desired"] for pool in first["pools"]] == [pool["desired"] for pool in second["pools"]]
    assert edge.calls == calls and len(bodies) == 2 and len(edge.writes) == 8


def test_two_invocations_in_one_second_produce_ingestible_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    _, bodies = install(monkeypatch)
    for instant in (NOW + 0.25, NOW + 0.75):
        monkeypatch.setattr(handler.time, "time", lambda moment=instant: moment)
        handler.handler({}, None)
    assert len(bodies) == 2
    previous: dict[tuple[tuple[str, str], ...], tuple[float, int]] = {}
    for body in bodies:
        for labels, points in samples(body).items():
            for value, timestamp in points:
                if labels in previous:
                    old_value, old_timestamp = previous[labels]
                    assert timestamp >= old_timestamp, f"out-of-order sample: {labels}"
                    assert timestamp != old_timestamp or value == old_value, (
                        f"conflicting sample: {labels}: ({old_value}, {old_timestamp}) then ({value}, {timestamp})"
                    )
                previous[labels] = (value, timestamp)


def test_default_sender_posts_the_signed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Any] = []

    def urlopen(request: Any, *, timeout: int) -> Any:
        assert timeout > 0
        requests.append(request)
        return nullcontext()

    monkeypatch.setattr(metrics.urllib.request, "urlopen", urlopen)
    writer = metrics.RemoteWriter(URL, "us-east-1", Credentials("stub", "stub"))
    writer.push([metrics.Series("rhi_scaler_desired_capacity", (("pool", "mock"),), ((1.0, NOW * 1000),))])
    (request,) = requests
    assert request.full_url == URL and request.get_method() == "POST"
    assert request.get_header("Authorization").startswith("AWS4-HMAC-SHA256 ")
    assert samples(request.data)[(("__name__", "rhi_scaler_desired_capacity"), ("pool", "mock"))] == [(1.0, NOW * 1000)]
