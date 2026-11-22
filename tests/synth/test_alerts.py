"""The guardrails stack: the alerts topic, its key and its subscription, the cost budget, and Cost
Anomaly Detection behind cost_anomaly_detection (`rhi.guardrails`)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk
    import pytest

    Templates = dict[str, dict[str, Any]]

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
PUBLISHERS = {"cloudwatch.amazonaws.com", "budgets.amazonaws.com", "costalerts.amazonaws.com"}


def guardrails(templates: Templates) -> dict[str, Any]:
    (path,) = [path for path in templates if Path(path).name.startswith("RhiGuardrails")]
    return templates[path]


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def services(statement: dict[str, Any]) -> set[str]:
    principal = statement.get("Principal")
    service = principal.get("Service", []) if isinstance(principal, dict) else []
    return {service} if isinstance(service, str) else set(service)


def test_the_topic_is_encrypted_with_a_rotating_key_the_three_services_may_use(templates: Templates) -> None:
    template = guardrails(templates)
    ((topic_id, topic),) = of_type(template, "AWS::SNS::Topic").items()
    ((key_id, key),) = of_type(template, "AWS::KMS::Key").items()
    assert topic["KmsMasterKeyId"] == {"Fn::GetAtt": [key_id, "Arn"]}
    assert (key["EnableKeyRotation"], key["RotationPeriodInDays"]) == (True, 365)
    allowed = [s for s in key["KeyPolicy"]["Statement"] if s["Effect"] == "Allow" and services(s)]
    assert set().union(*map(services, allowed)) == PUBLISHERS
    for statement in allowed:
        assert set(statement["Action"]) == {"kms:GenerateDataKey*", "kms:Decrypt"}
        # Only for the stack's own account, never a service acting for another.
        assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}
    # Unencrypted transport is refused.
    (policy,) = of_type(template, "AWS::SNS::TopicPolicy").values()
    assert policy["Topics"] == [{"Ref": topic_id}]
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in policy["PolicyDocument"]["Statement"]
    )


def test_the_topic_has_one_e_mail_subscription_to_the_admin_address(templates: Templates) -> None:
    template = guardrails(templates)
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    (subscription,) = of_type(template, "AWS::SNS::Subscription").values()
    assert subscription == {"Protocol": "email", "TopicArn": {"Ref": topic_id}, "Endpoint": EXAMPLE.admin_email}
    assert EXAMPLE.admin_email == "admin@example.com"


def synthesized(app_module: ModuleType, outdir: Path, configuration: config.Config) -> Templates:
    """The templates of the app's own build under this configuration, so that cdk-nag runs on it."""
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app_module.build(configuration, outdir=str(outdir), context=context).synth()
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in outdir.rglob("*.template.json")}


def budget_thresholds(template: dict[str, Any]) -> list[int]:
    """The budget's thresholds, each checked to be a percentage of actual cost sent to the topic."""
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    (budget,) = of_type(template, "AWS::Budgets::Budget").values()
    notifications = budget["NotificationsWithSubscribers"]
    for n in notifications:
        assert (n["Notification"]["NotificationType"], n["Notification"]["ThresholdType"]) == ("ACTUAL", "PERCENTAGE")
        assert n["Subscribers"] == [{"Address": {"Ref": topic_id}, "SubscriptionType": "SNS"}]
    return sorted(n["Notification"]["Threshold"] for n in notifications)


