"""The public load balancer (system design, sections 5.3 and 6.5): internet-facing in the public
subnets behind its own group, a 1200-second idle timeout, and, only when `https_listener` is true,
one HTTPS listener on 443 with a DNS-validated certificate and the domain's alias record.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
POLICY = "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"


@dataclasses.dataclass(frozen=True)
class Built:
    stack: Any  # the stage's NetworkStack
    template: dict[str, Any]
    outdir: Path

    def of_type(self, kind: str) -> dict[str, dict[str, Any]]:
        return {name: r for name, r in self.template["Resources"].items() if r["Type"] == kind}

    def logical_id(self, construct: Any) -> str:
        return self.stack.resolve(construct.node.default_child.logical_id)


def build(app_module: ModuleType, tmp_path: Path, settings: config.Config) -> Built:
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(settings, outdir=str(tmp_path), context=context)
    app.synth()
    stage = app.node.find_child("Rhi")
    stack = stage.network
    template = json.loads((Path(stage.outdir) / stack.template_file).read_text(encoding="utf-8"))
    return Built(stack, template, tmp_path)


@pytest.fixture(scope="module")
def off(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Built:
    return build(app_module, tmp_path_factory.mktemp("off"), dataclasses.replace(EXAMPLE, https_listener=False))


@pytest.fixture(scope="module")
def on(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Built:
    assert EXAMPLE.https_listener is True
    return build(app_module, tmp_path_factory.mktemp("on"), EXAMPLE)


def test_with_the_key_off_the_load_balancer_faces_the_internet_with_no_listener(off: Built) -> None:
    (name, balancer) = off.of_type("AWS::ElasticLoadBalancingV2::LoadBalancer").popitem()
    assert name == off.logical_id(off.stack.load_balancer)
    properties = balancer["Properties"]
    assert properties["Type"] == "application"
    assert properties["Scheme"] == "internet-facing"
    public = [subnet.node.default_child for subnet in off.stack.vpc.public_subnets]
    assert sorted(s["Ref"] for s in properties["Subnets"]) == sorted(off.stack.resolve(s.logical_id) for s in public)
    group = off.logical_id(off.stack.security_groups.public_load_balancer)
    assert properties["SecurityGroups"] == [{"Fn::GetAtt": [group, "GroupId"]}]
    assert {"Key": "idle_timeout.timeout_seconds", "Value": "1200"} in properties["LoadBalancerAttributes"]

    assert off.stack.https_listener is None
    for kind in ("Listener", "ListenerRule"):
        assert off.of_type(f"AWS::ElasticLoadBalancingV2::{kind}") == {}
    assert off.of_type("AWS::CertificateManager::Certificate") == {}
    assert off.of_type("AWS::Route53::RecordSet") == {}


def test_with_the_key_on_one_https_listener_answers_404_under_the_tls13_policy(on: Built) -> None:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    assert isinstance(on.stack.https_listener, elbv2.ApplicationListener)
    balancer = on.logical_id(on.stack.load_balancer)
    ((name, listener),) = on.of_type("AWS::ElasticLoadBalancingV2::Listener").items()
    assert name == on.logical_id(on.stack.https_listener)
    properties = listener["Properties"]
    assert properties["LoadBalancerArn"] == {"Ref": balancer}
    assert (properties["Port"], properties["Protocol"]) == (443, "HTTPS")
    assert properties["SslPolicy"] == POLICY
    assert properties["DefaultActions"] == [{"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "404"}}]

    ((certificate, issued),) = on.of_type("AWS::CertificateManager::Certificate").items()
    assert properties["Certificates"] == [{"CertificateArn": {"Ref": certificate}}]
    assert issued["Properties"]["DomainName"] == EXAMPLE.domain
    assert issued["Properties"]["ValidationMethod"] == "DNS"
    assert issued["Properties"]["DomainValidationOptions"] == [
        {"DomainName": EXAMPLE.domain, "HostedZoneId": EXAMPLE.hosted_zone_id}
    ]

    (alias,) = on.of_type("AWS::Route53::RecordSet").values()
    record = alias["Properties"]
    assert (record["Name"], record["Type"], record["HostedZoneId"]) == (f"{EXAMPLE.domain}.", "A", EXAMPLE.hosted_zone_id)
    assert record["AliasTarget"]["HostedZoneId"] == {"Fn::GetAtt": [balancer, "CanonicalHostedZoneID"]}
    assert {"Fn::GetAtt": [balancer, "DNSName"]} in record["AliasTarget"]["DNSName"]["Fn::Join"][1]


def test_nothing_listens_on_port_80_and_no_hosted_zone_is_looked_up(on: Built) -> None:
    listeners = on.of_type("AWS::ElasticLoadBalancingV2::Listener").values()
    assert [listener["Properties"]["Port"] for listener in listeners] == [443]
    assert on.of_type("AWS::Route53::HostedZone") == {}  # imported by its ID, not created
    # A lookup the CDK could not answer from context is recorded in a manifest as missing.
    for manifest in on.outdir.rglob("manifest.json"):
        assert json.loads(manifest.read_text(encoding="utf-8")).get("missing", []) == [], manifest
