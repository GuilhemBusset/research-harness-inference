"""A pool's lifecycle hooks (`rhi.pools.hooks`; system design, sections 4.3 and 17): the mock pool's
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
(MOCK,) = EXAMPLE.pools
HOOK = "AWS::AutoScaling::LifecycleHook"


def hooks(template: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The properties of a template's lifecycle hooks, by the hook's name."""
    found = [r["Properties"] for r in template["Resources"].values() if r["Type"] == HOOK]
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


def test_the_mock_pool_has_its_two_hooks_on_its_group(pools: dict[str, Any]) -> None:
    (group,) = [name for name, r in pools["Resources"].items() if r["Type"] == "AWS::AutoScaling::AutoScalingGroup"]
    found = hooks(pools)
    # The names the release parameter gives the agent.
    assert set(found) == {"rhi-launch", "rhi-termination"}
    assert all(hook["AutoScalingGroupName"] == {"Ref": group} for hook in found.values())
    assert found["rhi-launch"]["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_LAUNCHING"
    assert found["rhi-termination"]["LifecycleTransition"] == "autoscaling:EC2_INSTANCE_TERMINATING"


def test_the_launch_hook_waits_15_minutes_with_no_measured_start_then_abandons(pools: dict[str, Any]) -> None:
    assert MOCK.measured_start_seconds is None
    launch = hooks(pools)["rhi-launch"]
    assert launch["HeartbeatTimeout"] == 900
    assert launch["DefaultResult"] == "ABANDON"


def test_the_termination_hook_waits_the_drain_and_60_seconds_then_lets_the_instance_go(pools: dict[str, Any]) -> None:
    assert MOCK.drain_seconds == 300
    termination = hooks(pools)["rhi-termination"]
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
