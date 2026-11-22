"""The parked stage: the whole endpoint switched off, leaving data and the pipeline (system design,
sections 5.6, 8.5 and 11.2).

The stage is synthesized both ways from the example configuration, Cost Anomaly Detection on so
that its monitor is seen, as `app.py` builds it: cdk-nag runs inside each synthesis, so a finding
the parked stage brings fails here unless its module's file acknowledges it. The parked synthesis
may hold only the types `MAY_STAY` lists: a construct that forgets the flag fails it. What stays
keeps its stack and its logical ID, and the state recorded in the policy tests'
`stateful_logical_ids.json` keeps every protection it has unparked.

CloudFormation refuses to delete an export that a deployed stack still imports, and the pipeline
deploys a stack after the stacks it depends on. So an export that one synthesis imports and the
next one no longer declares must have its importer deploy first in the next one
(`deleted_exports_in_use`), whether its resource goes or only its last importer does.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    import aws_cdk as cdk

REPO = Path(__file__).resolve().parents[2]
RECORD = REPO / "tests" / "policy" / "stateful_logical_ids.json"

# What a parked stage may hold, by CloudFormation type. Everything else costs money while idle, or
# serves, or watches what serves.
MAY_STAY = {
    # State, and what guards it: the tables, the buckets, the workspace, the user pool and the
    # pools' secrets.
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::S3::BucketPolicy",
    "AWS::APS::Workspace",
    "AWS::Cognito::UserPool",
    "AWS::Cognito::UserPoolDomain",
    "AWS::Cognito::UserPoolClient",
    "AWS::Cognito::UserPoolUser",
    "AWS::SecretsManager::Secret",
    # The alerts topic and its key, the budget and Cost Anomaly Detection: the budget still counts.
    "AWS::KMS::Key",
    "AWS::SNS::Topic",
    "AWS::SNS::TopicPolicy",
    "AWS::SNS::Subscription",
    "AWS::Budgets::Budget",
    "AWS::CE::AnomalyMonitor",
    "AWS::CE::AnomalySubscription",
    # Log groups: those of a fixed name must stay.
    "AWS::Logs::LogGroup",
    # The VPC, which costs nothing without its NAT gateway, and the custom resource that strips its
    # default security group of every rule, with its function.
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::RouteTable",
    "AWS::EC2::SubnetRouteTableAssociation",
    "AWS::EC2::Route",
    "AWS::EC2::InternetGateway",
    "AWS::EC2::VPCGatewayAttachment",
    "AWS::EC2::VPCEndpoint",
    "AWS::EC2::SecurityGroup",
    "AWS::EC2::SecurityGroupIngress",
    "Custom::VpcRestrictDefaultSG",
    "AWS::Lambda::Function",
    # Roles and policies, which cost nothing: the pools' instance roles, the workspace's policies.
    "AWS::IAM::Role",
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
}

# What parking removes, by type: the load balancers, their listeners, rules and target groups, the
# alias record and the web ACL, the NAT gateway, the Fargate services, the groups and warm pools,
# the scaler's schedule and the alarms.
REMOVED = (
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS::ElasticLoadBalancingV2::Listener",
    "AWS::ElasticLoadBalancingV2::ListenerRule",
    "AWS::ElasticLoadBalancingV2::TargetGroup",
    "AWS::Route53::RecordSet",
    "AWS::WAFv2::WebACL",
    "AWS::EC2::NatGateway",
    "AWS::ECS::Service",
    "AWS::AutoScaling::AutoScalingGroup",
    "AWS::AutoScaling::WarmPool",
    "AWS::Events::Rule",
    "AWS::CloudWatch::Alarm",
)

# What must stay under its stack and logical ID, by type, beside every log group of a fixed name:
# the state, the alerts topic and its key, the budget, Cost Anomaly Detection, and the VPC with its
# subnets, their route tables, its security groups and its S3 endpoint.
KEPT = {
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::RouteTable",
    "AWS::EC2::SecurityGroup",
    "AWS::EC2::VPCEndpoint",
    "AWS::DynamoDB::Table",
    "AWS::S3::Bucket",
    "AWS::APS::Workspace",
    "AWS::Cognito::UserPool",
    "AWS::SecretsManager::Secret",
    "AWS::KMS::Key",
    "AWS::SNS::Topic",
    "AWS::Budgets::Budget",
    "AWS::CE::AnomalyMonitor",
    "AWS::CE::AnomalySubscription",
}


def _enabled(value: Any) -> bool:
    return value is True or value == "true"


# The protections a stateful resource may have, each by whether the resource has it.
PROTECTIONS = {
    "retain on deletion": lambda resource: resource.get("DeletionPolicy") == "Retain",
    "retain on replacement": lambda resource: resource.get("UpdateReplacePolicy") == "Retain",
    "deletion protection": lambda resource: (
        _enabled(resource.get("Properties", {}).get("DeletionProtectionEnabled"))
        or resource.get("Properties", {}).get("DeletionProtection") == "ACTIVE"
    ),
    "point-in-time recovery": lambda resource: _enabled(
        (resource.get("Properties", {}).get("PointInTimeRecoverySpecification") or {}).get(
            "PointInTimeRecoveryEnabled"
        )
    ),
    "versioning": lambda resource: (
        (resource.get("Properties", {}).get("VersioningConfiguration") or {}).get("Status") == "Enabled"
    ),
}


@dataclass(frozen=True)
class Stage:
    """A stage's synthesis: each stack's template, and the stacks it deploys after, directly or not,
    each by stack name."""

    templates: dict[str, dict[str, Any]]
    after: dict[str, set[str]]


def read_stage(assembly: cdk.cx_api.CloudAssembly) -> Stage:
    """The one stage of an app's cloud assembly."""
    [nested] = assembly.nested_assemblies
    stacks = nested.nested_assembly.stacks_recursively
    names = {stack.id: stack.stack_name for stack in stacks}
    direct = {
        stack.stack_name: {names[artifact.id] for artifact in stack.dependencies if artifact.id in names}
        for stack in stacks
    }

    def closure(name: str) -> set[str]:
        found: set[str] = set()
        pending = list(direct[name])
        while pending:
            other = pending.pop()
            if other not in found:
                found.add(other)
                pending.extend(direct[other])
        return found

    return Stage({stack.stack_name: stack.template for stack in stacks}, {name: closure(name) for name in direct})


