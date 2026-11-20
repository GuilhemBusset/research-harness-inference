"""Student affinity on each pool's target group (system design, section 5.5): duration-based
stickiness with the load balancer's own cookie, for the pool's `stickiness_seconds`, beside least
outstanding requests.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    import aws_cdk as cdk

EXAMPLE = config.load({"RHI_CONFIG": "example"})
MOCK = next(pool for pool in EXAMPLE.pools if pool.name == "mock")


def affinity(template: Mapping[str, Any]) -> list[dict[str, str]]:
    """The stickiness and routing attributes of each target group of the template."""
    return [
        {
            attribute["Key"]: attribute["Value"]
            for attribute in resource["Properties"]["TargetGroupAttributes"]
            if attribute["Key"].startswith(("stickiness.", "load_balancing.algorithm."))
        }
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
    ]


def sticky_for(seconds: int) -> dict[str, str]:
    """What a target group has for a pool of that duration, and nothing else: no application
    cookie, so the cookie is the load balancer's own."""
    return {
        "stickiness.enabled": "true",
        "stickiness.type": "lb_cookie",
        "stickiness.lb_cookie.duration_seconds": str(seconds),
        "load_balancing.algorithm.type": "least_outstanding_requests",
    }


def test_every_pool_of_the_example_keeps_a_student_for_an_hour(
    app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]
) -> None:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    assert [(pool.name, pool.stickiness_seconds) for pool in EXAMPLE.pools] == [("coursework", 3600), ("mock", 3600)]
    assert affinity(templates[path]) == [sticky_for(pool.stickiness_seconds) for pool in EXAMPLE.pools]
    assert affinity(templates[path]) == [sticky_for(3600), sticky_for(3600)]


def test_each_pool_gets_its_own_duration(app_module: ModuleType) -> None:
    from aws_cdk import assertions
    from aws_cdk import aws_ec2 as ec2

    from rhi.pools.routing import Routing

    cdk = app_module.cdk
    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Routing")
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=1)
    routing = Routing(
        stack,
        "Routing",
        vpc=vpc,
        security_group=ec2.SecurityGroup(stack, "Internal", vpc=vpc),
        pools=(MOCK, dataclasses.replace(MOCK, name="short", stickiness_seconds=600)),
    )
    template = assertions.Template.from_stack(stack).to_json()
    assert len(affinity(template)) == 2
    by_pool = {
        name: affinity({"Resources": {"Group": template["Resources"][stack.get_logical_id(group.node.default_child)]}})
        for name, group in routing.target_groups.items()
    }
    assert by_pool == {"mock": [sticky_for(3600)], "short": [sticky_for(600)]}