def test_the_budget_alerts_the_topic_at_each_threshold_of_the_monthly_amount(templates: Templates) -> None:
    template = guardrails(templates)
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    ((budget_id, budget),) = of_type(template, "AWS::Budgets::Budget").items()
    data = budget["Budget"]
    assert (data["BudgetType"], data["TimeUnit"]) == ("COST", "MONTHLY")
    assert data["BudgetLimit"] == {"Amount": EXAMPLE.monthly_budget_usd, "Unit": "USD"}
    assert budget_thresholds(template) == sorted(EXAMPLE.budget_alert_percentages) == [50, 80, 100]
    # AWS Budgets may publish, from the stack's own account, before the budget exists.
    ((policy_id, policy),) = of_type(template, "AWS::SNS::TopicPolicy").items()
    (grant,) = [s for s in policy["PolicyDocument"]["Statement"] if services(s) == {"budgets.amazonaws.com"}]
    assert (grant["Effect"], grant["Action"], grant["Resource"]) == ("Allow", "sns:Publish", {"Ref": topic_id})
    assert grant["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}
    assert policy_id in template["Resources"][budget_id]["DependsOn"]


def test_the_budget_alerts_at_the_percentages_the_configuration_gives(
    app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory
) -> None:
    configuration = dataclasses.replace(EXAMPLE, budget_alert_percentages=(25, 90))
    template = guardrails(synthesized(app_module, tmp_path_factory.mktemp("percentages"), configuration))
    assert budget_thresholds(template) == [25, 90]


def test_without_the_flag_there_is_no_anomaly_monitor(templates: Templates) -> None:
    assert EXAMPLE.cost_anomaly_detection is False
    everything = [r["Type"] for template in templates.values() for r in template.get("Resources", {}).values()]
    assert not [kind for kind in everything if kind.startswith("AWS::CE::")]


def test_the_stack_creates_no_resource_of_its_own_and_nothing_reads_it(app: cdk.App, templates: Templates) -> None:
    # Imported once the app has started jsii (`conftest.app_module`).
    import aws_cdk as cdk

    from rhi import nag

    stack = app.node.find_child("Rhi").node.find_child("Guardrails")
    resources = [
        c
        for c in stack.node.find_all()
        if isinstance(c, cdk.CfnResource) and c.cfn_resource_type != "AWS::CDK::Metadata"
    ]
    assert resources
    assert {nag.created_by(r, nag.SOURCE) for r in resources} <= {
        "guardrails_alerts",
        "guardrails_budget",
        "guardrails_anomaly",
        "guardrails_alarms_fleet",
        "guardrails_alarms_gateway",
    }
    assert "Outputs" not in guardrails(templates)


def test_anomaly_detection_behind_its_flag(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> None:
    # Through the app's own build, so cdk-nag runs on the monitor and its subscription too.
    configuration = dataclasses.replace(EXAMPLE, cost_anomaly_detection=True)
    template = guardrails(synthesized(app_module, tmp_path_factory.mktemp("anomaly"), configuration))

    ((monitor_id, monitor),) = of_type(template, "AWS::CE::AnomalyMonitor").items()
    assert (monitor["MonitorType"], monitor["MonitorDimension"]) == ("DIMENSIONAL", "SERVICE")
    (topic_id,) = of_type(template, "AWS::SNS::Topic")
    ((subscription_id, subscription),) = of_type(template, "AWS::CE::AnomalySubscription").items()
    assert subscription["Frequency"] == "IMMEDIATE"
    assert subscription["Subscribers"] == [{"Address": {"Ref": topic_id}, "Type": "SNS"}]
    assert subscription["MonitorArnList"] == [{"Fn::GetAtt": [monitor_id, "MonitorArn"]}]
    threshold = json.loads(subscription["ThresholdExpression"])["Dimensions"]
    assert (threshold["Key"], threshold["MatchOptions"], threshold["Values"]) == (
        "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
        ["GREATER_THAN_OR_EQUAL"],
        ["0"],
    )

    # Cost Anomaly Detection may publish, from the stack's own account, before the subscription exists.
    ((policy_id, policy),) = of_type(template, "AWS::SNS::TopicPolicy").items()
    (grant,) = [s for s in policy["PolicyDocument"]["Statement"] if services(s) == {"costalerts.amazonaws.com"}]
    assert (grant["Effect"], grant["Action"], grant["Resource"]) == ("Allow", "sns:Publish", {"Ref": topic_id})
    assert grant["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}
    assert policy_id in template["Resources"][subscription_id]["DependsOn"]
