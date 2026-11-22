"""The gateway's error-rate alarm in the guardrails stack, over the example configuration (`rhi.guardrails.alarms_gateway`)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from rhi import config
from rhi_gateway.telemetry import FAULT_STATUS, NO_CAUSE

EXAMPLE = config.load({"RHI_CONFIG": "example"})

Templates = dict[str, dict[str, Any]]


def template(templates: Templates, prefix: str) -> dict[str, Any]:
    (path,) = [path for path in templates if Path(path).name.startswith(prefix)]
    return templates[path]


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def output(templates: Templates, reference: dict[str, Any]) -> Any:
    """What a value read from another stack's output is in that stack."""
    read = reference["Fn::GetStackOutput"]
    return template(templates, read["StackName"].replace("-", ""))["Outputs"][read["OutputName"]]["Value"]


def depends_on(cloud_assembly: Path, stack_name: str) -> set[str]:
    """The stacks the stack named `stack_name` deploys after, by their names."""
    artifacts = {
        artifact_id: artifact
        for manifest in cloud_assembly.rglob("manifest.json")
        for artifact_id, artifact in json.loads(manifest.read_text(encoding="utf-8")).get("artifacts", {}).items()
    }
    names = {i: a["properties"]["stackName"] for i, a in artifacts.items() if "stackName" in a.get("properties", {})}
    (stack,) = [artifacts[i] for i, name in names.items() if name == stack_name]
    return {names[i] for i in stack.get("dependencies", []) if i in names}


def gateway_alarm(templates: Templates) -> dict[str, Any]:
    alarms = of_type(template(templates, "RhiGuardrails"), "AWS::CloudWatch::Alarm")
    (alarm,) = [a for a in alarms.values() if "Metrics" in a and any(q.get("Id") == "own" for q in a["Metrics"])]
    return alarm


def share(alarm: dict[str, Any], **counts: float | None) -> float | None:
    """The alarm's returned expression over one period's counts, by query id: None is a count with no
    data point, and the result is None where the expression has no value."""
    (expression,) = [q["Expression"] for q in alarm["Metrics"] if q.get("ReturnData")]
    row = ", ".join(f'? AS "{query_id}"' for query_id in counts)
    with closing(sqlite3.connect(":memory:")) as db:
        db.create_function("FILL", 2, lambda value, default: default if value is None else value)
        db.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
        (value,) = db.execute(f"SELECT {expression} FROM (SELECT {row})", tuple(counts.values())).fetchone()
    return value


def test_the_filter_counts_the_gateways_own_faults_with_no_dimension(
    templates: Templates, cloud_assembly: Path
) -> None:
    (metric_filter,) = of_type(template(templates, "RhiGuardrails"), "AWS::Logs::MetricFilter").values()
    pattern = metric_filter["FilterPattern"]
    assert FAULT_STATUS == 500 and NO_CAUSE == "none"
    assert "$.status >= 500" in pattern and '$.cause = "none"' in pattern and "&&" in pattern
    (transformation,) = metric_filter["MetricTransformations"]
    assert transformation["MetricNamespace"] == "rhi/gateway"
    assert "Dimensions" not in transformation
    # The gateway stack's log group, by its fixed name, and the gateway stack deploys first.
    gateway_logs = of_type(template(templates, "RhiGateway"), "AWS::Logs::LogGroup").values()
    assert metric_filter["LogGroupName"] in {group.get("LogGroupName") for group in gateway_logs}
    assert "Rhi-Gateway" in depends_on(cloud_assembly, "Rhi-Guardrails")


def test_the_alarm_compares_the_failed_share_of_the_load_balancers_requests_with_5_percent_over_5_minutes(
    templates: Templates,
) -> None:
    assert (EXAMPLE.gateway_error_alarm_percent, EXAMPLE.gateway_error_alarm_minutes) == (5, 5)
    alarm = gateway_alarm(templates)
    stats = {q["Id"]: q["MetricStat"] for q in alarm["Metrics"] if "MetricStat" in q}
    (metric_filter,) = of_type(template(templates, "RhiGuardrails"), "AWS::Logs::MetricFilter").values()
    (transformation,) = metric_filter["MetricTransformations"]
    assert stats["own"]["Metric"] == {"Namespace": "rhi/gateway", "MetricName": transformation["MetricName"]}
    # The public load balancer's own 5xx answers and its requests.
    (balancer_id,) = of_type(template(templates, "RhiNetwork"), "AWS::ElasticLoadBalancingV2::LoadBalancer")
    for query, name in (("elb5xx", "HTTPCode_ELB_5XX_Count"), ("requests", "RequestCount")):
        metric = stats[query]["Metric"]
        assert (metric["Namespace"], metric["MetricName"]) == ("AWS/ApplicationELB", name)
        (dimension,) = metric["Dimensions"]
        assert dimension["Name"] == "LoadBalancer"
        assert output(templates, dimension["Value"]) == {"Fn::GetAtt": [balancer_id, "LoadBalancerFullName"]}
    assert {(s["Stat"], s["Period"]) for s in stats.values()} == {("Sum", 300)}
    # The two 5xx counts over the requests that reached a target: one 504 in 19 such requests breaches.
    assert share(alarm, own=None, elb5xx=1.0, requests=19.0) == 100 / 19
    assert share(alarm, own=50.0, elb5xx=None, requests=1000.0) == 5
    # Both sources in the same period add up: 3 + 3 failures in 100 requests is 6%, and breaches.
    assert share(alarm, own=3.0, elb5xx=3.0, requests=100.0) == 6
    # A gateway at zero tasks: no request reaches a target, and the load balancer answers all of them.
    assert share(alarm, own=None, elb5xx=40.0, requests=None) == 100
    assert share(alarm, own=None, elb5xx=40.0, requests=0.0) == 100
    assert (alarm["ComparisonOperator"], alarm["Threshold"]) == ("GreaterThanThreshold", 5)
    assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]) == (1, 1)
    # A period with no request has no share, and the alarm clears.
    assert share(alarm, own=None, elb5xx=None, requests=None) is None
    assert alarm["TreatMissingData"] == "notBreaching"


def test_the_alarm_acts_on_the_alerts_topic(templates: Templates) -> None:
    (topic_id,) = of_type(template(templates, "RhiGuardrails"), "AWS::SNS::Topic")
    assert gateway_alarm(templates)["AlarmActions"] == [{"Ref": topic_id}]