def _imports(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if isinstance(node.get("Fn::ImportValue"), str):
            yield node["Fn::ImportValue"]
        for value in node.values():
            yield from _imports(value)
    elif isinstance(node, list):
        for value in node:
            yield from _imports(value)


def exports(stage: Stage) -> dict[str, str]:
    """Every export the stage declares, by its name, with the stack that declares it."""
    return {
        output["Export"]["Name"]: stack
        for stack, template in stage.templates.items()
        for output in template.get("Outputs", {}).values()
        if "Export" in output
    }


def deleted_exports_in_use(before: Stage, after: Stage) -> list[tuple[str, str]]:
    """Every export a stack of `before` imports that `after` no longer declares, while that stack
    does not deploy before the export's stack in `after`, as `(importer, export name)`: deploying
    `after` over `before` would delete an export in use."""
    declared_before, declared_after = exports(before), exports(after)
    return sorted(
        (importer, name)
        for importer, template in before.templates.items()
        for name in set(_imports(template))
        if name not in declared_after
        and importer not in after.after.get(declared_before.get(name, ""), set())
    )


def resources(stage: Stage) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for stack, template in stage.templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            yield stack, logical_id, resource


def fixed_log_groups(stage: Stage) -> dict[str, tuple[str, str]]:
    """Every log group of a fixed name, by its name, with its stack and its logical ID."""
    return {
        resource["Properties"]["LogGroupName"]: (stack, logical_id)
        for stack, logical_id, resource in resources(stage)
        if resource["Type"] == "AWS::Logs::LogGroup"
        and isinstance(resource.get("Properties", {}).get("LogGroupName"), str)
    }


@pytest.fixture(scope="module")
def stages(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[bool, Stage]:
    """The stage unparked and parked, by `parked`."""
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    example = dataclasses.replace(config.load({"RHI_CONFIG": "example"}), cost_anomaly_detection=True)
    built = {}
    for parked in (False, True):
        outdir = tmp_path_factory.mktemp("cdk.out")
        app = app_module.build(dataclasses.replace(example, parked=parked), outdir=str(outdir), context=context)
        built[parked] = read_stage(app.synth())
    return built


def test_parked_holds_only_what_may_stay(stages: dict[bool, Stage]) -> None:
    from rhi.pools import scaler  # The CDK is loaded by the fixture (`conftest.app_module`).

    parked = stages[True]
    assert sorted({(stack, resource["Type"]) for stack, _, resource in resources(parked)} - {
        (stack, kind) for stack in parked.templates for kind in MAY_STAY
    }) == []
    held = {resource["Type"] for _, _, resource in resources(parked)}
    assert [kind for kind in REMOVED if kind in held] == []
    # No scaler function: none is given the scaler's settings.
    assert [
        logical_id
        for _, logical_id, resource in resources(parked)
        if scaler.SETTINGS_VARIABLE in resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
    ] == []


def test_leaving_parked_out_builds_the_stage_as_before(stages: dict[bool, Stage]) -> None:
    assert config.load({"RHI_CONFIG": "example"}).parked is False
    held = [resource["Type"] for _, _, resource in resources(stages[False])]
    assert all(kind in held for kind in REMOVED)
    assert held.count("AWS::ElasticLoadBalancingV2::LoadBalancer") == 2


def test_parked_keeps_what_must_stay_under_its_logical_id(stages: dict[bool, Stage]) -> None:
    kept = {(stack, logical_id) for stack, logical_id, _ in resources(stages[True])}
    must_stay = {
        (stack, logical_id, resource["Type"])
        for stack, logical_id, resource in resources(stages[False])
        if resource["Type"] in KEPT
    }
    assert {kind for _, _, kind in must_stay} >= KEPT - {"AWS::Budgets::Budget"}
    assert sorted((stack, logical_id) for stack, logical_id, _ in must_stay if (stack, logical_id) not in kept) == []


def test_parked_keeps_every_log_group_of_a_fixed_name(stages: dict[bool, Stage]) -> None:
    from rhi.access import issuer
    from rhi.gateway import service
    from rhi.pools import role, scaler

    unparked, parked = fixed_log_groups(stages[False]), fixed_log_groups(stages[True])
    named = {service.LOG_GROUP_NAME, scaler.LOG_GROUP_NAME, issuer.LOG_GROUP_NAME, role.log_group_name("mock")}
    assert named <= unparked.keys()
    assert {name: parked.get(name) for name in unparked} == unparked


def test_parked_keeps_the_recorded_state_and_its_protections(stages: dict[bool, Stage]) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    unparked, parked = stages[False].templates, stages[True].templates
    for stack, logical_ids in record.items():
        for logical_id in logical_ids:
            before = unparked[stack]["Resources"][logical_id]
            after = parked.get(stack, {}).get("Resources", {}).get(logical_id)
            assert after is not None, f"{stack} {logical_id} is gone"
            assert after["Type"] == before["Type"]
            lost = [name for name, has in PROTECTIONS.items() if has(before) and not has(after)]
            assert lost == [], f"{stack} {logical_id}"


def two_stacks(*, parked: bool, consumer_first: bool = False) -> Stage:
    """A stage of two stacks. Unparked, `Consumer` imports the name of a bucket of `Producer`;
    parked, the bucket is gone, and `Consumer` deploys after `Producer`, or before it."""
    import aws_cdk as cdk

    app = cdk.App(analytics_reporting=False)
    stage = cdk.Stage(app, "Pair")
    producer = cdk.Stack(stage, "Producer", stack_name="Producer")
    consumer = cdk.Stack(stage, "Consumer", stack_name="Consumer")
    cdk.CfnWaitConditionHandle(consumer, "Kept")
    if parked:
        cdk.CfnWaitConditionHandle(producer, "Kept")
        if consumer_first:
            producer.add_dependency(consumer)
        else:
            consumer.add_dependency(producer)
    else:
        bucket = cdk.aws_s3.Bucket(producer, "Bucket")
        cdk.CfnOutput(consumer, "Imported", value=bucket.bucket_name)
    return read_stage(app.synth())


def test_no_export_in_use_is_deleted(stages: dict[bool, Stage]) -> None:
    before = two_stacks(parked=False)
    [name] = exports(before)
    assert deleted_exports_in_use(before, two_stacks(parked=True)) == [("Consumer", name)]
    assert deleted_exports_in_use(before, two_stacks(parked=True, consumer_first=True)) == []
    # The stage itself, parking and unparking.
    assert deleted_exports_in_use(stages[False], stages[True]) == []
    assert deleted_exports_in_use(stages[True], stages[False]) == []
