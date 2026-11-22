"""The guardrails stack: the alerts topic, its key and its subscription, and Cost Anomaly Detection
behind cost_anomaly_detection (`rhi.guardrails`)."""

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
    assert {nag.created_by(r, nag.SOURCE) for r in resources} <= {"guardrails_alerts", "guardrails_anomaly"}
    assert "Outputs" not in guardrails(templates)


def test_anomaly_detection_behind_its_flag(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> None:
    # Through the app's own build, so cdk-nag runs on the monitor and its subscription too.
    outdir = tmp_path_factory.mktemp("anomaly")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(
        dataclasses.replace(EXAMPLE, cost_anomaly_detection=True), outdir=str(outdir), context=context
    )
    app.synth()
    template = guardrails({p.name: json.loads(p.read_text(encoding="utf-8")) for p in outdir.rglob("*.template.json")})

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
