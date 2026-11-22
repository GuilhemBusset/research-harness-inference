"""Nothing of the stage is kept longer than 90 days, and nothing per student goes to CloudWatch
metrics (system design, section 6.6; evidence E27).

- Every log group has a retention, of at most 90 days: one with none is kept forever.
- Every function names, in its logging configuration, a log group of its own stack: Lambda creates
  one for a function that names none, at run time, outside every template and with no retention.
- The metrics workspace keeps at most 90 days; with no retention set, it keeps the service's
  default, which is longer.
- Every bucket a load balancer writes its access logs to expires every object within 90 days of
  its writing: the logs hold source addresses. A bucket this check cannot find, in the load
  balancer's template or as another template's export, counts as one that never expires.
- No CloudWatch alarm, metric or metric filter has a dimension that identifies a student: alias,
  student or github_id, whatever its case. A metric is checked wherever a resource holds one: an
  alarm, a dashboard, a scaling policy that tracks it, a service that publishes its events' metrics
  by a dimension it is configured with. CloudWatch metrics cannot be deleted for 15
  months; the per-student series live in Prometheus only.

These hold for the stage's templates, those of its nested assembly. The pipeline's own stack is not
in the stage: its logs hold no student data.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

MAX_DAYS = 90
STUDENT_DIMENSIONS = {"alias", "student", "github_id"}
LOG_GROUP = "AWS::Logs::LogGroup"
FUNCTION = "AWS::Lambda::Function"
WORKSPACE = "AWS::APS::Workspace"
DASHBOARD = "AWS::CloudWatch::Dashboard"
LOAD_BALANCER = "AWS::ElasticLoadBalancingV2::LoadBalancer"
BUCKET = "AWS::S3::Bucket"
# What narrows a lifecycle rule to some of a bucket's objects.
RULE_FILTERS = ("Prefix", "TagFilters", "ObjectSizeGreaterThan", "ObjectSizeLessThan")


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


def log_buckets(templates: Mapping[str, Any]) -> dict[tuple[str, str], tuple[str, str] | None]:
    """The bucket each load balancer writes its access logs to, as `(template, logical ID)`, by
    the load balancer's: a bucket of its own template by `Ref`, or of another by the import of an
    export of that `Ref`. None for a bucket that neither names."""
    exports = {
        output["Export"]["Name"]: (path, output["Value"]["Ref"])
        for path, template in templates.items()
        for output in template.get("Outputs", {}).values()
        if isinstance(output.get("Value"), dict) and "Ref" in output["Value"] and "Export" in output
    }
    found: dict[tuple[str, str], tuple[str, str] | None] = {}
    for path, template in templates.items():
        for name, properties in of_type(template, LOAD_BALANCER).items():
            attributes = {a.get("Key"): a.get("Value") for a in properties.get("LoadBalancerAttributes") or []}
            if attributes.get("access_logs.s3.enabled") not in (True, "true"):
                continue
            bucket = attributes.get("access_logs.s3.bucket")
            if isinstance(bucket, dict) and "Ref" in bucket:
                found[path, name] = (path, bucket["Ref"])
            elif isinstance(bucket, dict) and "Fn::ImportValue" in bucket:
                found[path, name] = exports.get(bucket["Fn::ImportValue"])
            else:
                found[path, name] = None
    return found


def expires_within(bucket: Mapping[str, Any] | None, days: int) -> bool:
    """Whether an enabled rule of the bucket's lifecycle, on every object, expires each within
    `days` of its writing."""
    rules = (bucket or {}).get("Properties", {}).get("LifecycleConfiguration", {}).get("Rules", [])
    return any(
        rule.get("Status") == "Enabled"
        and not any(rule.get(key) for key in RULE_FILTERS)
        and isinstance(expiry := rule.get("ExpirationInDays"), int)
        and expiry <= days
        for rule in rules
    )


def log_buckets_kept_too_long(templates: Mapping[str, Any]) -> list[str]:
    """Every bucket a load balancer logs to, as `template:logical ID`, with no expiry or a longer
    one than `MAX_DAYS`; or the load balancer, when its bucket is not found."""
    found = set()
    for (path, balancer), bucket in log_buckets(templates).items():
        resource = templates[bucket[0]].get("Resources", {}).get(bucket[1]) if bucket else None
        if bucket is None or resource is None or resource.get("Type") != BUCKET:
            found.add(f"{path}:{balancer}: no bucket found")
        elif not expires_within(resource, MAX_DAYS):
            found.add(":".join(bucket))
    return sorted(found)


def student_dimensions(template: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every dimension that identifies a student, with the resource that has it: in a CloudWatch
    metric of any resource (an alarm's, an anomaly detector's, a scaling policy's), in a metric
    filter's metrics, in the dimensions a service is configured to publish its events' metrics by
    (an e-mail event destination's), or in a dashboard's metrics."""
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
        # A dimension a service publishes to CloudWatch for its events (an e-mail event
        # destination's) is configured by its `DimensionName`.
        name = node.get("DimensionName")
        if isinstance(name, str) and name.lower() in STUDENT_DIMENSIONS:
            found.append((resource, name))
        for value in node.values():
            walk(value, resource)

    def walk_dashboard(node: object, resource: str) -> None:
        # A dashboard's metric is an array: namespace, metric name, then each dimension's name and
        # value, and last its options.
        if isinstance(node, dict):
            for value in node.values():
                walk_dashboard(value, resource)
        if not isinstance(node, list):
            return
        if node and all(isinstance(item, str) for item in node[:2]):
            pairs = [item for item in node[2:] if not isinstance(item, dict)]
            found.extend((resource, name) for name in pairs[::2] if str(name).lower() in STUDENT_DIMENSIONS)
        for item in node:
            walk_dashboard(item, resource)

    for name, resource in template.get("Resources", {}).items():
        # Any resource can hold a CloudWatch metric: a scaling policy tracks one.
        walk(resource.get("Properties", {}), name)
        if resource.get("Type") == DASHBOARD:
            walk_dashboard(dashboard_body(resource.get("Properties", {}).get("DashboardBody")), name)
    return found


