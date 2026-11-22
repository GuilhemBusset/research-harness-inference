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
next one no longer declares, or declares with another value, must have its importer deploy first in
the next one, its import gone (`deleted_exports_in_use`), whether its resource goes or only its last
importer does.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
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
    # default security group of every rule. Its function stays as `KEPT_FUNCTIONS` allows.
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


# What an import's name holds where the template does not say: a parameter, an attribute, a
# pseudo parameter other than the stack's name. It may be any text, the empty text included.
_ANY = ".*"


def _name_pattern(node: Any, stack: str) -> str:
    """The names an import's argument can resolve to in `stack`, as a regular expression: a literal,
    or a `Fn::Sub` or `Fn::Join` of what this resolves, and any other part matching any text."""
    if isinstance(node, str):
        return re.escape(node)
    if not isinstance(node, dict) or len(node) != 1:
        return _ANY
    [(function, argument)] = node.items()
    if function == "Fn::Join" and isinstance(argument, list) and len(argument) == 2:
        delimiter, parts = argument
        if isinstance(delimiter, str) and isinstance(parts, list):
            return re.escape(delimiter).join(_name_pattern(part, stack) for part in parts)
    if function == "Fn::Sub":
        template, variables = (argument, {}) if isinstance(argument, str) else (argument + [{}])[:2]
        if isinstance(template, str) and isinstance(variables, dict):
            variables = {"AWS::StackName": stack} | variables
            pattern, position = "", 0
            for match in re.finditer(r"\$\{(!?)([^}]*)\}", template):
                pattern += re.escape(template[position : match.start()])
                if match[1]:
                    pattern += re.escape("${" + match[2] + "}")
                elif match[2] in variables:
                    pattern += _name_pattern(variables[match[2]], stack)
                else:
                    pattern += _ANY
                position = match.end()
            return pattern + re.escape(template[position:])
    return _ANY


def _imports(node: Any, stack: str) -> Iterator[str]:
    """The name of every export `stack`'s template node imports, each as `_name_pattern` gives it."""
    if isinstance(node, dict):
        if "Fn::ImportValue" in node:
            yield _name_pattern(node["Fn::ImportValue"], stack)
        for value in node.values():
            yield from _imports(value, stack)
    elif isinstance(node, list):
        for value in node:
            yield from _imports(value, stack)


def exports(stage: Stage) -> dict[str, str]:
    """Every export the stage declares, by its name, with the stack that declares it."""
    return {name: stack for name, (stack, _) in _exported_values(stage).items()}


def _exported_values(stage: Stage) -> dict[str, tuple[str, Any]]:
    """Every export the stage declares, by its name, with the stack that declares it and its value."""
    return {
        output["Export"]["Name"]: (stack, output.get("Value"))
        for stack, template in stage.templates.items()
        for output in template.get("Outputs", {}).values()
        if "Export" in output
    }


def deleted_exports_in_use(before: Stage, after: Stage) -> list[tuple[str, str]]:
    """Every export of `before` a stack of `before` imports that `after` no longer declares, or
    declares with another value, unless that stack deploys before the export's stack in `after` and
    no longer imports it there, as `(importer, export name)`: deploying `after` over `before` would
    delete or change an export in use, which CloudFormation refuses. An import whose name the
    template does not fully say counts for every export its name may resolve to."""
    values_before, values_after = _exported_values(before), _exported_values(after)

    def imported(stage: Stage, importer: str, name: str) -> bool:
        template = stage.templates.get(importer, {})
        return any(re.fullmatch(pattern, name) for pattern in set(_imports(template, importer)))

    return sorted(
        {
            (importer, name)
            for importer in before.templates
            for name, (exporter, _) in values_before.items()
            if values_after.get(name) != values_before[name]
            and imported(before, importer, name)
            and (importer not in after.after.get(exporter, set()) or imported(after, importer, name))
        }
    )


def resources(stage: Stage) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for stack, template in stage.templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            yield stack, logical_id, resource


def kept_functions(stage: Stage) -> set[tuple[str, str]]:
    """The only functions a parked stage may hold, by stack and logical ID: the handlers of the
    custom resources `MAY_STAY` lists, each the function its custom resource's service token names.
    A function that serves, such as the issuer or the scaler, is no one's handler."""
    return {
        (stack, resource["Properties"]["ServiceToken"]["Fn::GetAtt"][0])
        for stack, _, resource in resources(stage)
        if resource["Type"].startswith("Custom::")
        and resource["Type"] in MAY_STAY
        and isinstance(resource.get("Properties", {}).get("ServiceToken"), dict)
        and "Fn::GetAtt" in resource["Properties"]["ServiceToken"]
    }


def fixed_log_groups(stage: Stage) -> dict[str, tuple[str, str]]:
    """Every log group of a fixed name, by its name, with its stack and its logical ID."""
    return {
        resource["Properties"]["LogGroupName"]: (stack, logical_id)
        for stack, logical_id, resource in resources(stage)
        if resource["Type"] == "AWS::Logs::LogGroup"
        and isinstance(resource.get("Properties", {}).get("LogGroupName"), str)
    }


def synthesize(
    app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory, configuration: config.Config
) -> dict[bool, Stage]:
    """The stage of `configuration` unparked and parked, by `parked`."""
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    built = {}
    for parked in (False, True):
        outdir = tmp_path_factory.mktemp("cdk.out")
        app = app_module.build(dataclasses.replace(configuration, parked=parked), outdir=str(outdir), context=context)
        built[parked] = read_stage(app.synth())
    return built


