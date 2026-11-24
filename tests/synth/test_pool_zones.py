"""The zones each pool's group launches in (system design, section 5.4): the private subnets of
the pool's own zones and no other, or those of the VPC's first three zones for a pool that lists
none, while the internal load balancer spans every private subnet of the VPC, so that it reaches a
target in whichever zone a group launches it. The zones place the groups and nothing else: no
template holds a zone's name, and the gateway and the scaler are told nothing of them.

The example configuration lists four zones; its coursework pool lists the first, the second and the
fourth, and its mock pool none. `without_zones` (conftest.py) is the example with no zones.

The CDK is imported inside the fixtures, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping

    import aws_cdk as cdk

EXAMPLE = config.load({"RHI_CONFIG": "example"})
ZONE = {"Fn::GetAZs": ""}
# The VPC's zones as the network's template writes them, by position: the region's first three,
# and the example's fourth.
FIRST, SECOND, THIRD = ({"Fn::Select": [index, ZONE]} for index in range(3))
FOURTH = {"Fn::Join": ["", [{"Ref": "AWS::Region"}, "d"]]}


def of_type(template: Mapping[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def private_subnets(network: Mapping[str, Any]) -> dict[str, Any]:
    """Each private subnet of the network's template, by its logical ID, with its zone."""
    return {
        name: subnet["AvailabilityZone"]
        for name, subnet in of_type(network, "AWS::EC2::Subnet").items()
        if {"Key": "aws-cdk:subnet-type", "Value": "Private"} in subnet["Tags"]
    }


def zones_of(imported: list[dict[str, str]], network: Mapping[str, Any]) -> list[Any]:
    """The zones of the subnets a resource of another stack imports from the network stack, each
    one a private subnet named once, in the order of the zones' positions in the VPC."""
    exported = {o["Export"]["Name"]: o["Value"] for o in network["Outputs"].values() if "Export" in o}
    subnets = [exported[subnet["Fn::ImportValue"]]["Ref"] for subnet in imported]
    assert len(set(subnets)) == len(subnets)
    private = private_subnets(network)
    return [private[subnet] for subnet in sorted(subnets)]


def groups(pools: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each pool's group, by the pool's name, which the group carries as a tag."""
    found = of_type(pools, "AWS::AutoScaling::AutoScalingGroup").values()
    return {next(tag["Value"] for tag in group["Tags"] if tag["Key"] == "rhi-pool"): group for group in found}


def group_zones(pools: Mapping[str, Any], network: Mapping[str, Any]) -> dict[str, list[Any]]:
    """The zones of each pool's group, by the pool's name."""
    return {pool: zones_of(group["VPCZoneIdentifier"], network) for pool, group in groups(pools).items()}


def balancer_zones(pools: Mapping[str, Any], network: Mapping[str, Any]) -> list[Any]:
    (balancer,) = of_type(pools, "AWS::ElasticLoadBalancingV2::LoadBalancer").values()
    assert balancer["Scheme"] == "internal"
    return zones_of(balancer["Subnets"], network)


@pytest.fixture(scope="module")
def example(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The templates of the example's network stack and pools stack, by the stack's ID."""
    stage = app.node.find_child("Rhi")
    stacks = {"Network": stage.network, "Pools": stage.pools}  # ty: ignore[missing-attribute] - the app's stage, typed as a construct
    return {
        name: templates[(Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()]  # ty: ignore[missing-attribute]
        for name, stack in stacks.items()
    }


def test_each_group_spans_the_private_subnets_of_its_pools_zones_alone(example: dict[str, dict[str, Any]]) -> None:
    by_name = {pool.name: pool for pool in EXAMPLE.pools}
    zones = EXAMPLE.zones or ()
    assert len(zones) == 4
    assert (by_name["coursework"].zones, by_name["mock"].zones) == ((zones[0], zones[1], zones[3]), None)
    assert group_zones(example["Pools"], example["Network"]) == {
        # Its own zones: the first, the second and the fourth, and not the third.
        "coursework": [FIRST, SECOND, FOURTH],
        # No zones of its own: the first three of the VPC.
        "mock": [FIRST, SECOND, THIRD],
    }


def test_the_internal_load_balancer_spans_every_private_subnet(example: dict[str, dict[str, Any]]) -> None:
    network = example["Network"]
    assert len(private_subnets(network)) == 4
    assert balancer_zones(example["Pools"], network) == [FIRST, SECOND, THIRD, FOURTH]


def test_without_zones_every_group_spans_the_first_three(
    example: dict[str, dict[str, Any]], without_zones: dict[str, dict[str, Any]]
) -> None:
    pools, network = without_zones["Pools"], without_zones["Network"]
    assert group_zones(pools, network) == {"coursework": [FIRST, SECOND, THIRD], "mock": [FIRST, SECOND, THIRD]}
    assert balancer_zones(pools, network) == [FIRST, SECOND, THIRD]

    # A zone appended to the VPC changes nothing of the group of a pool that lists none.
    assert groups(example["Pools"])["mock"] == groups(pools)["mock"]


def test_no_template_names_a_zone(templates: dict[str, dict[str, Any]]) -> None:
    # The owner's configuration holds the names, and a template only positions and a last letter:
    # a stack that held one in full would name its region.
    zones = EXAMPLE.zones or ()
    assert [path for path, template in templates.items() if any(zone in json.dumps(template) for zone in zones)] == []


def test_the_zones_change_nothing_the_gateway_and_the_scaler_are_given(
    app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]], without_zones: dict[str, dict[str, Any]]
) -> None:
    # A pool's zones say where its group launches, which neither program reads: giving them, or
    # changing them, replaces neither the gateway's task definition nor the scaler's function.
    stage = app.node.find_child("Rhi")
    for stack, kind in (("Gateway", "AWS::ECS::TaskDefinition"), ("Pools", "AWS::Lambda::Function")):
        held_by = stage.node.find_child(stack)
        path = (Path(stage.outdir) / held_by.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[missing-attribute] - the app's stage and stack, typed as constructs
        assert of_type(templates[path], kind)
        assert of_type(templates[path], kind) == of_type(without_zones[stack], kind)
