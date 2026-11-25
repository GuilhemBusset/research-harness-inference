"""The fleet's alarms in the guardrails stack, over the example configuration (`rhi.guardrails.alarms_fleet`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rhi import config

EXAMPLE = config.load({"RHI_CONFIG": "example"})
POOLS = {pool.name for pool in EXAMPLE.pools}
WALL_CLOCK = {"WallClockWindow": {"Timezone": "UTC"}}

Templates = dict[str, dict[str, Any]]


def template(templates: Templates, prefix: str) -> dict[str, Any]:
    (path,) = [path for path in templates if Path(path).name.startswith(prefix)]
    return templates[path]


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def metrics(alarm: dict[str, Any]) -> list[dict[str, Any]]:
    """Each metric the alarm reads, as its namespace, name and dimensions."""
    if "Metrics" not in alarm:
        return [alarm]
    return [query["MetricStat"]["Metric"] for query in alarm["Metrics"] if "MetricStat" in query]


def by_metric(alarms: dict[str, dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [a for a in alarms.values() if name in {m["MetricName"] for m in metrics(a)}]


def test_the_fleet_alarms_read_auto_scaling_over_20_whole_minutes(templates: Templates) -> None:
    alarms = of_type(template(templates, "RhiGuardrails"), "AWS::CloudWatch::Alarm")
    groups = of_type(template(templates, "RhiPools"), "AWS::AutoScaling::AutoScalingGroup")
    assert POOLS and len(groups) == len(POOLS)
    expected = {
        "GroupTotalInstances": (20, {"total": "GroupTotalInstances", "in_service": "GroupInServiceInstances"}),
    }
    for first, (periods, reads) in expected.items():
        found = by_metric(alarms, first)
        assert len(found) == len(POOLS)
        for alarm in found:
            assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]) == (periods, periods)
            assert alarm["EvaluationWindow"] == WALL_CLOCK
            # A minute with no data does not count towards the alarm.
            assert alarm["TreatMissingData"] == "notBreaching"
            assert (alarm["ComparisonOperator"], alarm["Threshold"]) == ("GreaterThanOrEqualToThreshold", 1)
            stats = {q["Id"]: q["MetricStat"] for q in alarm["Metrics"] if "MetricStat" in q}
            assert {i: s["Metric"]["MetricName"] for i, s in stats.items()} == reads
            assert {s["Period"] for s in stats.values()} == {60}
            assert {s["Metric"]["Namespace"] for s in stats.values()} == {"AWS/AutoScaling"}
            # Every metric of one alarm reads the same group.
            (dimension,) = {str(s["Metric"]["Dimensions"]) for s in stats.values()}
            assert "AutoScalingGroupName" in dimension
            (condition,) = [q for q in alarm["Metrics"] if q.get("ReturnData")]
            assert "in_service == 0" in condition["Expression"]
        # The alarms read Auto Scaling's own metrics, which every pool's group publishes every minute.
        for group in groups.values():
            (collection,) = group["MetricsCollection"]
            assert collection["Granularity"] == "1Minute"
            assert set(reads.values()) <= set(collection["Metrics"])


def test_the_scaler_alarms_on_5_minutes_without_a_completed_run(templates: Templates) -> None:
    alarms = of_type(template(templates, "RhiGuardrails"), "AWS::CloudWatch::Alarm")
    (alarm,) = by_metric(alarms, "RunCompleted")
    assert (alarm["Namespace"], alarm["Period"]) == ("rhi/scaler", 60)
    assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]) == (5, 5)
    assert alarm["EvaluationWindow"] == WALL_CLOCK
    assert alarm["TreatMissingData"] == "breaching"
    assert (alarm["Statistic"], alarm["ComparisonOperator"], alarm["Threshold"]) == ("Sum", "LessThanThreshold", 1)


def test_each_pool_alarms_on_suspended_launches_and_an_hour_in_service_outside_sessions(templates: Templates) -> None:
    alarms = of_type(template(templates, "RhiGuardrails"), "AWS::CloudWatch::Alarm")
    assert EXAMPLE.outside_sessions_alarm_hours == 1
    for name, operator, threshold in (
        ("LaunchesSuspended", "GreaterThanOrEqualToThreshold", 1),
        ("MinutesInServiceOutsideSessions", "GreaterThanThreshold", 60),
    ):
        found = by_metric(alarms, name)
        assert {a["Dimensions"][0]["Value"] for a in found} == POOLS
        for alarm in found:
            assert (alarm["Namespace"], alarm["Period"], alarm["EvaluationPeriods"]) == ("rhi/scaler", 60, 1)
            assert alarm["Dimensions"][0]["Name"] == "Pool"
            assert (alarm["ComparisonOperator"], alarm["Threshold"]) == (operator, threshold)


def test_each_pool_alarms_on_a_session_short_of_its_instances_for_30_whole_minutes(templates: Templates) -> None:
    guardrails = template(templates, "RhiGuardrails")
    alarms = of_type(guardrails, "AWS::CloudWatch::Alarm")
    (topic_id,) = of_type(guardrails, "AWS::SNS::Topic")
    assert EXAMPLE.capacity_alarm_minutes == 30
    found = by_metric(alarms, "InstancesMissing")
    assert sorted(a["Dimensions"][0]["Value"] for a in found) == sorted(POOLS)
    for alarm in found:
        assert (alarm["Namespace"], alarm["Period"]) == ("rhi/scaler", 60)
        assert [d["Name"] for d in alarm["Dimensions"]] == ["Pool"]
        # A minute reads the least of its runs: one that saw the session filled breaks the shortage,
        # so a session filled in the last of the 30 minutes sends nothing.
        assert alarm["Statistic"] == "Minimum"
        # Above zero in each of the 30 minutes, and in exactly those: no older data point stands in
        # for a minute with none, which leaves the alarm in the state it has.
        assert (alarm["ComparisonOperator"], alarm["Threshold"]) == ("GreaterThanThreshold", 0)
        assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]) == (30, 30)
        assert alarm["EvaluationWindow"] == WALL_CLOCK
        assert alarm["TreatMissingData"] == "ignore"
        # It tells the instructor when it fires, and nothing when it clears.
        assert {"Ref": topic_id} in alarm["AlarmActions"]
        assert not alarm.get("OKActions")
        assert not alarm.get("InsufficientDataActions")


def test_no_alarm_reads_a_group_s_desired_capacity(templates: Templates) -> None:
    # A pool whose launch waits for capacity has a desired capacity and nothing in service: under
    # sessions that is its ordinary state, and the alarm on a session short of its instances covers it.
    alarms = of_type(template(templates, "RhiGuardrails"), "AWS::CloudWatch::Alarm")
    assert alarms
    assert by_metric(alarms, "GroupDesiredCapacity") == []
    assert "GroupDesiredCapacity" not in str(alarms)


def test_every_alarm_acts_on_the_alerts_topic_which_cloudwatch_may_publish_to(templates: Templates) -> None:
    guardrails = template(templates, "RhiGuardrails")
    alarms = of_type(guardrails, "AWS::CloudWatch::Alarm")
    (topic_id,) = of_type(guardrails, "AWS::SNS::Topic")
    # The fleet's, and the gateway's error-rate alarm (`rhi.guardrails.alarms_gateway`).
    assert len(alarms) == 1 + 4 * len(POOLS) + 1
    for alarm in alarms.values():
        assert alarm["AlarmActions"] == [{"Ref": topic_id}]
        # A group's name, a pool's name or the load balancer's: no dimension that could identify a student.
        names = {d["Name"] for m in metrics(alarm) for d in m.get("Dimensions", [])}
        assert names <= {"AutoScalingGroupName", "Pool", "LoadBalancer"}
    (policy,) = of_type(guardrails, "AWS::SNS::TopicPolicy").values()
    (grant,) = [
        s for s in policy["PolicyDocument"]["Statement"] if s.get("Principal") == {"Service": "cloudwatch.amazonaws.com"}
    ]
    assert (grant["Effect"], grant["Action"], grant["Resource"]) == ("Allow", "sns:Publish", {"Ref": topic_id})
    assert grant["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}
