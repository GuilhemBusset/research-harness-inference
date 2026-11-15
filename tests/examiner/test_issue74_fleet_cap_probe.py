"""Independent synthesis witnesses for the fleet-cap invariant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType


def load_policy(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "issue74_" + filename.removesuffix(".py"), Path(__file__).resolve().parents[1] / "policy" / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load_policy("test_fleet_cap.py")
inputs = load_policy("conftest.py")
cdk = inputs.cdk
synthesized = inputs.synthesized
templates = inputs.templates

CAPS = {"alpha": 3, "beta": 7}


def stack_with_group(cdk: ModuleType, pool: str = "alpha") -> tuple[Any, Any]:
    stack = cdk.Stack()
    group = cdk.aws_autoscaling.CfnAutoScalingGroup(
        stack,
        "Fleet",
        min_size="0",
        max_size=str(CAPS[pool]),
        tags=[{"key": "rhi-pool", "value": pool, "propagateAtLaunch": True}],
    )
    return stack, group


def check(cdk: ModuleType, stack: Any) -> list[tuple[str, str, str]]:
    template = cdk.assertions.Template.from_stack(stack).to_json()
    return policy.violations({"independent.template.json": template}, CAPS)


def test_current_app(templates: dict[str, dict[str, Any]]) -> None:
    caps = {pool.name: pool.max_instances for pool in config.load({"RHI_CONFIG": "example"}).pools}
    assert templates
    assert policy.violations(templates, caps) == []


@pytest.mark.parametrize("pool", ["alpha", "beta"])
def test_valid_pool_and_nightly_stop(cdk: ModuleType, pool: str) -> None:
    stack, group = stack_with_group(cdk, pool)
    cdk.aws_autoscaling.CfnScheduledAction(
        stack, "Nightly", auto_scaling_group_name=group.ref, desired_capacity=0, recurrence="0 2 * * *"
    )
    assert check(cdk, stack) == []


@pytest.mark.parametrize(
    ("property_name", "value"),
    [("MinSize", "1"), ("DesiredCapacity", "0"), ("DesiredCapacity", "2"), ("MaxSize", "4"), ("MaxSize", "2")],
)
def test_group_breaches(cdk: ModuleType, property_name: str, value: str) -> None:
    clean, _ = stack_with_group(cdk)
    assert check(cdk, clean) == []
    stack, group = stack_with_group(cdk)
    group.add_property_override(property_name, value)
    breaches = check(cdk, stack)
    assert len(breaches) == 1
    assert breaches[0][:2] == ("independent.template.json", "Fleet")


@pytest.mark.parametrize("tags", [[], [{"Key": "rhi-pool", "Value": "absent", "PropagateAtLaunch": True}]])
def test_missing_or_unknown_pool(cdk: ModuleType, tags: list[dict[str, Any]]) -> None:
    stack, group = stack_with_group(cdk)
    group.add_property_override("Tags", tags)
    assert len(check(cdk, stack)) == 1


@pytest.mark.parametrize("bound", ["MinSize", "MaxSize"])
def test_scheduled_bounds(cdk: ModuleType, bound: str) -> None:
    stack, group = stack_with_group(cdk)
    action = cdk.aws_autoscaling.CfnScheduledAction(
        stack, "Scheduled", auto_scaling_group_name=group.ref, recurrence="0 2 * * *"
    )
    action.add_property_override(bound, 1)
    breaches = check(cdk, stack)
    assert len(breaches) == 1
    assert breaches[0][:2] == ("independent.template.json", "Scheduled")


def test_scaling_policy(cdk: ModuleType) -> None:
    stack, group = stack_with_group(cdk)
    cdk.aws_autoscaling.CfnScalingPolicy(
        stack, "Scaling", auto_scaling_group_name=group.ref, adjustment_type="ChangeInCapacity", scaling_adjustment=1
    )
    breaches = check(cdk, stack)
    assert len(breaches) == 1
    assert breaches[0][:2] == ("independent.template.json", "Scaling")