def dashboard_body(body: object) -> object:
    """A dashboard's body, as JSON, read from the string the template writes it as or joins it
    from. Each value the template joins in at deploy time stands as `TOKEN`."""
    if isinstance(body, dict) and "Fn::Join" in body:
        separator, parts = body["Fn::Join"]
        body = separator.join(part if isinstance(part, str) else "TOKEN" for part in parts)
    elif isinstance(body, dict) and "Fn::Sub" in body:
        body = body["Fn::Sub"] if isinstance(body["Fn::Sub"], str) else body["Fn::Sub"][0]
    assert isinstance(body, str), f"a dashboard body this check cannot read: {body!r}"
    return json.loads(body)


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


def test_every_access_log_bucket_of_the_stage_expires_within_90_days(templates: dict[str, Any]) -> None:
    stage = stage_templates(templates)
    # Both load balancers, the public one and the internal one, log, to the one bucket.
    buckets = log_buckets(stage)
    assert len(buckets) == 2
    assert len(set(buckets.values())) == 1
    assert log_buckets_kept_too_long(stage) == []


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


def test_detects_an_access_log_bucket_kept_too_long(cdk: ModuleType) -> None:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from aws_cdk import aws_s3 as s3

    def build(stack: Any) -> None:
        for name, days in (("NoExpiry", None), ("Year", 365), ("Kept", MAX_DAYS)):
            rules = [s3.LifecycleRule(expiration=cdk.Duration.days(days))] if days else None
            bucket = s3.Bucket(stack, name, lifecycle_rules=rules)
            attribute = elbv2.CfnLoadBalancer.LoadBalancerAttributeProperty
            elbv2.CfnLoadBalancer(
                stack,
                f"LogsTo{name}",
                load_balancer_attributes=[
                    attribute(key="access_logs.s3.enabled", value="true"),
                    attribute(key="access_logs.s3.bucket", value=bucket.bucket_name),
                ],
            )
        # A bucket no load balancer logs to is not held to it.
        s3.Bucket(stack, "Other")

    found = log_buckets_kept_too_long({"Built": synthesize(cdk, build)})
    assert [name.removeprefix("Built:")[:-8] for name in found] == ["NoExpiry", "Year"]


def test_detects_a_student_dimension(cdk: ModuleType) -> None:
    from aws_cdk import aws_applicationautoscaling as scaling
    from aws_cdk import aws_cloudwatch as cloudwatch
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_ses as ses
    from constructs import Construct

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
        dashboard = cloudwatch.Dashboard(stack, "Dashboard")
        for dimensions in ({"pool": "coursework"}, {"pool": "coursework", "student": "a-student"}):
            metric = cloudwatch.Metric(namespace="rhi", metric_name="requests", dimensions_map=dimensions)
            dashboard.add_widgets(cloudwatch.GraphWidget(left=[metric]))
        target = scaling.ScalableTarget(
            stack,
            "Target",
            service_namespace=scaling.ServiceNamespace.ECS,
            scalable_dimension="ecs:service:DesiredCount",
            resource_id="service/cluster/workers",
            min_capacity=1,
            max_capacity=2,
        )
        metric = cloudwatch.Metric(namespace="rhi", metric_name="requests", dimensions_map={"STUDENT": "a-student"})
        target.scale_to_track_metric("PerStudentTracking", custom_metric=metric, target_value=10)
        destination = ses.CfnConfigurationSetEventDestination
        dimension = destination.DimensionConfigurationProperty(
            dimension_name="Alias", dimension_value_source="messageTag", default_dimension_value="unknown"
        )
        # Within a construct, so that its logical ID ends in a hash like those of the others.
        destination(
            Construct(stack, "PerStudentEvents"),
            "Destination",
            configuration_set_name="events",
            event_destination=destination.EventDestinationProperty(
                matching_event_types=["send"],
                cloud_watch_destination=destination.CloudWatchDestinationProperty(dimension_configurations=[dimension]),
            ),
        )

    found = student_dimensions(synthesize(cdk, build))
    assert sorted((resource[:-8], name) for resource, name in found) == [
        ("Dashboard", "student"),
        ("PerGithubId", "GitHub_ID"),
        ("PerStudent", "Alias"),
        ("PerStudentEventsDestination", "Alias"),
        ("TargetPerStudentTracking", "STUDENT"),
    ]
