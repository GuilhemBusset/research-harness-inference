"""The network (system design, sections 4.4, 5.4 and 6.5): three zones from the deploying region,
public subnets for the load balancer, private subnets for tasks and instances, one NAT gateway,
and an S3 gateway endpoint on the private route tables.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import inspect
import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "src" / "rhi"
ACKNOWLEDGED = REPO / "tests" / "policy" / "acknowledged"
ZONE = {"Fn::GetAZs": ""}


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r for name, r in template["Resources"].items() if r["Type"] == kind}


def tag(resource: dict[str, Any], key: str) -> str | None:
    return next((t["Value"] for t in resource["Properties"].get("Tags", []) if t["Key"] == key), None)


def route_table_of(template: dict[str, Any], subnet: str) -> str:
    (table,) = [
        a["Properties"]["RouteTableId"]["Ref"]
        for a in of_type(template, "AWS::EC2::SubnetRouteTableAssociation").values()
        if a["Properties"]["SubnetId"] == {"Ref": subnet}
    ]
    return table


@pytest.fixture(scope="module")
def network(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The template of the stage's network stack."""
    import aws_cdk as cdk

    from rhi.network.stack import NetworkStack

    stage = app.node.find_child("Rhi")
    stack = stage.network  # ty: ignore[missing-attribute] - the app's stage, typed as a construct
    assert isinstance(stack, NetworkStack)
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[missing-attribute]
    assert "/" in path, "the network stack is not in the stage's nested assembly"
    template = templates[path]
    default_child = stack.vpc.node.default_child
    assert isinstance(default_child, cdk.CfnElement)
    vpc = stack.resolve(default_child.logical_id)
    assert template["Resources"][vpc]["Type"] == "AWS::EC2::VPC"
    return template


def test_subnets_span_three_zones_of_the_deploying_region(network: dict[str, Any]) -> None:
    subnets = of_type(network, "AWS::EC2::Subnet").values()
    by_type: dict[str | None, list[Any]] = {}
    for subnet in subnets:
        by_type.setdefault(tag(subnet, "aws-cdk:subnet-type"), []).append(subnet["Properties"]["AvailabilityZone"])
    assert set(by_type) == {"Public", "Private"}
    zones = [{"Fn::Select": [index, ZONE]} for index in range(3)]
    for kind, selected in by_type.items():
        assert sorted(selected, key=json.dumps) == zones, kind


def test_no_private_subnet_assigns_public_addresses(network: dict[str, Any]) -> None:
    private = [s for s in of_type(network, "AWS::EC2::Subnet").values() if tag(s, "aws-cdk:subnet-type") == "Private"]
    assert len(private) == 3
    assert all(s["Properties"].get("MapPublicIpOnLaunch") is False for s in private)


def test_one_nat_gateway_in_a_public_subnet_routes_every_private_subnet(network: dict[str, Any]) -> None:
    (nat,) = of_type(network, "AWS::EC2::NatGateway")
    subnets = of_type(network, "AWS::EC2::Subnet")
    assert tag(subnets[network["Resources"][nat]["Properties"]["SubnetId"]["Ref"]], "aws-cdk:subnet-type") == "Public"
    routes = of_type(network, "AWS::EC2::Route").values()
    for name, subnet in subnets.items():
        if tag(subnet, "aws-cdk:subnet-type") == "Private":
            table = route_table_of(network, name)
            (default,) = [
                r["Properties"]
                for r in routes
                if r["Properties"]["RouteTableId"] == {"Ref": table}
                and r["Properties"].get("DestinationCidrBlock") == "0.0.0.0/0"
            ]
            assert default.get("NatGatewayId") == {"Ref": nat}, name


def test_an_s3_gateway_endpoint_serves_the_private_route_tables(network: dict[str, Any]) -> None:
    (endpoint,) = of_type(network, "AWS::EC2::VPCEndpoint").values()
    properties = endpoint["Properties"]
    assert properties["VpcEndpointType"] == "Gateway"
    assert properties["ServiceName"] == {"Fn::Join": ["", ["com.amazonaws.", {"Ref": "AWS::Region"}, ".s3"]]}
    private = [n for n, s in of_type(network, "AWS::EC2::Subnet").items() if tag(s, "aws-cdk:subnet-type") == "Private"]
    assert sorted(t["Ref"] for t in properties["RouteTableIds"]) == sorted(route_table_of(network, n) for n in private)


def test_other_stacks_take_the_vpc_from_the_network_stack(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk
    from aws_cdk import aws_ec2 as ec2

    from rhi.stage import EndpointStage

    app = cdk.App(outdir=str(tmp_path), analytics_reporting=False)
    stage = EndpointStage(app, "Rhi", config=config.load({"RHI_CONFIG": "example"}))
    consumer = cdk.Stack(stage, "Consumer")
    ec2.SecurityGroup(consumer, "Group", vpc=stage.network.vpc)
    app.synth()
    template = json.loads((Path(stage.outdir) / consumer.template_file).read_text(encoding="utf-8"))
    assert "Fn::ImportValue" in json.dumps(template["Resources"])


def owning_module(construct: Any) -> str | None:
    """The acknowledgement file of the nearest enclosing construct whose class is under `src/rhi/`
    (tests/policy/acknowledged/README.md)."""
    for scope in reversed(construct.node.scopes):
        try:
            defined_in = Path(inspect.getfile(type(scope))).resolve()
        except (TypeError, OSError):
            continue
        if defined_in.is_relative_to(SOURCE):
            return "_".join(defined_in.relative_to(SOURCE).with_suffix("").parts)
    return None


def test_every_cdk_nag_finding_is_acknowledged_by_its_module(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk
    import cdk_nag

    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(config.load({"RHI_CONFIG": "example"}), outdir=str(tmp_path), context=context)
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    try:
        app.synth()
    except RuntimeError:
        pass  # A finding fails synthesis; the report lists them all.
    report = json.loads((tmp_path / "validation-report.json").read_text(encoding="utf-8"))
    constructs = {c.node.path: c for c in app.node.find_all()}
    unacknowledged = []
    for plugin in report["pluginReports"]:
        for violation in plugin["violations"]:
            for violating in violation["violatingConstructs"]:
                module = owning_module(constructs[violating["constructPath"]])
                file = ACKNOWLEDGED / f"{module}.toml"
                nag = tomllib.loads(file.read_text(encoding="utf-8")).get("nag", {}) if file.is_file() else {}
                if violation["ruleName"] not in nag:
                    unacknowledged.append((module, violation["ruleName"], violating["constructPath"]))
    assert unacknowledged == []
