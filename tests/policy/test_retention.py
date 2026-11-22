"""Nothing of the stage is kept longer than 90 days, and nothing per student goes to CloudWatch
metrics (system design, section 6.6; evidence E27).

- Every log group has a retention, of at most 90 days: one with none is kept forever.
- Every function names, in its logging configuration, a log group of its own stack: Lambda creates
  one for a function that names none, at run time, outside every template and with no retention.
- The metrics workspace keeps at most 90 days; with no retention set, it keeps the service's
  default, which is longer.
- No CloudWatch alarm, metric or metric filter has a dimension that identifies a student: alias,
  student or github_id, whatever its case. CloudWatch metrics cannot be deleted for 15 months; the
  per-student series live in Prometheus only.

These hold for the stage's templates, those of its nested assembly. The pipeline's own stack is not
in the stage: its logs hold no student data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

MAX_DAYS = 90
STUDENT_DIMENSIONS = {"alias", "student", "github_id"}
LOG_GROUP = "AWS::Logs::LogGroup"
FUNCTION = "AWS::Lambda::Function"
WORKSPACE = "AWS::APS::Workspace"
METRIC_FILTER = "AWS::Logs::MetricFilter"


def stage_templates(templates: Mapping[str, Any]) -> dict[str, Any]:
    """The templates of the stage: those of the nested assembly, below the app's own."""
    return {path: template for path, template in templates.items() if "/" in path}


def of_type(template: Mapping[str, Any], kind: str) -> dict[str, Mapping[str, Any]]:
    """The properties of every resource of the template of that type, by logical ID."""
    resources = template.get("Resources", {})
    return {name: r.get("Properties", {}) for name, r in resources.items() if r.get("Type") == kind}


def log_groups_kept_too_long(template: Mapping[str, Any]) -> list[str]:
    """Every log group with no retention, or one longer than `MAX_DAYS`."""
    return [
        name
        for name, properties in of_type(template, LOG_GROUP).items()
        if not isinstance(days := properties.get("RetentionInDays"), int) or days > MAX_DAYS
    ]


def functions_without_their_log_group(template: Mapping[str, Any]) -> list[str]:
    """Every function whose logging configuration names no log group of its template, by a
    reference or by the name the template gives it."""
    groups = of_type(template, LOG_GROUP)
    names = {name for properties in groups.values() if isinstance(name := properties.get("LogGroupName"), str)}
    found = []
    for name, properties in of_type(template, FUNCTION).items():
        named = properties.get("LoggingConfig", {}).get("LogGroup")
        by_reference = isinstance(named, dict) and named.keys() == {"Ref"} and named["Ref"] in groups
        if not by_reference and not (isinstance(named, str) and named in names):
            found.append(name)
    return found


def workspaces_kept_too_long(template: Mapping[str, Any]) -> list[str]:
    """Every metrics workspace with no retention, or one longer than `MAX_DAYS`."""
    return [
        name
        for name, properties in of_type(template, WORKSPACE).items()
        if not isinstance(days := properties.get("WorkspaceConfiguration", {}).get("RetentionPeriodInDays"), int)
        or days > MAX_DAYS
    ]


def student_dimensions(template: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every dimension that identifies a student, with the resource that has it, in a CloudWatch
    resource (an alarm, its metrics, an anomaly detector) or in a metric filter's metrics."""
    found: list[tuple[str, str]] = []

    def walk(node: object, resource: str) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, resource)
        if not isinstance(node, dict):
            return
        for dimension in node.get("Dimensions") or []:
            # A CloudWatch dimension has a `Name`; a metric filter's, a `Key`.
            name = dimension.get("Name", dimension.get("Key")) if isinstance(dimension, dict) else None
            if isinstance(name, str) and name.lower() in STUDENT_DIMENSIONS:
                found.append((resource, name))
        for value in node.values():
            walk(value, resource)

    for name, resource in template.get("Resources", {}).items():
        kind = resource.get("Type", "")
        if kind.startswith("AWS::CloudWatch::") or kind == METRIC_FILTER:
            walk(resource.get("Properties", {}), name)
    return found


def findings(templates: Mapping[str, Any], check: Callable[[Mapping[str, Any]], list[Any]]) -> dict[str, list[Any]]:
    return {path: check(template) for path, template in templates.items() if check(template)}


def test_every_log_group_of_the_stage_expires_within_90_days(templates: dict[str, Any]) -> None:
    stage = stage_templates(templates)
    assert any(of_type(template, LOG_GROUP) for template in stage.values())
    assert findings(stage, log_groups_kept_too_long) == {}