@pytest.fixture(scope="module")
def stages(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[bool, Stage]:
    """The example's stage unparked and parked, by `parked`."""
    example = dataclasses.replace(config.load({"RHI_CONFIG": "example"}), cost_anomaly_detection=True)
    return synthesize(app_module, tmp_path_factory, example)


def unexpected(stage: Stage) -> list[tuple[str, str]]:
    """Every resource of `stage` a parked stage may not hold, as `(stack, logical ID)`: a type
    outside `MAY_STAY`, or a function `kept_functions` does not allow."""
    handlers = kept_functions(stage)
    return sorted(
        (stack, logical_id)
        for stack, logical_id, resource in resources(stage)
        if resource["Type"] not in MAY_STAY
        and not (resource["Type"] == "AWS::Lambda::Function" and (stack, logical_id) in handlers)
    )


def test_parked_holds_only_what_may_stay(stages: dict[bool, Stage]) -> None:
    from rhi.pools import scaler  # The CDK is loaded by the fixture (`conftest.app_module`).

    parked = stages[True]
    assert unexpected(parked) == []
    held = {resource["Type"] for _, _, resource in resources(parked)}
    assert [kind for kind in REMOVED if kind in held] == []
    # No scaler function: none is given the scaler's settings.
    assert [
        logical_id
        for _, logical_id, resource in resources(parked)
        if scaler.SETTINGS_VARIABLE in resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
    ] == []


def test_a_function_left_serving_may_not_stay(stages: dict[bool, Stage]) -> None:
    """Only a kept custom resource's handler is allowed: a parked stage that kept the issuer's
    function, or the scaler's, fails."""
    parked = stages[True]
    serving = [
        (stack, logical_id, resource)
        for stack, logical_id, resource in resources(stages[False])
        if resource["Type"] == "AWS::Lambda::Function" and logical_id.startswith(("Issuer", "Scaler"))
    ]
    assert {stack for stack, _, _ in serving} == {"Rhi-Access", "Rhi-Pools"}
    for stack, logical_id, resource in serving:
        template = parked.templates[stack]
        leftover = template | {"Resources": template.get("Resources", {}) | {logical_id: resource}}
        assert unexpected(dataclasses.replace(parked, templates=parked.templates | {stack: leftover})) == [
            (stack, logical_id)
        ]


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


def two_stacks(
    *,
    parked: bool,
    consumer_first: bool = False,
    still_imports: str = "",
    substituted: str = "",
    rewritten: str = "",
) -> Stage:
    """A stage of two stacks. Unparked, `Consumer` imports the name of a bucket of `Producer`, by a
    reference or, given `substituted`, by a `Fn::Sub` of that text that resolves to the export's
    name; parked, the bucket is gone, `Consumer` deploys after `Producer` or before it,
    `still_imports` names an export it keeps importing, and `rewritten` names an export `Producer`
    keeps declaring, its value now `none`."""
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
        if still_imports:
            cdk.CfnOutput(consumer, "Imported", value=cdk.Fn.import_value(still_imports))
        if rewritten:
            cdk.CfnOutput(producer, "Exported", value="none", export_name=rewritten)
    else:
        bucket = cdk.aws_s3.Bucket(producer, "Bucket")
        if substituted:
            cdk.CfnOutput(producer, "Exported", value=bucket.bucket_name, export_name="Producer-Bucket")
            consumer.add_dependency(producer)
            # A parameter of the importer's own, which may resolve to the empty text.
            cdk.CfnParameter(consumer, "Suffix", type="String", default="")
            imported = cdk.Fn.import_value(cdk.Fn.sub(substituted, {"Prefix": "Producer"}))
            cdk.CfnOutput(consumer, "Imported", value=imported)
        else:
            cdk.CfnOutput(consumer, "Imported", value=bucket.bucket_name)
    return read_stage(app.synth())


def test_no_export_in_use_is_deleted(stages: dict[bool, Stage]) -> None:
    before = two_stacks(parked=False)
    [name] = exports(before)
    assert deleted_exports_in_use(before, two_stacks(parked=True)) == [("Consumer", name)]
    assert deleted_exports_in_use(before, two_stacks(parked=True, consumer_first=True)) == []
    # Deploying first is not enough: the importer must also drop its import.
    retained = two_stacks(parked=True, consumer_first=True, still_imports=name)
    assert deleted_exports_in_use(before, retained) == [("Consumer", name)]
    # Nor may the export stay with another value while the importer still deploys after it.
    assert deleted_exports_in_use(before, two_stacks(parked=True, rewritten=name)) == [("Consumer", name)]
    assert deleted_exports_in_use(before, two_stacks(parked=True, consumer_first=True, rewritten=name)) == []
    # An import whose name a substitution gives, a parameter possibly giving the empty text.
    for template in ("${Prefix}-Bucket", "Producer-Bucket${Suffix}"):
        substituted = two_stacks(parked=False, substituted=template)
        assert deleted_exports_in_use(substituted, two_stacks(parked=True)) == [("Consumer", "Producer-Bucket")]
        assert deleted_exports_in_use(substituted, two_stacks(parked=True, consumer_first=True)) == []
    # The stage itself, parking and unparking.
    assert deleted_exports_in_use(stages[False], stages[True]) == []
    assert deleted_exports_in_use(stages[True], stages[False]) == []


def test_parking_with_no_pool_deletes_no_export_in_use(
    app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory
) -> None:
    raw = tomllib.loads((REPO / config.EXAMPLE_FILE).read_text(encoding="utf-8"))
    raw["pools"] = {}
    raw.pop("default_pool", None)
    built = synthesize(app_module, tmp_path_factory, config.parse(raw, "no pools"))
    assert deleted_exports_in_use(built[False], built[True]) == []
    assert deleted_exports_in_use(built[True], built[False]) == []
