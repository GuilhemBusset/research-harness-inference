"""The pipeline's events, kept in a log group that Grafana reads (system design, sections 6.6, 7.2
and 7.4): a rule on this pipeline's state changes whose one target is a log group of a fixed name,
that log group's retention from the configuration, its resource policy as a resource of the
template with no custom resource to install it, and the role of Grafana's task, which may read it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    import aws_cdk as cdk

Template = dict[str, Any]

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
LOG_GROUP_NAME = "/rhi/pipeline/events"
DETAIL_TYPES = {
    "CodePipeline Pipeline Execution State Change",
    "CodePipeline Stage Execution State Change",
    "CodePipeline Action Execution State Change",
}
WRITERS = {"events.amazonaws.com", "delivery.logs.amazonaws.com"}
WRITE_ACTIONS = {"logs:CreateLogStream", "logs:PutLogEvents"}
GRAFANA_LOG_ACTIONS = {"logs:StartQuery", "logs:GetLogGroupFields", "logs:GetLogEvents"}
PSEUDO = {"AWS::Partition": "aws", "AWS::Region": "us-east-1", "AWS::AccountId": "123456789012"}


def of_type(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A fragment of a template as text: a pseudo parameter as `PSEUDO` reads it, another reference
    as `<logical ID>` or `<logical ID.attribute>`, and a join of them joined."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and list(value) == ["Ref"]:
        return PSEUDO.get(value["Ref"], f"<{value['Ref']}>")
    if isinstance(value, dict) and list(value) == ["Fn::GetAtt"]:
        return f"<{'.'.join(value['Fn::GetAtt'])}>"
    if isinstance(value, dict) and list(value) == ["Fn::Join"]:
        separator, parts = value["Fn::Join"]
        return separator.join(text(part) for part in parts)
    return json.dumps(value)


def the_log_group(template: Template) -> tuple[str, dict[str, Any]]:
    (found,) = [(n, p) for n, p in of_type(template, "AWS::Logs::LogGroup").items() if p.get("LogGroupName") == LOG_GROUP_NAME]
    return found


def rules_targeting(template: Template, log_group_id: str) -> list[dict[str, Any]]:
    """The rules of the template one of whose targets names the log group."""
    return [
        rule
        for rule in of_type(template, "AWS::Events::Rule").values()
        if any(f"<{log_group_id}" in text(target["Arn"]) for target in rule.get("Targets", []))
    ]


def test_the_rule_sends_this_pipeline_s_state_changes_to_the_log_group_alone(templates: dict[str, Template]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    (pipeline_id,) = of_type(template, "AWS::CodePipeline::Pipeline")
    log_group_id, _ = the_log_group(template)
    # No rule of any other template sends events to it.
    assert [path for path, other in templates.items() if path != PIPELINE_TEMPLATE and log_group_id in json.dumps(other)] == []

    (rule,) = rules_targeting(template, log_group_id)
    assert "EventBusName" not in rule and rule.get("State", "ENABLED") == "ENABLED"
    # CodePipeline's three kinds of state change, of this pipeline's ARN, and nothing else.
    pattern = rule["EventPattern"]
    assert set(pattern) == {"source", "detail-type", "resources"}
    assert pattern["source"] == ["aws.codepipeline"]
    assert set(pattern["detail-type"]) == DETAIL_TYPES and len(pattern["detail-type"]) == len(DETAIL_TYPES)
    (resource,) = pattern["resources"]
    assert text(resource) == f"arn:aws:codepipeline:us-east-1:123456789012:<{pipeline_id}>"

    # Its one target is the log group, by the ARN EventBridge names a log group by: no `:*`, and no
    # role, since the log group's resource policy lets EventBridge write.
    (target,) = rule["Targets"]
    assert text(target["Arn"]) == f"arn:aws:logs:us-east-1:123456789012:log-group:<{log_group_id}>"
    assert "RoleArn" not in target


def test_a_record_keeps_the_run_s_fields_and_not_the_commit_s_author(templates: dict[str, Template]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    log_group_id, _ = the_log_group(template)
    ((target,),) = [rule["Targets"] for rule in rules_targeting(template, log_group_id)]
    # The target writes a record made of chosen fields, never the whole event or its detail, whose
    # trigger names the commit's author.
    assert "Input" not in target and "InputPath" not in target
    transformer = target["InputTransformer"]
    assert transformer["InputPathsMap"] == {
        "time": "$.time",
        "detail-type": "$.detail-type",
        "pipeline": "$.detail.pipeline",
        "execution-id": "$.detail.execution-id",
        "state": "$.detail.state",
        "stage": "$.detail.stage",
        "action": "$.detail.action",
    }
    assert "<aws.events." not in transformer["InputTemplate"]
    # The record CloudWatch Logs takes: the event's time, and a message as text, a JSON object.
    record = json.loads(transformer["InputTemplate"].replace("<time>", '"TIME"'))
    assert record["timestamp"] == "TIME"
    message = json.loads(record["message"])
    assert message == {field: f"<{field}>" for field in set(transformer["InputPathsMap"]) - {"time"}}


def test_the_log_group_keeps_its_events_for_the_configured_days(templates: dict[str, Template]) -> None:
    _, log_group = the_log_group(templates[PIPELINE_TEMPLATE])
    assert config.load({"RHI_CONFIG": "example"}).pipeline_events_retention_days == 90
    assert log_group["RetentionInDays"] == 90

    # The key, and nothing else, sets it.
    import aws_cdk as cdk
    from aws_cdk import aws_codepipeline as codepipeline

    from rhi.pipeline.events import PipelineEvents

    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Alone")
    pipeline = codepipeline.Pipeline.from_pipeline_arn(
        stack, "Pipeline", stack.format_arn(service="codepipeline", resource="example")
    )
    configured = dataclasses.replace(config.load({"RHI_CONFIG": "example"}), pipeline_events_retention_days=30)
    events = PipelineEvents(stack, "Events", config=configured, pipeline=pipeline)
    assert stack.resolve(events.log_group.retention_in_days) == 30


def test_the_resource_policy_is_a_resource_of_the_template(templates: dict[str, Template], app: cdk.App) -> None:
    import aws_cdk as cdk

    template = templates[PIPELINE_TEMPLATE]
    log_group_id, _ = the_log_group(template)
    (rule_id,) = [n for n, r in of_type(template, "AWS::Events::Rule").items() if r in rules_targeting(template, log_group_id)]

    # One policy names the log group: EventBridge's two principals may create streams in it and
    # put events to them, and nothing else.
    policies = {
        name: properties
        for name, properties in of_type(template, "AWS::Logs::ResourcePolicy").items()
        if log_group_id in json.dumps(properties)
    }
    ((policy_id, policy),) = policies.items()
    document = json.loads(text(policy["PolicyDocument"]).replace(f"<{log_group_id}.Arn>", "LOG_GROUP_ARN"))
    (statement,) = document["Statement"]
    assert statement["Effect"] == "Allow"
    assert set(as_list(statement["Principal"]["Service"])) == WRITERS
    assert set(as_list(statement["Action"])) == WRITE_ACTIONS
    # The log group's ARN as CloudFormation gives it, which ends `:*`: the log group and its streams.
    assert as_list(statement["Resource"]) == ["LOG_GROUP_ARN"]
    assert set(statement) == {"Effect", "Principal", "Action", "Resource"}
    # The rule is created once the policy is.
    assert policy_id in as_list(template["Resources"][rule_id].get("DependsOn", []))

    # No custom resource serves it: the construct holds the log group, the policy and the rule, and
    # no resource of the template calls CloudWatch Logs' PutResourcePolicy.
    pipeline_stack = app.node.find_child("RhiPipeline")
    held = {
        child.cfn_resource_type
        for child in pipeline_stack.node.find_child("Events").node.find_all()
        if isinstance(child, cdk.CfnResource)
    }
    assert held == {"AWS::Logs::LogGroup", "AWS::Logs::ResourcePolicy", "AWS::Events::Rule"}
    assert not [
        name
        for name, resource in template["Resources"].items()
        if (resource["Type"].startswith("Custom::") or resource["Type"] == "AWS::CloudFormation::CustomResource")
        and ("PutResourcePolicy" in json.dumps(resource) or log_group_id in json.dumps(resource))
    ]


def test_grafana_s_role_reads_the_log_group_by_its_name(templates: dict[str, Template]) -> None:
    (path,) = [path for path in templates if Path(path).name.startswith("RhiObservability")]
    template = templates[path]
    (task,) = of_type(template, "AWS::ECS::TaskDefinition").values()
    role_id = task["TaskRoleArn"]["Fn::GetAtt"][0]
    statements = [
        statement
        for policy in of_type(template, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in policy["Roles"]
        for statement in as_list(policy["PolicyDocument"]["Statement"])
    ]
    resources = {
        text(resource)
        for statement in statements
        if set(as_list(statement["Action"])) == GRAFANA_LOG_ACTIONS
        for resource in as_list(statement["Resource"])
    }
    # By its name, as text: no reference to the pipeline's stack, which is another assembly.
    assert f"arn:aws:logs:us-east-1:123456789012:log-group:{LOG_GROUP_NAME}:*" in resources
    # And still no build project's log group.
    assert not [resource for resource in resources if "/aws/codebuild" in resource or resource.endswith("log-group:*")]
