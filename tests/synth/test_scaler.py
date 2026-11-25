"""The scaler in the pools stack (system design, sections 2, 5.1 and 5.4): a function packaged by
`rhi.packaging`, one invocation at a time, invoked every minute, logging to /rhi/scaler, with its
settings in the shape `rhi_scaler.settings` reads and a role that may do what a run does and no more.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config
from rhi_scaler.settings import SETTINGS_VARIABLE, parse_settings

if TYPE_CHECKING:
    import aws_cdk as cdk

Template = dict[str, Any]

EXAMPLE = config.load({"RHI_CONFIG": "example"})
HANDLER = "rhi_scaler.handler.handler"
REPO = Path(__file__).resolve().parents[2]
# What the template leaves to the deployment, as the test gives it: the workspace's endpoint ends
# with a slash, as Managed Prometheus writes it.
ENDPOINT = "https://aps-workspaces.example.com/workspaces/ws-example/"

GROUP_WRITES = {
    "autoscaling:ResumeProcesses",
    "autoscaling:SetDesiredCapacity",
    "autoscaling:SetInstanceProtection",
    "autoscaling:SuspendProcesses",
}
DESCRIBES = {
    "autoscaling:DescribeAutoScalingGroups",
    "autoscaling:DescribeScalingActivities",
    "elasticloadbalancing:DescribeTargetHealth",
}
ITEMS = {
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:UpdateItem",
}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def find_template(templates: dict[str, Template], prefix: str) -> Template:
    (template,) = [template for path, template in templates.items() if Path(path).name.startswith(prefix)]
    return template


def of_type(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


def scaler_function(template: Template) -> tuple[str, dict[str, Any]]:
    (found,) = [item for item in of_type(template, "AWS::Lambda::Function").items() if item[1]["Handler"] == HANDLER]
    return found


def resolve(value: Any, templates: dict[str, Template]) -> str:
    """A value of the template as text, with what the deployment chooses given as the test's example:
    a Ref as `ref:<logical ID>`, the workspace's endpoint as `ENDPOINT`, an import as what its stack
    exports."""
    if isinstance(value, str):
        return value
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(resolve(part, templates) for part in parts)
    if "Ref" in value:
        return f"ref:{value['Ref']}"
    if "Fn::GetAtt" in value:
        logical_id, attribute = value["Fn::GetAtt"]
        return ENDPOINT if attribute == "PrometheusEndpoint" else f"getatt:{logical_id}.{attribute}"
    if "Fn::ImportValue" in value:
        (exported,) = [
            output["Value"]
            for template in templates.values()
            for output in template.get("Outputs", {}).values()
            if output.get("Export", {}).get("Name") == value["Fn::ImportValue"]
        ]
        return resolve(exported, templates)
    raise AssertionError(f"not a value the test reads: {value}")


def role_statements(template: Template, templates: dict[str, Template], role_id: str) -> list[dict[str, Any]]:
    """Every statement the role holds: its inline policies, the policies of its template that name
    it, and the managed policies it attaches, read from the template that holds each."""
    role = template["Resources"][role_id]["Properties"]
    found = [statement for policy in role.get("Policies", []) for statement in policy["PolicyDocument"]["Statement"]]
    for policy in of_type(template, "AWS::IAM::Policy").values():
        if {"Ref": role_id} in policy["Roles"]:
            found += as_list(policy["PolicyDocument"]["Statement"])
    for attached in role.get("ManagedPolicyArns", []):
        reference = resolve(attached, templates)
        assert reference.startswith("ref:"), f"the role attaches a policy the test cannot read: {attached}"
        (policy,) = [
            resource["Properties"]
            for other in templates.values()
            for logical_id, resource in other["Resources"].items()
            if logical_id == reference.removeprefix("ref:") and resource["Type"] == "AWS::IAM::ManagedPolicy"
        ]
        found += as_list(policy["PolicyDocument"]["Statement"])
    return found


@pytest.fixture(scope="module")
def pools(templates: dict[str, Template]) -> Template:
    return find_template(templates, "RhiPools")


def test_one_function_at_a_time_packaged_without_docker_logging_to_rhi_scaler(
    pools: Template, app_module: Any, tmp_path: Path
) -> None:
    import aws_cdk as cdk
    from aws_cdk import aws_lambda as lambda_

    from rhi import packaging

    _, function = scaler_function(pools)
    assert function["ReservedConcurrentExecutions"] == 1
    assert function["Architectures"] == ["arm64"]
    assert function["Runtime"] == packaging.RUNTIME.name
    log_group = function["LoggingConfig"]["LogGroup"]["Ref"]
    assert of_type(pools, "AWS::Logs::LogGroup")[log_group]["LogGroupName"] == "/rhi/scaler"

    # The code is the scaler's bundle of `rhi.packaging`, local bundling with no container: the same
    # asset as a function given `packaging.function_code("scaler", ...)` gets.
    app = cdk.App(outdir=str(tmp_path), context={"aws:cdk:bundling-stacks": []})
    stack = cdk.Stack(app, "Probe")
    probe = lambda_.Function(
        stack,
        "Probe",
        runtime=packaging.RUNTIME,
        architecture=lambda_.Architecture.ARM_64,
        handler=HANDLER,
        code=packaging.function_code("scaler", lambda_.Architecture.ARM_64),
    )
    expected = stack.resolve(probe.node.default_child.code)["s3Key"]  # ty: ignore[missing-attribute]
    assert function["Code"]["S3Key"] == expected


def test_a_rule_invokes_the_function_every_minute(pools: Template) -> None:
    function_id, _ = scaler_function(pools)
    rules = [rule for rule in of_type(pools, "AWS::Events::Rule").values() if rule.get("ScheduleExpression")]
    (rule,) = [rule for rule in rules if {"Fn::GetAtt": [function_id, "Arn"]} in [t["Arn"] for t in rule["Targets"]]]
    assert rule["ScheduleExpression"] == "rate(1 minute)"
    assert rule.get("State", "ENABLED") == "ENABLED"


def test_the_settings_name_every_pool_s_group_and_target_group_and_read(
    pools: Template, templates: dict[str, Template]
) -> None:
    _, function = scaler_function(pools)
    settings = parse_settings(resolve(function["Environment"]["Variables"][SETTINGS_VARIABLE], templates), "eu-west-1")

    # Every pool as the example gives it, but for the zones of its group, which the scaler is not told.
    assert [pool.config for pool in settings.pools] == [dataclasses.replace(pool, zones=None) for pool in EXAMPLE.pools]
    assert [pool.config.name for pool in settings.pools] == ["coursework", "mock"]
    groups = of_type(pools, "AWS::AutoScaling::AutoScalingGroup")
    for pool in settings.pools:
        group = groups[pool.group.removeprefix("ref:")]
        assert {"Key": "rhi-pool", "Value": pool.config.name, "PropagateAtLaunch": False} in group["Tags"]
        assert [f"ref:{arn['Ref']}" for arn in group["TargetGroupARNs"]] == [pool.target_group]
        target_group = pools["Resources"][pool.target_group.removeprefix("ref:")]
        assert target_group["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"

    tables = of_type(pools, "AWS::DynamoDB::Table")
    assert settings.tables.heartbeats.removeprefix("ref:") in tables
    assert settings.tables.heartbeats.startswith("ref:TablesHeartbeats")
    assert settings.tables.pool_state.startswith("ref:TablesPoolState")
    assert settings.tables.pool_state.removeprefix("ref:") in tables
    assert settings.workspace_url == ENDPOINT + "api/v1/remote_write"
    assert settings.session_max_hours == EXAMPLE.session_max_hours


def test_the_role_may_do_what_a_run_does_and_nothing_else(pools: Template, templates: dict[str, Template]) -> None:
    _, function = scaler_function(pools)
    role_id = function["Role"]["Fn::GetAtt"][0]
    granted: dict[str, set[str]] = {}
    conditions: dict[str, Any] = {}
    for statement in role_statements(pools, templates, role_id):
        assert statement["Effect"] == "Allow"
        assert "NotAction" not in statement and "NotResource" not in statement
        for action in as_list(statement["Action"]):
            for resource in as_list(statement["Resource"]):
                granted.setdefault(action, set()).add(resolve(resource, templates))
            if action in GROUP_WRITES or action == "cloudwatch:PutMetricData":
                conditions.setdefault(action, []).append(statement.get("Condition"))

    log_group = function["LoggingConfig"]["LogGroup"]["Ref"]
    observability = find_template(templates, "RhiObservability")
    (workspace,) = of_type(observability, "AWS::APS::Workspace")
    group_pattern = (
        "arn:ref:AWS::Partition:autoscaling:ref:AWS::Region:ref:AWS::AccountId"
        ":autoScalingGroup:*:autoScalingGroupName/*"
    )
    tables = {
        f"getatt:{name}.Arn"
        for name in of_type(pools, "AWS::DynamoDB::Table")
        if name.startswith(("TablesHeartbeats", "TablesPoolState"))
    }
    assert len(tables) == 2
    assert granted == {
        **{action: {group_pattern} for action in GROUP_WRITES},
        **{action: {"*"} for action in DESCRIBES},
        **{action: tables for action in ITEMS},
        "logs:CreateLogStream": {f"getatt:{log_group}.Arn"},
        "logs:PutLogEvents": {f"getatt:{log_group}.Arn"},
        "aps:RemoteWrite": {f"getatt:{workspace}.Arn"},
        "cloudwatch:PutMetricData": {"*"},
    }
    # The groups by pattern, narrowed to those this stack created for the stage's pools.
    for action in GROUP_WRITES:
        assert conditions[action] == [
            {
                "StringEquals": {
                    "autoscaling:ResourceTag/aws:cloudformation:stack-id": {"Ref": "AWS::StackId"},
                    "autoscaling:ResourceTag/rhi-pool": [pool.name for pool in EXAMPLE.pools],
                }
            }
        ]
    # Metrics, in every statement that grants them, to the scaler's namespace and no other.
    assert conditions["cloudwatch:PutMetricData"] == [{"StringEquals": {"cloudwatch:namespace": "rhi/scaler"}}]


def test_the_stage_reaches_the_function_and_nothing_else_may_invoke_it(app: cdk.App, pools: Template) -> None:
    from aws_cdk import aws_lambda as lambda_

    stage = app.node.find_child("Rhi")
    function = stage.pools.scaler.function  # ty: ignore[missing-attribute]
    assert isinstance(function, lambda_.Function)
    function_id, _ = scaler_function(pools)
    assert stage.pools.resolve(function.function_arn) == {"Fn::GetAtt": [function_id, "Arn"]}  # ty: ignore[missing-attribute]

    invokers = [
        permission
        for permission in of_type(pools, "AWS::Lambda::Permission").values()
        if function_id in str(permission["FunctionName"])
    ]
    (rule_id,) = [
        name
        for name, rule in of_type(pools, "AWS::Events::Rule").items()
        if {"Fn::GetAtt": [function_id, "Arn"]} in [t["Arn"] for t in rule["Targets"]]
    ]
    assert invokers == [
        {
            "Action": "lambda:InvokeFunction",
            "FunctionName": {"Fn::GetAtt": [function_id, "Arn"]},
            "Principal": "events.amazonaws.com",
            "SourceArn": {"Fn::GetAtt": [rule_id, "Arn"]},
        }
    ]


def test_the_admin_finds_the_function_and_the_pool_state_table_by_their_logical_ids(app: cdk.App) -> None:
    from rhi_admin import main

    stack = app.node.find_child("Rhi").pools  # ty: ignore[missing-attribute]
    assert stack.stack_name == main.POOLS_STACK_NAME
    function = stack.resolve(stack.get_logical_id(stack.scaler.function.node.default_child))
    table = stack.resolve(stack.get_logical_id(stack.tables.by_name["pool-state"].node.default_child))
    assert (function, table) == (main.SCALER_FUNCTION_LOGICAL_ID, main.POOL_STATE_LOGICAL_ID)


@pytest.fixture(scope="module")
def changed(app_module: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The scaler's settings in a stage whose configuration is the example's with two values
    changed: a pool priced above the 5 dollars an hour a pool was once held to, and a longest
    session that is not the default."""
    import json
    import tomllib

    import aws_cdk as cdk
    from aws_cdk.assertions import Template as Assertions

    source = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
    mock = source["pools"]["mock"]
    mock["prices"] = dict.fromkeys(mock["instance_types"], 6.0)
    source["session_max_hours"] = 6
    tmp_path = tmp_path_factory.mktemp("changed")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = cdk.App(outdir=str(tmp_path), context=context, analytics_reporting=False)
    stage = app_module.EndpointStage(app, "Rhi", config=config.parse(source, "example"))
    templates = {
        child.stack_name: dict(Assertions.from_stack(child).to_json())
        for child in stage.node.children
        if isinstance(child, cdk.Stack)
    }
    _, function = scaler_function(templates["Rhi-Pools"])
    return parse_settings(resolve(function["Environment"]["Variables"][SETTINGS_VARIABLE], templates), "eu-west-1")


def test_the_settings_read_whatever_a_pool_s_prices(changed: Any) -> None:
    # No rule holds a pool's prices to a limit, in the deployment or in the scaler: the scaler
    # reads the pool the deployment accepted.
    (mock_pool,) = [pool for pool in changed.pools if pool.config.name == "mock"]
    assert set(mock_pool.config.prices.values()) == {6.0}


def test_the_settings_hold_the_configuration_s_longest_session(changed: Any) -> None:
    assert changed.session_max_hours == 6
