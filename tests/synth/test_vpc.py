"""The network (system design, sections 4.4, 5.4 and 6.5): the zones the configuration lists,
resolved in the deploying region and named nowhere, or the region's first three when it lists none,
public subnets for the load balancer, private subnets for tasks and instances, one NAT gateway,
and an S3 gateway endpoint on the private route tables.

The example configuration lists four zones. `without_zones` (conftest.py) is the example with no
zones: a zone appended to the first three leaves the network of those three as it was.

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
# The region's first three zones, as the template writes them, and the example's fourth: the
# deploying region followed by the last letter of its name.
FIRST_THREE = [{"Fn::Select": [index, ZONE]} for index in range(3)]
FOURTH = {"Fn::Join": ["", [{"Ref": "AWS::Region"}, "d"]]}
# What a zone appended must leave as it was, under its logical ID.
KEPT = (
    "AWS::EC2::Subnet",
    "AWS::EC2::RouteTable",
    "AWS::EC2::Route",
    "AWS::EC2::SubnetRouteTableAssociation",
    "AWS::EC2::NatGateway",
)


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


def zones_by_type(template: dict[str, Any]) -> dict[str | None, list[Any]]:
    """The zone of each subnet by the subnets' type, in the order of the subnets' positions: a
    subnet's logical ID holds its group's name, then its position."""
    by_type: dict[str | None, list[Any]] = {}
    for _, subnet in sorted(of_type(template, "AWS::EC2::Subnet").items()):
        by_type.setdefault(tag(subnet, "aws-cdk:subnet-type"), []).append(subnet["Properties"]["AvailabilityZone"])
    return by_type


def test_subnets_span_the_configured_zones_in_their_order(network: dict[str, Any]) -> None:
    assert len(config.load({"RHI_CONFIG": "example"}).zones or ()) == 4
    # Four public and four private subnets: the first three of each in the region's first three
    # zones, as with no zones configured, and the fourth in the region's zone `d`.
    assert zones_by_type(network) == {"Public": [*FIRST_THREE, FOURTH], "Private": [*FIRST_THREE, FOURTH]}
    # The fourth of each takes the next block of its kind, after those of the first three.
    blocks = {name: s["Properties"]["CidrBlock"] for name, s in of_type(network, "AWS::EC2::Subnet").items()}
    assert [block for name, block in blocks.items() if "PublicSubnet4" in name] == ["10.0.3.0/24"]
    assert [block for name, block in blocks.items() if "PrivateSubnet4" in name] == ["10.0.64.0/20"]


def test_the_network_names_no_zone(network: dict[str, Any]) -> None:
    assert "example-zone" not in json.dumps(network)


def test_without_zones_the_subnets_span_the_first_three_of_the_deploying_region(
    without_zones: dict[str, dict[str, Any]],
) -> None:
    assert zones_by_type(without_zones["Network"]) == {"Public": FIRST_THREE, "Private": FIRST_THREE}


def test_a_zone_appended_leaves_the_network_of_the_first_three_as_it_was(
    network: dict[str, Any], without_zones: dict[str, dict[str, Any]]
) -> None:
    before = {name: r for name, r in without_zones["Network"]["Resources"].items() if r["Type"] in KEPT}
    # Three subnets of each type, a route table, an association and a default route for each, and
    # the one NAT gateway.
    assert [sum(r["Type"] == kind for r in before.values()) for kind in KEPT] == [6, 6, 6, 6, 1]
    after = network["Resources"]
    for name, resource in before.items():
        assert name in after, name
        assert (after[name]["Type"], after[name]["Properties"]) == (resource["Type"], resource["Properties"]), name


def test_the_vpc_refuses_fewer_than_three_zones(app_module: ModuleType) -> None:
    from rhi.network.vpc import Network

    cdk = app_module.cdk
    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Stack")
    zones = [cdk.Fn.select(index, cdk.Fn.get_azs()) for index in range(2)]
    with pytest.raises(ValueError, match="at least 3 zones"):
        Network(stack, "Network", availability_zones=zones)


def test_no_private_subnet_assigns_public_addresses(network: dict[str, Any]) -> None:
    private = [s for s in of_type(network, "AWS::EC2::Subnet").values() if tag(s, "aws-cdk:subnet-type") == "Private"]
    assert len(private) == 4
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
