"""The internal load balancer (system design, sections 2, 4.1, 5.1 and 5.2): one HTTP listener on
port 80 whose rules forward the four inference routes, POST only, to the target group of the pool
the `X-Rhi-Pool` header names, and a fixed 404 for everything else; each target group on the
engine's port, checked every 5 seconds, least outstanding requests, drained for `drain_seconds`.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    import aws_cdk as cdk

ROUTES = {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens"}
# What the engine answers outside the four, and what the load balancer must not forward (E23).
FORBIDDEN = ("/invocations", "/pause", "/tokenize", "/metrics", "/health")
EXAMPLE = config.load({"RHI_CONFIG": "example"})


def of_type(template: Mapping[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def forwards(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each forwarding rule of the template, read as the paths, methods and pool headers it
    matches, and the target groups it sends to (`None` where a condition is absent: anything)."""
    found = []
    for properties in of_type(template, "AWS::ElasticLoadBalancingV2::ListenerRule").values():
        actions = [action for action in properties["Actions"] if action["Type"] == "forward"]
        if not actions:
            continue
        rule: dict[str, Any] = {"paths": None, "methods": None, "pool": None, "listener": properties["ListenerArn"]}
        for condition in properties["Conditions"]:
            field = condition["Field"]
            if field == "path-pattern":
                rule["paths"] = set(condition["PathPatternConfig"]["Values"])
            elif field == "http-request-method":
                rule["methods"] = set(condition["HttpRequestMethodConfig"]["Values"])
            elif field == "http-header" and condition["HttpHeaderConfig"]["HttpHeaderName"] == "X-Rhi-Pool":
                rule["pool"] = set(condition["HttpHeaderConfig"]["Values"])
        rule["targets"] = [action["TargetGroupArn"] for action in actions]
        found.append(rule)
    return found


def forwarded_outside_the_routes(template: Mapping[str, Any]) -> list[str]:
    """Each path of `FORBIDDEN` that a forwarding rule matches: a rule with no path condition
    matches every path, and a pattern's `*` and `?` are wildcards."""
    return [
        path
        for rule in forwards(template)
        for path in FORBIDDEN
        if rule["paths"] is None or any(fnmatchcase(path, pattern) for pattern in rule["paths"])
    ]


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    return templates[path]


def test_one_internal_listener_on_port_80_with_a_default_404(pools: dict[str, Any]) -> None:
    (balancer,) = of_type(pools, "AWS::ElasticLoadBalancingV2::LoadBalancer").values()
    assert balancer["Scheme"] == "internal"
    attributes = {a["Key"]: a["Value"] for a in balancer["LoadBalancerAttributes"]}
    assert attributes["idle_timeout.timeout_seconds"] == "1200"
    # The network stack's: the internal load balancer's group alone, and its private subnets.
    (group,) = balancer["SecurityGroups"]
    assert "SecurityGroupsInternalLoadBalancer" in group["Fn::ImportValue"]
    assert len(balancer["Subnets"]) == 3
    assert all("PrivateSubnet" in subnet["Fn::ImportValue"] for subnet in balancer["Subnets"])
    (listener,) = of_type(pools, "AWS::ElasticLoadBalancingV2::Listener").values()
    assert (listener["Port"], listener["Protocol"]) == (80, "HTTP")
    (default,) = listener["DefaultActions"]
    assert default["Type"] == "fixed-response"
    assert default["FixedResponseConfig"]["StatusCode"] == "404"
    # The listener opens nothing: the group of the network stack decides who reaches it.
    assert of_type(pools, "AWS::EC2::SecurityGroupIngress") == {}


def test_four_post_rules_per_pool_by_its_header(pools: dict[str, Any]) -> None:
    rules = forwards(pools)
    assert {pool.name for pool in EXAMPLE.pools} == {"mock"}
    for pool in EXAMPLE.pools:
        mine = [rule for rule in rules if rule["pool"] == {pool.name}]
        assert len(mine) == 4
        assert set().union(*(rule["paths"] for rule in mine)) == ROUTES
        assert all(len(rule["paths"]) == 1 and rule["methods"] == {"POST"} for rule in mine)
    assert len(rules) == 4 * len(EXAMPLE.pools)
    assert all(rule["pool"] is not None for rule in rules)
    # No rule other than these: nothing redirects or answers in the listener's place.
    assert len(of_type(pools, "AWS::ElasticLoadBalancingV2::ListenerRule")) == len(rules)


