"""The security groups (system design, sections 4.1 and 6.5): the public load balancer admits HTTPS
from anywhere and reaches the gateway and Grafana; only the gateway reaches the internal load
balancer; only the internal load balancer reaches the engine's port on the instances.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

    import aws_cdk as cdk

GROUPS = ("public_load_balancer", "gateway", "grafana", "internal_load_balancer", "instances")


@pytest.fixture(scope="module")
def network(
    app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The network stack's template, and the logical ID of each of the five groups."""
    import aws_cdk as cdk

    stage = app.node.find_child("Rhi")
    stack = stage.network  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    ids = {}
    for name in GROUPS:
        resource = getattr(stack.security_groups, name).node.default_child
        assert isinstance(resource, cdk.CfnElement)
        ids[name] = stack.resolve(resource.logical_id)
    return templates[path], ids


def ingress(template: dict[str, Any], group: str) -> set[tuple[str, int, int, str]]:
    """The group's rules, inline and separate, as `(protocol, from, to, source)`: a range, or the
    logical ID of the source group."""

    def read(rule: dict[str, Any]) -> tuple[str, int, int, str]:
        source = rule.get("CidrIp") or rule.get("CidrIpv6") or rule["SourceSecurityGroupId"]["Fn::GetAtt"][0]
        return rule["IpProtocol"], rule["FromPort"], rule["ToPort"], source

    resources = template["Resources"]
    found = {read(rule) for rule in resources[group]["Properties"].get("SecurityGroupIngress", [])}
    for resource in resources.values():
        properties = resource.get("Properties", {})
        if resource["Type"] == "AWS::EC2::SecurityGroupIngress" and properties["GroupId"] == {
            "Fn::GetAtt": [group, "GroupId"]
        }:
            found.add(read(properties))
    return found


def test_who_reaches_whom_on_which_port(network: tuple[dict[str, Any], dict[str, str]]) -> None:
    template, ids = network
    groups = {n for n, r in template["Resources"].items() if r["Type"] == "AWS::EC2::SecurityGroup"}
    assert groups == set(ids.values())
    expected = {
        "public_load_balancer": {("tcp", 443, 443, "0.0.0.0/0")},
        "gateway": {("tcp", 8080, 8080, ids["public_load_balancer"])},
        "grafana": {("tcp", 3000, 3000, ids["public_load_balancer"])},
        "internal_load_balancer": {("tcp", 80, 80, ids["gateway"])},
        "instances": {("tcp", 8000, 8000, ids["internal_load_balancer"])},
    }
    assert {name: ingress(template, ids[name]) for name in GROUPS} == expected


def test_groups_are_in_the_vpc_and_the_edge_is_tagged(network: tuple[dict[str, Any], dict[str, str]]) -> None:
    template, ids = network
    (vpc,) = [n for n, r in template["Resources"].items() if r["Type"] == "AWS::EC2::VPC"]
    for name in GROUPS:
        properties = template["Resources"][ids[name]]["Properties"]
        assert properties["VpcId"] == {"Ref": vpc}, name
        tags = {t["Key"]: t["Value"] for t in properties.get("Tags", [])}
        assert (tags.get("rhi-edge") == "public-load-balancer") == (name == "public_load_balancer"), name


def test_other_stacks_take_the_groups_from_the_network_stack(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk
    from aws_cdk import aws_ec2 as ec2

    from rhi.stage import EndpointStage

    app = cdk.App(outdir=str(tmp_path), analytics_reporting=False)
    stage = EndpointStage(app, "Rhi", config=config.load({"RHI_CONFIG": "example"}))
    consumer = cdk.Stack(stage, "Consumer")
    groups = stage.network.security_groups
    ec2.CfnSecurityGroupIngress(
        consumer,
        "Rule",
        group_id=groups.instances.security_group_id,
        source_security_group_id=groups.gateway.security_group_id,
        ip_protocol="tcp",
        from_port=9110,
        to_port=9110,
    )
    app.synth()
    template = json.loads((Path(stage.outdir) / consumer.template_file).read_text(encoding="utf-8"))
    (rule,) = template["Resources"].values()
    assert "Fn::ImportValue" in rule["Properties"]["GroupId"]
    assert "Fn::ImportValue" in rule["Properties"]["SourceSecurityGroupId"]
