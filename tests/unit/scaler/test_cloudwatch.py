"""What a run publishes to CloudWatch (`rhi_scaler.cloudwatch`), and when (`rhi_scaler.handler`)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from rhi_common.pool_config import parse_pools
from rhi_common.records import Mode, RecordError
from rhi_scaler import handler as scaler
from rhi_scaler.aws_io import Clients, Run, TableNames
from rhi_scaler.metrics import DecisionCounts
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply
from rhi_scaler.settings import Settings

NOW = 1_800_000_000
ENTRY = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.0168},
    "max_instances": 2,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 4,
    "context_length": 32_768,
    "largest_output": 4_096,
}
COURSEWORK, MOCK = parse_pools({"coursework": ENTRY, "mock": ENTRY})
# Coursework has been in service since an hour and a minute ago with no session on; mock has no
# instance in service and its launches are suspended.
RUN = Run(
    inputs=Inputs(
        now=NOW,
        pools=(
            PoolInputs(COURSEWORK, Mode.ENABLED, Supply(1, in_service=("i-a",)), memory=Memory(outside_sessions_since=NOW - 3660)),
            PoolInputs(MOCK, Mode.ENABLED, Supply(0), memory=Memory(outside_sessions_since=NOW - 600)),
        ),
    ),
    groups={},
)
DECISION = Decision(
    pools=(
        PoolDecision("coursework", 1, (), False, Memory(), Reason.UNCHANGED),
        PoolDecision("mock", 0, (), True, Memory(launch_suspended=True), Reason.LAUNCH_SUSPENDED),
    )
)
SETTINGS = Settings(region="us-east-1", pools=(), tables=TableNames("heartbeats", "pool-state"), workspace_url="")


class Recorder:
    """A CloudWatch client that keeps every call made to it, and a writer that keeps every push."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **parameters: Any) -> None:
        self.calls.append(parameters)

    def push(self, series: object) -> None:
        del series


def a_run(monkeypatch: pytest.MonkeyPatch, cloudwatch: Any, failing: str | None = None) -> Decision:
    """`run_cycle` over the run read above and its decision, with one step raising if named."""

    def step(name: str, result: object) -> Any:
        def call(*_: object) -> object:
            if name == failing:
                raise RuntimeError(name)
            return result

        return call

    monkeypatch.setattr(scaler.aws_io, "read", step("read", RUN))
    monkeypatch.setattr(scaler, "decide", step("decide", DECISION))
    monkeypatch.setattr(scaler.aws_io, "apply", step("apply", None))
    clients = Clients(autoscaling=None, elbv2=None, dynamodb=None, cloudwatch=cloudwatch)
    return scaler.run_cycle(SETTINGS, clients, Recorder(), NOW, DecisionCounts())  # ty: ignore[invalid-argument-type] - a test double


def test_a_completed_run_publishes_that_it_completed_and_each_pool_s_two_values(monkeypatch: pytest.MonkeyPatch) -> None:
    client = boto3.client("cloudwatch", region_name="us-east-1", aws_access_key_id="stub", aws_secret_access_key="stub")

    def per_pool(pool: str, suspended: float, outside: float) -> list[dict[str, Any]]:
        pool_dimension = [{"Name": "Pool", "Value": pool}]
        return [
            {"MetricName": "LaunchesSuspended", "Dimensions": pool_dimension, "Value": suspended, "Unit": "None"},
            {"MetricName": "MinutesInServiceOutsideSessions", "Dimensions": pool_dimension, "Value": outside, "Unit": "None"},
        ]

    expected = {
        "Namespace": "rhi/scaler",
        "MetricData": [
            {"MetricName": "RunCompleted", "Value": 1.0, "Unit": "Count"},
            *per_pool("coursework", 0.0, 61.0),
            *per_pool("mock", 1.0, 0.0),
        ],
    }
    with Stubber(client) as stubber:
        stubber.add_response("put_metric_data", {}, expected)
        a_run(monkeypatch, client)
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("failing", ["read", "decide", "apply"])
def test_a_run_that_raises_publishes_nothing(monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    recorder = Recorder()
    with pytest.raises(RuntimeError, match=failing):
        a_run(monkeypatch, recorder, failing)
    assert recorder.calls == []


def test_no_published_metric_has_a_dimension_other_than_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    a_run(monkeypatch, recorder)
    (call,) = recorder.calls
    for datum in call["MetricData"]:
        assert [dimension["Name"] for dimension in datum.get("Dimensions", [])] in ([], ["Pool"])
    assert {d["Dimensions"][0]["Value"] for d in call["MetricData"] if "Dimensions" in d} == {"coursework", "mock"}


def test_a_switch_that_could_not_be_read_fails_the_run_after_its_decision_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[Decision] = []
    recorder = Recorder()
    monkeypatch.setattr(scaler.aws_io, "read", lambda *_: replace(RUN, session_error=RecordError("unreadable")))
    monkeypatch.setattr(scaler, "decide", lambda *_: DECISION)
    monkeypatch.setattr(scaler.aws_io, "apply", lambda clients, tables, run, decision: applied.append(decision))
    clients = Clients(autoscaling=None, elbv2=None, dynamodb=None, cloudwatch=recorder)
    with pytest.raises(RecordError):
        scaler.run_cycle(SETTINGS, clients, Recorder(), NOW, DecisionCounts())  # ty: ignore[invalid-argument-type] - a test double
    assert recorder.calls == []
    # The decision was applied: coursework, in service, kept its stretch; mock, with none in service, ended its own.
    (decision,) = applied
    assert [pool.memory.outside_sessions_since for pool in decision.pools] == [NOW - 3660, None]


def test_a_publication_cloudwatch_refuses_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    client = boto3.client("cloudwatch", region_name="us-east-1", aws_access_key_id="stub", aws_secret_access_key="stub")
    with Stubber(client) as stubber:
        stubber.add_client_error("put_metric_data", "InvalidParameterValue")
        with pytest.raises(ClientError):
            a_run(monkeypatch, client)
        stubber.assert_no_pending_responses()
