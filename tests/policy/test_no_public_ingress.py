"""Nothing but the public load balancer faces the internet (system design, sections 6.5 and 8.4).

No security group of a task or an instance admits an address range open to every IPv4 or every
IPv6 address. The one group that may is the public load balancer's, and only on HTTPS: TCP 443.

A rule is read in both of its forms: inline, in a group's `SecurityGroupIngress`, and as an
`AWS::EC2::SecurityGroupIngress` resource, which names its group by `Ref` or `Fn::GetAtt` in its
template. A range is open when its prefix length is 0, and also when the template does not write it
as a literal: a range built from a parameter or another resource could be anything at deployment.
An open rule whose group the template does not name is refused, since it could be on any group.

The public load balancer's group says it is the edge by its tag `rhi-edge` = `public-load-balancer`.
Only one group may carry it.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import ModuleType

EDGE_TAG = "rhi-edge"
EDGE_VALUE = "public-load-balancer"
HTTPS = 443

NOT_THE_EDGE = "a group other than the public load balancer's admits every address"
EDGE_PORT = "the public load balancer's group admits every address on another port than 443"
NO_GROUP = "an ingress rule open to every address names no group of its template"
TWO_EDGES = f"more than one group carries the tag {EDGE_TAG}"

GROUP = "AWS::EC2::SecurityGroup"
INGRESS = "AWS::EC2::SecurityGroupIngress"
RANGES = ("CidrIp", "CidrIpv6")


def is_open(rule: Mapping[str, Any]) -> bool:
    """Whether the rule admits a range open to every address, or one the template does not fix."""
    for key in RANGES:
        if key not in rule:
            continue
        value = rule[key]
        if not isinstance(value, str):
            return True
        try:
            if ipaddress.ip_network(value, strict=False).prefixlen == 0:
                return True
        except ValueError:
            return True
    return False


def number(value: Any) -> int | None:
    if type(value) is int:
        return value
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]+", value, re.ASCII) else None


def only_https(rule: Mapping[str, Any]) -> bool:
    return (
        rule.get("IpProtocol") in ("tcp", "6")
        and number(rule.get("FromPort")) == HTTPS
        and number(rule.get("ToPort")) == HTTPS
    )


def is_edge(properties: Mapping[str, Any]) -> bool:
    tags = properties.get("Tags")
    return isinstance(tags, list) and any(t.get("Key") == EDGE_TAG and t.get("Value") == EDGE_VALUE for t in tags)


def group_of(template: Mapping[str, Any], group_id: Any) -> str | None:
    """The logical ID of the group in the template that a `GroupId` names, or None."""
    resources = template.get("Resources", {})
    if isinstance(group_id, dict) and "Ref" in group_id:
        name = group_id["Ref"]
    elif isinstance(group_id, dict) and "Fn::GetAtt" in group_id and group_id["Fn::GetAtt"][1:] == ["GroupId"]:
        name = group_id["Fn::GetAtt"][0]
    else:
        return None
    return name if resources.get(name, {}).get("Type") == GROUP else None


def rules(template: Mapping[str, Any]) -> Iterator[tuple[str, str | None, Mapping[str, Any]]]:
    """Every ingress rule of the template, as `(where it is written, its group or None, the rule)`."""
    for logical_id, resource in template.get("Resources", {}).items():
        properties = resource.get("Properties", {})
        if resource.get("Type") == GROUP:
            for rule in properties.get("SecurityGroupIngress", []) or []:
                yield logical_id, logical_id, rule
        elif resource.get("Type") == INGRESS:
            yield logical_id, group_of(template, properties.get("GroupId")), properties


def violations(templates: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """Every open rule that is refused, as `(template path, logical ID, what)`."""
    edges = [
        (path, logical_id)
        for path, template in templates.items()
        for logical_id, resource in template.get("Resources", {}).items()
        if resource.get("Type") == GROUP and is_edge(resource.get("Properties", {}))
    ]
    found = [(path, logical_id, TWO_EDGES) for path, logical_id in edges] if len(edges) > 1 else []
    for path, template in templates.items():
        for written_in, group, rule in rules(template):
            if not is_open(rule):
                continue
            if group is None:
                found.append((path, written_in, NO_GROUP))
            elif (path, group) not in edges:
                found.append((path, group, NOT_THE_EDGE))
            elif not only_https(rule):
                found.append((path, group, EDGE_PORT))
    return found


def test_nothing_but_the_public_load_balancer_faces_the_internet(templates: dict[str, dict[str, Any]]) -> None:
    assert violations(templates) == []


BUILT = "built.template.json"


def built(
    cdk: ModuleType, *, open_rule: dict[str, Any] | None = None, form: str = "inline", edge_rule: Any = None
) -> dict[str, Any]:
    """A template, built by the CDK, with the edge group open on 443 (or on `edge_rule`) and an
    instance group, with `open_rule` on it in the given form."""
    ec2 = cdk.aws_ec2
    stack = cdk.Stack()
    https = ec2.CfnSecurityGroup.IngressProperty(ip_protocol="tcp", from_port=HTTPS, to_port=HTTPS, cidr_ip="0.0.0.0/0")
    ec2.CfnSecurityGroup(
        stack,
        "Edge",
        group_description="edge",
        security_group_ingress=[edge_rule or https],
        tags=[cdk.CfnTag(key=EDGE_TAG, value=EDGE_VALUE)],
    )
    inline = [ec2.CfnSecurityGroup.IngressProperty(**open_rule)] if open_rule and form == "inline" else None
    instances = ec2.CfnSecurityGroup(stack, "Instances", group_description="instances", security_group_ingress=inline)
    if open_rule and form == "separate":
        ec2.CfnSecurityGroupIngress(stack, "Open", group_id=instances.attr_group_id, **open_rule)
    return {BUILT: cdk.assertions.Template.from_stack(stack).to_json()}


def found(templates: dict[str, Any]) -> list[tuple[str, str]]:
    return [(logical_id, what) for _, logical_id, what in violations(templates)]


@pytest.mark.parametrize("form", ["inline", "separate"])
@pytest.mark.parametrize("cidr", [{"cidr_ip": "0.0.0.0/0"}, {"cidr_ipv6": "::/0"}], ids=["IPv4", "IPv6"])
def test_detects_an_open_rule(cdk: ModuleType, form: str, cidr: dict[str, str]) -> None:
    assert found(built(cdk)) == []
    engine = {"ip_protocol": "tcp", "from_port": 8000, "to_port": 8000} | cidr
    assert found(built(cdk, open_rule=engine, form=form)) == [("Instances", NOT_THE_EDGE)]


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param({"ip_protocol": "tcp", "from_port": 80, "to_port": 80, "cidr_ip": "0.0.0.0/0"}, id="port 80"),
        pytest.param({"ip_protocol": "tcp", "from_port": 0, "to_port": 65535, "cidr_ipv6": "::/0"}, id="every port"),
        pytest.param({"ip_protocol": "-1", "cidr_ip": "0.0.0.0/0"}, id="every protocol"),
    ],
)
def test_detects_the_edge_open_on_another_port(cdk: ModuleType, rule: dict[str, Any]) -> None:
    edge_rule = cdk.aws_ec2.CfnSecurityGroup.IngressProperty(**rule)
    assert found(built(cdk, edge_rule=edge_rule)) == [("Edge", EDGE_PORT)]


def by_hand(*extra: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """A template written by hand: the edge group, an instance group, and the extra resources."""
    edge = {"Tags": [{"Key": EDGE_TAG, "Value": EDGE_VALUE}], "SecurityGroupIngress": []}
    resources = {"Edge": {"Type": GROUP, "Properties": edge}, "Instances": {"Type": GROUP, "Properties": {}}}
    return {BUILT: {"Resources": resources | dict(extra)}}


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param(
            ("Open", {"Type": INGRESS, "Properties": {"GroupId": {"Ref": "Instances"}, "CidrIp": {"Ref": "Range"}}}),
            [("Instances", NOT_THE_EDGE)],
            id="a range of a parameter",
        ),
        pytest.param(
            ("Open", {"Type": INGRESS, "Properties": {"GroupId": {"Fn::ImportValue": "Group"}, "CidrIp": "0.0.0.0/0"}}),
            [("Open", NO_GROUP)],
            id="a rule on a group of another template",
        ),
        pytest.param(
            ("Other", {"Type": GROUP, "Properties": {"Tags": [{"Key": EDGE_TAG, "Value": EDGE_VALUE}]}}),
            [("Edge", TWO_EDGES), ("Other", TWO_EDGES)],
            id="a second edge",
        ),
        pytest.param(
            ("Closed", {"Type": INGRESS, "Properties": {"GroupId": {"Ref": "Instances"}, "CidrIp": "10.0.0.0/16"}}),
            [],
            id="a private range",
        ),
    ],
)
def test_detects_each_breach(extra: tuple[str, dict[str, Any]], expected: list[tuple[str, str]]) -> None:
    assert found(by_hand()) == []
    assert found(by_hand(extra)) == expected
