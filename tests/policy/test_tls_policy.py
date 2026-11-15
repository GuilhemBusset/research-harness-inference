"""Every listener of a public load balancer takes TLS under the policy AWS recommends, and none
listens on port 80 (system design, sections 6.5 and 8.4).

The CDK's default policy, `ELBSecurityPolicy-2016-08`, allows TLS 1.1 and lacks TLS 1.3
(evidence E26), so a listener must name `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`; a listener
that names none gets the default. A port-80 listener is refused even as a redirect: the key would
already have crossed in cleartext.

A load balancer is public unless its template says `Scheme: internal`, since internet-facing is
CloudFormation's default. A listener whose load balancer is not a resource of its own template is
held to the same rule: nothing shows it is internal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

POLICY = "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"
LOAD_BALANCER = "AWS::ElasticLoadBalancingV2::LoadBalancer"
LISTENER = "AWS::ElasticLoadBalancingV2::Listener"

OTHER_POLICY = f"a listener of a public load balancer names another policy than {POLICY}"
PORT_80 = "a listener of a public load balancer listens on port 80"


def public_listeners(template: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The properties of every listener of the template whose load balancer may be public."""
    resources = template.get("Resources", {})
    found = {}
    for name, resource in resources.items():
        if resource.get("Type") != LISTENER:
            continue
        properties = resource.get("Properties", {})
        arn = properties.get("LoadBalancerArn")
        balancer = resources.get(arn.get("Ref")) if isinstance(arn, dict) else None
        internal = (
            isinstance(balancer, dict)
            and balancer.get("Type") == LOAD_BALANCER
            and balancer.get("Properties", {}).get("Scheme") == "internal"
        )
        if not internal:
            found[name] = properties
    return found


def violations(template: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Each listener of a public load balancer that breaks the invariant, with what it breaks."""
    found = []
    for name, properties in public_listeners(template).items():
        if properties.get("SslPolicy") != POLICY:
            found.append((name, OTHER_POLICY))
        if str(properties.get("Port")) == "80":
            found.append((name, PORT_80))
    return found


def test_every_listener_of_a_public_load_balancer_names_the_tls13_policy(templates: dict[str, Any]) -> None:
    # The example configuration turns the listener on: the invariant has something to read.
    assert any(public_listeners(template) for template in templates.values())
    assert {path: violations(template) for path, template in templates.items() if violations(template)} == {}


def synthesize(cdk: ModuleType, build: Any) -> Mapping[str, Any]:
    """The template of a stack with one VPC, built by `build(stack, vpc)`."""
    from aws_cdk import assertions
    from aws_cdk import aws_ec2 as ec2

    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Edge")
    build(stack, ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=0))
    return assertions.Template.from_stack(stack).to_json()


def test_detects_the_default_policy(cdk: ModuleType) -> None:
    from aws_cdk import aws_certificatemanager as acm
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    def https(policy: elbv2.SslPolicy | None) -> Any:
        def build(stack: Any, vpc: Any) -> None:
            balancer = elbv2.ApplicationLoadBalancer(stack, "LoadBalancer", vpc=vpc, internet_facing=True)
            certificate = acm.Certificate(stack, "Certificate", domain_name="example.com")
            balancer.add_listener(
                "Https",
                port=443,
                certificates=[certificate],
                ssl_policy=policy,
                open=False,
                default_action=elbv2.ListenerAction.fixed_response(404),
            )

        return build

    unnamed = synthesize(cdk, https(None))
    assert all("SslPolicy" not in listener for listener in public_listeners(unnamed).values())
    assert [problem for _, problem in violations(unnamed)] == [OTHER_POLICY]

    # The CDK's member for the default policy (evidence E26).
    default = synthesize(cdk, https(elbv2.SslPolicy.RECOMMENDED))
    assert [listener["SslPolicy"] for listener in public_listeners(default).values()] == ["ELBSecurityPolicy-2016-08"]
    assert [problem for _, problem in violations(default)] == [OTHER_POLICY]

    assert violations(synthesize(cdk, https(elbv2.SslPolicy.TLS13_12_RES_PQ))) == []


def test_detects_a_listener_on_port_80(cdk: ModuleType) -> None:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    def http(*, internet_facing: bool) -> Any:
        def build(stack: Any, vpc: Any) -> None:
            balancer = elbv2.ApplicationLoadBalancer(stack, "LoadBalancer", vpc=vpc, internet_facing=internet_facing)
            balancer.add_redirect(source_port=80, target_port=443)

        return build

    assert PORT_80 in [problem for _, problem in violations(synthesize(cdk, http(internet_facing=True)))]
    # An internal load balancer is not the edge: its port 80 is the gateway's way to the pools.
    assert violations(synthesize(cdk, http(internet_facing=False))) == []
