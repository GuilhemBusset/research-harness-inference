"""Independent witnesses for the security groups and the public-ingress invariant."""

from __future__ import annotations

import importlib.util
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
NAMES = ("public_load_balancer", "gateway", "grafana", "internal_load_balancer", "instances")
PORTS = (443, 8080, 3000, 80, 8000)


def module_at(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("examined", REPO / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def examined(app_module: ModuleType, tmp_path: Path) -> tuple[Any, Any, dict[str, Any]]:
    import aws_cdk as cdk

    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"]
    app = app_module.build(load({"RHI_CONFIG": "example"}), outdir=str(tmp_path), context=context)
    stack = app.node.find_child("Rhi").network
    template = dict(cdk.assertions.Template.from_stack(stack).to_json())
    return app, stack, template


def group_ids(stack: Any) -> dict[str, str]:
    return {
        name: stack.resolve(getattr(stack.security_groups, name).node.default_child.logical_id)
        for name in NAMES
    }


def ingress_rules(template: dict[str, Any], group: str) -> list[dict[str, Any]]:
    resources = template["Resources"]
    rules = list(resources[group]["Properties"].get("SecurityGroupIngress", []))
    for resource in resources.values():
        properties = resource.get("Properties", {})
        if resource["Type"] == "AWS::EC2::SecurityGroupIngress" and properties.get("GroupId") in (
            {"Ref": group}, {"Fn::GetAtt": [group, "GroupId"]}
        ):
            rules.append(properties)
    return rules


def test_exact_five_connections(examined: tuple[Any, Any, dict[str, Any]]) -> None:
    _, stack, template = examined
    ids = group_ids(stack)
    resources = template["Resources"]
    groups = {key: value for key, value in resources.items() if value["Type"] == "AWS::EC2::SecurityGroup"}
    assert set(groups) == set(ids.values())
    (vpc,) = [key for key, value in resources.items() if value["Type"] == "AWS::EC2::VPC"]
    sources = [None, "public_load_balancer", "public_load_balancer", "gateway", "internal_load_balancer"]
    for name, port, source in zip(NAMES, PORTS, sources, strict=True):
        properties = groups[ids[name]]["Properties"]
        assert properties["VpcId"] == {"Ref": vpc}
        rules = ingress_rules(template, ids[name])
        assert len(rules) == 1
        rule = rules[0]
        assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", port, port)
        if source is None:
            assert rule["CidrIp"] == "0.0.0.0/0"
            assert "SourceSecurityGroupId" not in rule
        else:
            assert rule["SourceSecurityGroupId"] in ({"Ref": ids[source]}, {"Fn::GetAtt": [ids[source], "GroupId"]})
            assert not {"CidrIp", "CidrIpv6", "SourcePrefixListId"}.intersection(rule)


def test_synth_assertion_rejects_wrong_ports(examined: tuple[Any, Any, dict[str, Any]]) -> None:
    _, stack, template = examined
    ids = group_ids(stack)
    check = module_at("tests/synth/test_security_groups.py").test_who_reaches_whom_on_which_port
    check((template, ids))
    for group in ids.values():
        changed = json.loads(json.dumps(template))
        rules = ingress_rules(changed, group)
        assert rules
        rules[0]["FromPort"] = rules[0]["ToPort"] = 1234
        with pytest.raises(AssertionError):
            check((changed, ids))


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("family", ["CidrIp", "CidrIpv6"])
def test_policy_rejects_open_instances(app_module: ModuleType, separate: bool, family: str) -> None:
    import aws_cdk as cdk
    from aws_cdk import aws_ec2 as ec2

    policy = module_at("tests/policy/test_no_public_ingress.py")
    stack = cdk.Stack()
    group = ec2.CfnSecurityGroup(stack, "Pool", group_description="pool")
    rule = {family: "0.0.0.0/0" if family == "CidrIp" else "::/0", "IpProtocol": "tcp",
            "FromPort": 8000, "ToPort": 8000}
    if separate:
        ingress = ec2.CfnSecurityGroupIngress(stack, "Open", group_id=group.attr_group_id, ip_protocol="tcp")
        for key, value in rule.items():
            ingress.add_property_override(key, value)
    else:
        group.add_property_override("SecurityGroupIngress", [rule])
    template = cdk.assertions.Template.from_stack(stack).to_json()
    assert policy.violations({"pool": template})
    closed = json.loads(json.dumps(template))
    for resource in closed["Resources"].values():
        resource.get("Properties", {}).pop("SecurityGroupIngress", None)
    closed["Resources"] = {k: v for k, v in closed["Resources"].items()
                           if v["Type"] != "AWS::EC2::SecurityGroupIngress"}
    assert policy.violations({"pool": closed}) == []


def test_policy_allows_only_edge_https(examined: tuple[Any, Any, dict[str, Any]]) -> None:
    _, stack, template = examined
    policy = module_at("tests/policy/test_no_public_ingress.py")
    assert policy.violations({"app": template}) == []
    ingress_rules(template, group_ids(stack)["public_load_balancer"])[0]["FromPort"] = 80
    assert policy.violations({"app": template})


def test_nag_reasons_and_no_security_roles(app_module: ModuleType, tmp_path: Path) -> None:
    import aws_cdk as cdk
    import cdk_nag

    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"]
    app = app_module.build(load({"RHI_CONFIG": "example"}), outdir=str(tmp_path), context=context)
    cdk.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    try:
        app.synth()
    except RuntimeError:
        assert (tmp_path / "validation-report.json").is_file()
    constructs = {c.node.path: c for c in app.node.find_all()}
    report = json.loads((tmp_path / "validation-report.json").read_text(encoding="utf-8"))
    for plugin in report["pluginReports"]:
        for finding in plugin["violations"]:
            for affected in finding["violatingConstructs"]:
                construct = constructs[affected["constructPath"]]
                for scope in reversed(construct.node.scopes):
                    if type(scope).__module__ in {"rhi.network.security", "rhi.network.stack"}:
                        module = Path(inspect.getfile(type(scope))).stem
                        path = REPO / "tests/policy/acknowledged" / f"network_{module}.toml"
                        reason = tomllib.loads(path.read_text(encoding="utf-8"))["nag"][finding["ruleName"]]
                        assert len(reason.split()) >= 3 and reason.endswith(".")
                        break
                    if type(scope).__module__.startswith("rhi."):
                        break
    groups = app.node.find_child("Rhi").network.security_groups
    assert all(not c.cfn_resource_type.startswith("AWS::IAM::")
               for c in groups.node.find_all() if isinstance(c, cdk.CfnResource))
