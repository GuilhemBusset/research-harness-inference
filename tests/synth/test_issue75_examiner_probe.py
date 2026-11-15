"""Independent synthesis witnesses for issue 75, including managed-policy permissions."""

from __future__ import annotations

import inspect
import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi.config import load

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src/rhi"
ACKS = REPO / "tests/policy/acknowledged"


def context() -> dict[str, Any]:
    return json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {
        "aws:cdk:bundling-stacks": [],
    }


def owner(construct: Any) -> str:
    for enclosing in reversed(construct.node.scopes):
        try:
            source = Path(inspect.getfile(type(enclosing))).resolve()
        except (TypeError, OSError):
            continue
        if source.is_relative_to(SOURCE):
            return "_".join(source.relative_to(SOURCE).with_suffix("").parts)
    raise AssertionError(f"No source module owns {construct.node.path}")


def acknowledgements(module: str) -> dict[str, Any]:
    path = ACKS / f"{module}.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.fixture
def examined(app_module: ModuleType, tmp_path: Path) -> tuple[Any, Any, dict[str, Any], Path]:
    from rhi.network.stack import NetworkStack

    app = app_module.build(load({"RHI_CONFIG": "example"}), outdir=str(tmp_path), context=context())
    stage = app.node.find_child("Rhi")
    stack = stage.network
    assert isinstance(stack, NetworkStack)
    app.synth()
    template = json.loads((Path(stage.outdir) / stack.template_file).read_text(encoding="utf-8"))
    return app, stack, template, tmp_path


def resources(template: dict[str, Any], kind: str) -> dict[str, Any]:
    return {name: value["Properties"] for name, value in template["Resources"].items() if value["Type"] == kind}


def test_three_deployment_time_zones_and_no_lookup(examined: tuple[Any, Any, dict[str, Any], Path]) -> None:
    import aws_cdk as cdk

    _, stack, template, outdir = examined
    assert cdk.Token.is_unresolved(stack.account)
    assert cdk.Token.is_unresolved(stack.region)
    subnets = resources(template, "AWS::EC2::Subnet")
    assert len(subnets) == 6
    for index in range(3):
        selected = {"Fn::Select": [index, {"Fn::GetAZs": ""}]}
        assert sum(subnet["AvailabilityZone"] == selected for subnet in subnets.values()) == 2
    for path in outdir.rglob("manifest.json"):
        assert not json.loads(path.read_text(encoding="utf-8")).get("missing")
    assert not list(outdir.rglob("cdk.context.json"))
    assert not (REPO / "cdk.context.json").exists()


def test_public_and_private_routes_and_single_nat(examined: tuple[Any, Any, dict[str, Any], Path]) -> None:
    _, _, template, _ = examined
    subnets = resources(template, "AWS::EC2::Subnet")
    associations = resources(template, "AWS::EC2::SubnetRouteTableAssociation").values()
    routes = resources(template, "AWS::EC2::Route").values()
    (nat_name, nat), = resources(template, "AWS::EC2::NatGateway").items()
    (internet_gateway,) = resources(template, "AWS::EC2::InternetGateway")
    counts = {"Public": 0, "Private": 0}
    for name, subnet in subnets.items():
        tags = {tag["Key"]: tag["Value"] for tag in subnet["Tags"]}
        kind = tags["aws-cdk:subnet-type"]
        counts[kind] += 1
        (table,) = [a["RouteTableId"] for a in associations if a["SubnetId"] == {"Ref": name}]
        (default,) = [r for r in routes if r["RouteTableId"] == table and r.get("DestinationCidrBlock") == "0.0.0.0/0"]
        if kind == "Public":
            assert default["GatewayId"] == {"Ref": internet_gateway}
        else:
            assert subnet.get("MapPublicIpOnLaunch", False) is False
            assert "GatewayId" not in default
            assert default["NatGatewayId"] == {"Ref": nat_name}
    assert counts == {"Public": 3, "Private": 3}
    nat_subnet = subnets[nat["SubnetId"]["Ref"]]
    assert {t["Key"]: t["Value"] for t in nat_subnet["Tags"]}["aws-cdk:subnet-type"] == "Public"


