"""The stage's retention aspect (`rhi.retention`, system design section 6.6) on stacks built here:
what it sets is what the template holds, whatever a construct wrote before it.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType


def stage_stack(build: Callable[[Any], None]) -> dict[str, Any]:
    """The template of a stack of a stage with the aspect, built by `build(stack)`."""
    import aws_cdk as cdk
    from aws_cdk import assertions

    from rhi.retention import apply

    stage = cdk.Stage(cdk.App(analytics_reporting=False), "Endpoint")
    apply(stage)
    stack = cdk.Stack(stage, "Built")
    build(stack)
    return dict(assertions.Template.from_stack(stack).to_json())


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def test_a_function_logs_to_a_log_group_of_its_stack_not_an_imported_one(app_module: ModuleType) -> None:
    del app_module  # started jsii
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_logs as logs

    def function(stack: Any, construct_id: str, log_group: Any) -> None:
        lambda_.Function(
            stack,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(event, context): pass"),
            log_group=log_group,
        )

    def build(stack: Any) -> None:
        function(stack, "Imported", logs.LogGroup.from_log_group_name(stack, "Shared", "/shared/logs"))
        function(stack, "Own", logs.LogGroup(stack, "OwnLogs"))
        overridden: Any = lambda_.Function(
            stack,
            "Overridden",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(event, context): pass"),
        )
        cfn: Any = overridden.node.default_child
        cfn.add_property_override("LoggingConfig.LogGroup", "/shared/logs")

    template = stage_stack(build)
    groups = of_type(template, "AWS::Logs::LogGroup")
    named = {name[:-8]: p["LoggingConfig"]["LogGroup"] for name, p in of_type(template, "AWS::Lambda::Function").items()}
    assert all(target.keys() == {"Ref"} and target["Ref"] in groups for target in named.values()), named
    # The function that names a log group of its stack keeps it.
    assert groups[named["Own"]["Ref"]] is groups[next(name for name in groups if name.startswith("OwnLogs"))]
    assert len({target["Ref"] for target in named.values()}) == 3


def test_every_log_group_keeps_90_days_whatever_a_construct_overrode(app_module: ModuleType) -> None:
    del app_module  # started jsii
    from aws_cdk import aws_logs as logs

    def build(stack: Any) -> None:
        logs.CfnLogGroup(stack, "Overridden").add_property_override("RetentionInDays", 365)
        logs.CfnLogGroup(stack, "Deleted", retention_in_days=30).add_property_deletion_override("RetentionInDays")
        logs.LogGroup(stack, "Forever", retention=logs.RetentionDays.INFINITE)

    groups = of_type(stage_stack(build), "AWS::Logs::LogGroup")
    assert len(groups) == 3
    assert {name: p.get("RetentionInDays") for name, p in groups.items()} == dict.fromkeys(groups, 90)


def test_a_function_names_its_log_group_by_the_name_the_template_gives_it(app_module: ModuleType) -> None:
    del app_module  # started jsii
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_logs as logs

    def build(stack: Any) -> None:
        # The function names the group by the name its construct gave; an override renames it.
        group = logs.CfnLogGroup(stack, "Logs", log_group_name="/rhi/jobs")
        group.add_property_override("LogGroupName", "/rhi/workers")
        lambda_.CfnFunction(
            stack,
            "Worker",
            role=stack.format_arn(service="iam", resource="role", resource_name="worker"),
            runtime="python3.13",
            handler="index.handler",
            code=lambda_.CfnFunction.CodeProperty(zip_file="def handler(event, context): pass"),
            logging_config=lambda_.CfnFunction.LoggingConfigProperty(log_group="/rhi/jobs"),
        )

    template = stage_stack(build)
    (function,) = of_type(template, "AWS::Lambda::Function").values()
    (group,) = of_type(template, "AWS::Logs::LogGroup")
    assert function["LoggingConfig"]["LogGroup"] == {"Ref": group}