def test_every_function_of_the_stage_logs_to_a_log_group_of_its_stack(templates: dict[str, Any]) -> None:
    stage = stage_templates(templates)
    # The CDK's own functions among them: that of the AWS SDK custom resources, and the VPC's.
    assert sum(len(of_type(template, FUNCTION)) for template in stage.values()) > 2
    assert findings(stage, functions_without_their_log_group) == {}


def test_the_workspace_keeps_at_most_90_days(templates: dict[str, Any]) -> None:
    stage = stage_templates(templates)
    assert any(of_type(template, WORKSPACE) for template in stage.values())
    assert findings(stage, workspaces_kept_too_long) == {}


def test_no_cloudwatch_dimension_of_the_stage_identifies_a_student(templates: dict[str, Any]) -> None:
    assert findings(stage_templates(templates), student_dimensions) == {}


def synthesize(cdk: ModuleType, build: Callable[[Any], None]) -> Mapping[str, Any]:
    """The template of a stack built here by `build(stack)`."""
    from aws_cdk import assertions

    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Built")
    build(stack)
    return assertions.Template.from_stack(stack).to_json()


def constructs_of(logical_ids: list[str]) -> list[str]:
    """The construct IDs, in a stack built here, of these logical IDs: each less its hash."""
    return sorted(logical_id[:-8] for logical_id in logical_ids)


def test_detects_a_log_group_kept_forever(cdk: ModuleType) -> None:
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_logs as logs

    def function(stack: Any, construct_id: str, log_group: Any = None) -> None:
        lambda_.Function(
            stack,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(event, context): pass"),
            log_group=log_group,
        )

    def build(stack: Any) -> None:
        logs.LogGroup(stack, "Forever", retention=logs.RetentionDays.INFINITE)
        logs.LogGroup(stack, "TwoYears", retention=logs.RetentionDays.TWO_YEARS)
        kept = logs.LogGroup(stack, "Kept", retention=logs.RetentionDays.THREE_MONTHS)
        function(stack, "Unnamed")
        function(stack, "Named", kept)
        function(stack, "Elsewhere", logs.LogGroup.from_log_group_name(stack, "Imported", "/elsewhere"))

    template = synthesize(cdk, build)
    # A log group that never expires has no retention in its template.
    assert "RetentionInDays" not in next(p for n, p in of_type(template, LOG_GROUP).items() if n.startswith("Forever"))
    assert "LoggingConfig" not in next(p for n, p in of_type(template, FUNCTION).items() if n.startswith("Unnamed"))
    assert constructs_of(log_groups_kept_too_long(template)) == ["Forever", "TwoYears"]
    assert constructs_of(functions_without_their_log_group(template)) == ["Elsewhere", "Unnamed"]


def test_detects_a_workspace_kept_too_long(cdk: ModuleType) -> None:
    from aws_cdk import aws_aps as aps

    def build(stack: Any) -> None:
        aps.CfnWorkspace(stack, "Unset")
        for name, days in (("Long", 150), ("Kept", MAX_DAYS)):
            config = aps.CfnWorkspace.WorkspaceConfigurationProperty(retention_period_in_days=days)
            aps.CfnWorkspace(stack, name, workspace_configuration=config)

    assert sorted(workspaces_kept_too_long(synthesize(cdk, build))) == ["Long", "Unset"]


def test_detects_a_student_dimension(cdk: ModuleType) -> None:
    from aws_cdk import aws_cloudwatch as cloudwatch
    from aws_cdk import aws_logs as logs

    def alarm(stack: Any, construct_id: str, dimensions: dict[str, str]) -> None:
        metric = cloudwatch.Metric(namespace="rhi", metric_name="tokens", dimensions_map=dimensions)
        metric.create_alarm(stack, construct_id, threshold=1, evaluation_periods=1)

    def build(stack: Any) -> None:
        alarm(stack, "PerStudent", {"Alias": "a-student"})
        alarm(stack, "PerPool", {"pool": "coursework"})
        logs.MetricFilter(
            stack,
            "PerGithubId",
            log_group=logs.LogGroup(stack, "Logs", retention=logs.RetentionDays.THREE_MONTHS),
            filter_pattern=logs.FilterPattern.all_events(),
            metric_namespace="rhi",
            metric_name="requests",
            dimensions={"GitHub_ID": "$.github_id"},
        )

    found = student_dimensions(synthesize(cdk, build))
    assert sorted((resource[:-8], name) for resource, name in found) == [
        ("PerGithubId", "GitHub_ID"),
        ("PerStudent", "Alias"),
    ]