def test_s3_endpoint_attaches_to_all_private_routes(examined: tuple[Any, Any, dict[str, Any], Path]) -> None:
    _, _, template, _ = examined
    (vpc,) = resources(template, "AWS::EC2::VPC")
    (endpoint,) = resources(template, "AWS::EC2::VPCEndpoint").values()
    assert endpoint["VpcId"] == {"Ref": vpc}
    assert endpoint["VpcEndpointType"] == "Gateway"
    assert endpoint["ServiceName"] == {"Fn::Join": ["", ["com.amazonaws.", {"Ref": "AWS::Region"}, ".s3"]]}
    private = {
        name for name, subnet in resources(template, "AWS::EC2::Subnet").items()
        if {t["Key"]: t["Value"] for t in subnet["Tags"]}["aws-cdk:subnet-type"] == "Private"
    }
    tables = {
        a["RouteTableId"]["Ref"] for a in resources(template, "AWS::EC2::SubnetRouteTableAssociation").values()
        if a["SubnetId"]["Ref"] in private
    }
    assert len(tables) == 3
    assert {t["Ref"] for t in endpoint["RouteTableIds"]} == tables


def test_exposed_vpc_can_be_consumed_across_stacks(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk
    from aws_cdk import aws_ec2 as ec2

    from rhi.network.stack import NetworkStack

    app = app_module.build(load({"RHI_CONFIG": "example"}), outdir=str(tmp_path), context=context())
    stage = app.node.find_child("Rhi")
    assert isinstance(stage.network, NetworkStack)
    consumer = cdk.Stack(stage, "NetworkConsumer")
    group = ec2.SecurityGroup(consumer, "Group", vpc=stage.network.vpc)
    app.synth()
    assert Path(stage.outdir).parent == tmp_path
    template = json.loads((Path(stage.outdir) / consumer.template_file).read_text(encoding="utf-8"))
    resource = group.node.default_child
    assert isinstance(resource, cdk.CfnResource)
    vpc_id = template["Resources"][consumer.resolve(resource.logical_id)]["Properties"]["VpcId"]
    assert "Fn::ImportValue" in vpc_id


def test_raw_nag_findings_have_module_reasons(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk
    import cdk_nag

    app = app_module.build(load({"RHI_CONFIG": "example"}), outdir=str(tmp_path), context=context())
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    try:
        app.synth()
    except RuntimeError:
        assert (tmp_path / "validation-report.json").is_file()
    report = json.loads((tmp_path / "validation-report.json").read_text(encoding="utf-8"))
    assert any(p["pluginName"] == "AwsSolutions" for p in report["pluginReports"])
    constructs = {construct.node.path: construct for construct in app.node.find_all()}
    for plugin in report["pluginReports"]:
        for violation in plugin["violations"]:
            for affected in violation["violatingConstructs"]:
                module = owner(constructs[affected["constructPath"]])
                if module in {"network_vpc", "network_stack", "stage"}:
                    reason = acknowledgements(module).get("nag", {}).get(violation["ruleName"], "")
                    assert len(reason.split()) >= 3 and reason.endswith("."), (module, violation["ruleName"])


def test_wildcard_role_actions_include_attached_managed_policies(
    examined: tuple[Any, Any, dict[str, Any], Path],
) -> None:
    import aws_cdk as cdk

    app, stack, template, _ = examined
    owners = {
        stack.resolve(c.logical_id): owner(c)
        for c in app.node.find_all() if isinstance(c, cdk.CfnResource) and cdk.Stack.of(c) is stack
    }
    missing = []
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] != "AWS::IAM::Role":
            continue
        module = owners[logical_id]
        admitted = acknowledgements(module).get("wildcard_actions", {})
        properties = resource["Properties"]
        wildcard_actions = set()
        for policy in properties.get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                targets = statement.get("Resource", [])
                if statement.get("Effect") == "Allow" and (targets == "*" or "*" in targets):
                    actions = statement["Action"]
                    wildcard_actions.update([actions] if isinstance(actions, str) else actions)
        for managed in properties.get("ManagedPolicyArns", []):
            if "policy/service-role/AWSLambdaBasicExecutionRole" in json.dumps(managed):
                # The published v1 document grants these on Resource "*":
                # https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSLambdaBasicExecutionRole.html
                wildcard_actions.update({"logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"})
        for action in wildcard_actions:
            reason = admitted.get(action, "")
            if not (len(reason.split()) >= 3 and reason.endswith(".")):
                missing.append((module, action))
    assert sorted(missing) == [], f"Wildcard actions lack module acknowledgements: {sorted(missing)}"
