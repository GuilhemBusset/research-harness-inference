"""A pool's lifecycle hooks (`rhi.pools.hooks`; system design, sections 4.3 and 17): each pool's
launch and termination hooks in the app, and the launch hook's timeout once a start is measured.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    import aws_cdk as cdk

    from rhi_common.pool_config import Pool

EXAMPLE = config.load({"RHI_CONFIG": "example"})
MOCK = next(pool for pool in EXAMPLE.pools if pool.name == "mock")
HOOK = "AWS::AutoScaling::LifecycleHook"


def group_of(template: Mapping[str, Any], pool: Pool) -> str:
    """The logical ID of the group a template tags with this pool's name."""
    (group,) = [
        name
        for name, r in template["Resources"].items()
        if r["Type"] == "AWS::AutoScaling::AutoScalingGroup"
        and [tag["Value"] for tag in r["Properties"]["Tags"] if tag["Key"] == "rhi-pool"] == [pool.name]
    ]
    return group


def of_kind(template: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [r["Properties"] for r in template["Resources"].values() if r["Type"] == kind]


def hooks(template: Mapping[str, Any], pool: Pool | None = None) -> dict[str, dict[str, Any]]:
    """The properties of a template's lifecycle hooks, by the hook's name: those on this pool's
    group, or every hook of a template that holds one group."""
    found = of_kind(template, HOOK)
    if pool is not None:
        found = [hook for hook in found if hook["AutoScalingGroupName"] == {"Ref": group_of(template, pool)}]
    by_name = {properties["LifecycleHookName"]: properties for properties in found}
    assert len(by_name) == len(found)
    return by_name


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    return templates[path]


def standalone(app_module: ModuleType, pool: Pool) -> Mapping[str, Any]:
    """The template of a stack that holds only a group and this pool's hooks on it."""
    from aws_cdk import assertions
    from aws_cdk import aws_autoscaling as autoscaling

    from rhi.pools.hooks import PoolHooks

    cdk = app_module.cdk
    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Hooks")
    group = autoscaling.CfnAutoScalingGroup(stack, "Group", min_size="0", max_size=str(pool.max_instances))
    PoolHooks(stack, "Hooks", pool=pool, group=group)
    return assertions.Template.from_stack(stack).to_json()


def test_each_pool_of_the_example_has_its_two_hooks_on_its_group(pools: dict[str, Any]) -> None:
    for pool in EXAMPLE.pools:
        found = hooks(pools, pool)
        # The names the release parameter gives the agent.
        assert set(found) == {"rhi-launch", "rhi-termination"}, pool.name
        assert found["rhi-launch"]["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_LAUNCHING"
        assert found["rhi-termination"]["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_TERMINATING"
    # Every hook of the stack is on the group of one of them.
    assert len(of_kind(pools, HOOK)) == 2 * len(EXAMPLE.pools)


def test_the_launch_hook_waits_15_minutes_with_no_measured_start_then_abandons(pools: dict[str, Any]) -> None:
    # Neither pool of the example has a measured start: the coursework pool's is spike S1's to give.
    for pool in EXAMPLE.pools:
        assert pool.measured_start_seconds is None
        launch = hooks(pools, pool)["rhi-launch"]
        assert launch["HeartbeatTimeout"] == 900
        assert launch["DefaultResult"] == "ABANDON"


def test_the_termination_hook_waits_the_drain_and_60_seconds_then_lets_the_instance_go(pools: dict[str, Any]) -> None:
    for pool in EXAMPLE.pools:
        assert pool.drain_seconds == 300
        termination = hooks(pools, pool)["rhi-termination"]
        assert termination["HeartbeatTimeout"] == 360
        assert termination["DefaultResult"] == "CONTINUE"


@pytest.mark.parametrize(("measured", "timeout"), [(600, 1200), (300, 900)])
def test_the_launch_hook_waits_the_longer_of_15_minutes_and_twice_a_measured_start(
    app_module: ModuleType, measured: int, timeout: int
) -> None:
    found = hooks(standalone(app_module, dataclasses.replace(MOCK, measured_start_seconds=measured)))
    assert found["rhi-launch"]["HeartbeatTimeout"] == timeout
    assert found["rhi-launch"]["DefaultResult"] == "ABANDON"
    # A measured start changes nothing of the termination hook.
    assert found["rhi-termination"]["HeartbeatTimeout"] == 360
