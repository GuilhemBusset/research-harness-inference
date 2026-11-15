"""Independent checks of issue 77's edge and TLS guard, without an AWS client."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
POLICY = "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"
LB = "AWS::ElasticLoadBalancingV2::LoadBalancer"
LISTENER = "AWS::ElasticLoadBalancingV2::Listener"
CERT = "AWS::CertificateManager::Certificate"
RECORD = "AWS::Route53::RecordSet"


@pytest.fixture(scope="module")
def cdk77() -> ModuleType:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        import aws_cdk
    return aws_cdk


def create(cdk77: ModuleType, folder: Path, enabled: bool) -> tuple[Any, Any]:
    from rhi.stage import EndpointStage

    settings = dataclasses.replace(
        config.load({"RHI_CONFIG": "example"}),
        https_listener=enabled,
        domain="edge.example.com",
        hosted_zone_id="EXAMPLEHOSTEDZONE",
    )
    context = json.loads((ROOT / "cdk.json").read_text())["context"] | {"aws:cdk:bundling-stacks": []}
    app = cdk77.App(outdir=str(folder), context=context, analytics_reporting=False)
    return app, EndpointStage(app, "Probe", config=settings).network


@pytest.fixture(scope="module")
def edges(cdk77: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[bool, tuple[Any, dict[str, Any]]]:
    from aws_cdk.assertions import Template

    result = {}
    for enabled in (False, True):
        app, stack = create(cdk77, tmp_path_factory.mktemp(f"edge-{enabled}"), enabled)
        app.synth()
        for manifest in Path(app.outdir).rglob("manifest.json"):
            assert not json.loads(manifest.read_text()).get("missing", [])
        result[enabled] = (stack, dict(Template.from_stack(stack).to_json()))
    return result


def resources(template: dict[str, Any], kind: str) -> dict[str, Any]:
    return {name: item["Properties"] for name, item in template["Resources"].items() if item["Type"] == kind}


def test_public_balancer_in_both_states(edges: dict[bool, tuple[Any, dict[str, Any]]]) -> None:
    for stack, template in edges.values():
        ((name, lb),) = resources(template, LB).items()
        assert name == stack.get_logical_id(stack.load_balancer.node.default_child)
        assert (lb["Scheme"], lb["Type"]) == ("internet-facing", "application")
        assert lb["Subnets"] == [{"Ref": stack.get_logical_id(s.node.default_child)} for s in stack.vpc.public_subnets]
        sg = stack.get_logical_id(stack.security_groups.public_load_balancer.node.default_child)
        assert lb["SecurityGroups"] == [{"Fn::GetAtt": [sg, "GroupId"]}]
        assert {"Key": "idle_timeout.timeout_seconds", "Value": "1200"} in lb["LoadBalancerAttributes"]


def test_disabled_means_no_tls_resources(edges: dict[bool, tuple[Any, dict[str, Any]]]) -> None:
    stack, template = edges[False]
    assert stack.https_listener is None
    assert all(not resources(template, kind) for kind in (LISTENER, CERT, RECORD))


def test_enabled_uses_configured_dns_and_tls(edges: dict[bool, tuple[Any, dict[str, Any]]]) -> None:
    stack, template = edges[True]
    ((lb, _),) = resources(template, LB).items()
    ((listener_id, listener),) = resources(template, LISTENER).items()
    assert listener_id == stack.get_logical_id(stack.https_listener.node.default_child)
    assert listener["LoadBalancerArn"] == {"Ref": lb}
    assert (listener["Port"], listener["Protocol"], listener["SslPolicy"]) == (443, "HTTPS", POLICY)
    assert listener["DefaultActions"] == [{"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "404"}}]
    ((certificate_id, certificate),) = resources(template, CERT).items()
    assert listener["Certificates"] == [{"CertificateArn": {"Ref": certificate_id}}]
    assert certificate["DomainName"] == "edge.example.com"
    assert certificate["ValidationMethod"] == "DNS"
    assert certificate["DomainValidationOptions"] == [{"DomainName": "edge.example.com", "HostedZoneId": "EXAMPLEHOSTEDZONE"}]
    (alias,) = resources(template, RECORD).values()
    assert (alias["Name"], alias["Type"], alias["HostedZoneId"]) == ("edge.example.com.", "A", "EXAMPLEHOSTEDZONE")
    assert alias["AliasTarget"]["HostedZoneId"] == {"Fn::GetAtt": [lb, "CanonicalHostedZoneID"]}
    assert {"Fn::GetAtt": [lb, "DNSName"]} in alias["AliasTarget"]["DNSName"]["Fn::Join"][1]
    assert not resources(template, "AWS::Route53::HostedZone")


def test_config_default_and_boolean_roundtrip(tmp_path: Path) -> None:
    values = tomllib.loads((ROOT / "config.example.toml").read_text())
    assert values.pop("https_listener") is True
    assert config.parse(values, "probe").https_listener is False
    for enabled in (True, False):
        settings = config.parse(values | {"https_listener": enabled}, "probe")
        destination = tmp_path / "config.toml"
        config.write_file(settings, destination)
        assert config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(destination)}) == settings
        assert config.KEYS["https_listener"].from_text(str(enabled).lower()) is enabled


def test_ssm_switch_and_missing_parameter() -> None:
    class Parameters:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def put_parameter(self, **request: Any) -> None:
            self.values[request["Name"]] = request["Value"]

        def get_parameters_by_path(self, **request: Any) -> dict[str, Any]:
            assert request["Path"] == "/rhi/config/"
            return {"Parameters": [{"Name": k, "Value": v, "Type": "String"} for k, v in self.values.items()]}

    stored = Parameters()
    example = config.load({"RHI_CONFIG": "example"})
    for enabled in (False, True):
        settings = dataclasses.replace(example, https_listener=enabled)
        config.write_ssm(settings, stored)
        assert stored.values["/rhi/config/https_listener"] == str(enabled).lower()
        assert config.read_ssm(stored) == settings
    del stored.values["/rhi/config/https_listener"]
    assert config.read_ssm(stored) == dataclasses.replace(example, https_listener=False)


def test_policy_rejects_mutations(edges: dict[bool, tuple[Any, dict[str, Any]]]) -> None:
    spec = importlib.util.spec_from_file_location("tls77", ROOT / "tests/policy/test_tls_policy.py")
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    template = edges[True][1]
    assert guard.violations(template) == []
    for policy in (None, "ELBSecurityPolicy-2016-08", "ELBSecurityPolicy-TLS13-1-2-2021-06"):
        mutated = json.loads(json.dumps(template))
        (listener,) = resources(mutated, LISTENER).values()
        listener.pop("SslPolicy")
        if policy is not None:
            listener["SslPolicy"] = policy
        assert guard.violations(mutated)
    mutated = json.loads(json.dumps(template))
    (listener,) = resources(mutated, LISTENER).values()
    listener["Port"] = 80
    assert guard.violations(mutated)


def test_nag_findings_have_module_reasons(cdk77: ModuleType, tmp_path: Path) -> None:
    import cdk_nag

    app, stack = create(cdk77, tmp_path, True)
    cdk77.Validations.of(app).add_plugins(cdk_nag.AwsSolutionsChecks(app))
    try:
        app.synth()
    except RuntimeError:
        pass  # cdk-nag reports findings in validation-report.json even when synthesis fails.
    report = json.loads((tmp_path / "validation-report.json").read_text())
    constructs = {item.node.path: item for item in app.node.find_all()}
    for plugin in report["pluginReports"]:
        for violation in plugin["violations"]:
            for location in violation["violatingConstructs"]:
                construct = constructs[location["constructPath"]]
                modules = [type(parent).__module__ for parent in reversed(construct.node.scopes)]
                module = next(name for name in modules if name.startswith("rhi."))
                acknowledged = ROOT / "tests/policy/acknowledged" / (module.removeprefix("rhi.").replace(".", "_") + ".toml")
                reasons = tomllib.loads(acknowledged.read_text())["nag"]
                assert len(reasons[violation["ruleName"]].split()) >= 3
    edge = stack.node.find_child("Edge")
    assert not any(isinstance(item, cdk77.CfnResource) and item.cfn_resource_type.startswith("AWS::IAM::") for item in edge.node.find_all())