def test_the_mock_pool_target_group(pools: dict[str, Any]) -> None:
    (name, group), *others = of_type(pools, "AWS::ElasticLoadBalancingV2::TargetGroup").items()
    assert others == []
    assert {target["Ref"] for rule in forwards(pools) for target in rule["targets"]} == {name}
    assert (group["Port"], group["Protocol"], group["TargetType"]) == (8000, "HTTP", "instance")
    assert (group["HealthCheckPath"], group["HealthCheckPort"]) == ("/health", "8000")
    assert group["HealthCheckIntervalSeconds"] == 5
    attributes = {a["Key"]: a["Value"] for a in group["TargetGroupAttributes"]}
    assert attributes["deregistration_delay.timeout_seconds"] == "300"
    assert attributes["load_balancing.algorithm.type"] == "least_outstanding_requests"
    assert "stickiness.enabled" not in attributes or attributes["stickiness.enabled"] == "false"


def routing(app_module: ModuleType, pools: tuple[Any, ...]) -> Mapping[str, Any]:
    """The template of a stack that holds the routing of these pools and a network for it."""
    from aws_cdk import assertions
    from aws_cdk import aws_ec2 as ec2

    from rhi.pools.routing import Routing

    cdk = app_module.cdk
    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Routing")
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=1)
    group = ec2.SecurityGroup(stack, "Group", vpc=vpc, allow_all_outbound=False)
    Routing(stack, "Routing", vpc=vpc, security_group=group, pools=pools)
    return assertions.Template.from_stack(stack).to_json()


def test_each_pool_has_its_own_rules_target_group_and_drain(app_module: ModuleType) -> None:
    (mock,) = EXAMPLE.pools
    template = routing(app_module, (mock, dataclasses.replace(mock, name="second", drain_seconds=120)))
    groups = of_type(template, "AWS::ElasticLoadBalancingV2::TargetGroup")
    rules = forwards(template)
    drains = {}
    for pool in ("mock", "second"):
        mine = [rule for rule in rules if rule["pool"] == {pool}]
        assert set().union(*(rule["paths"] for rule in mine)) == ROUTES
        assert len(mine) == 4
        # The four send to one group, which is no other pool's.
        (target,) = {target["Ref"] for rule in mine for target in rule["targets"]}
        attributes = {a["Key"]: a["Value"] for a in groups.pop(target)["TargetGroupAttributes"]}
        drains[pool] = attributes["deregistration_delay.timeout_seconds"]
    assert drains == {"mock": "300", "second": "120"}
    assert groups == {}
    assert len(rules) == 8


def test_no_pool_leaves_the_listener_and_its_default_404(app_module: ModuleType) -> None:
    template = routing(app_module, ())
    (listener,) = of_type(template, "AWS::ElasticLoadBalancingV2::Listener").values()
    assert [action["FixedResponseConfig"]["StatusCode"] for action in listener["DefaultActions"]] == ["404"]
    assert of_type(template, "AWS::ElasticLoadBalancingV2::ListenerRule") == {}
    assert of_type(template, "AWS::ElasticLoadBalancingV2::TargetGroup") == {}


def test_no_rule_forwards_the_health_route(pools: dict[str, Any], app_module: ModuleType) -> None:
    assert forwarded_outside_the_routes(pools) == []

    from aws_cdk import assertions
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    cdk = app_module.cdk
    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Routing")
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=0)
    balancer = elbv2.ApplicationLoadBalancer(stack, "LoadBalancer", vpc=vpc)
    listener = balancer.add_listener(
        "Http", port=80, open=False, default_action=elbv2.ListenerAction.fixed_response(404)
    )
    group = elbv2.ApplicationTargetGroup(stack, "Pool", vpc=vpc, port=8000, protocol=elbv2.ApplicationProtocol.HTTP)
    listener.add_action(
        "Health",
        priority=1,
        conditions=[
            elbv2.ListenerCondition.path_patterns(["/health"]),
            elbv2.ListenerCondition.http_header("X-Rhi-Pool", ["mock"]),
        ],
        action=elbv2.ListenerAction.forward([group]),
    )
    assert forwarded_outside_the_routes(assertions.Template.from_stack(stack).to_json()) == ["/health"]
