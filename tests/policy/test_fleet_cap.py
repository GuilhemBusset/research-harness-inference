"""The fleet cap: a pool's group has a minimum of zero, no desired capacity, its pool's maximum and
no scheduled action (system design, sections 4.2, 5.1 and 8.4).

A pool becomes one Auto Scaling group, and the group's maximum size is the first thing that stays
outside the scaler, so that a broken scaler cannot cost money. Schedules, ceilings and stops all
live in the scaler, so that they cannot contradict one another: the group's minimum is always
zero, and nothing in a template scales it or moves its bounds. The scaler is the only writer of
the desired capacity. A desired capacity in the template would reset the pool at every
deployment, a second writer.

A group says which pool it is by its tag `rhi-pool`, and the pool's `max_instances` is read from
the configuration the policy tests synthesize. Every group is a pool's: one without the tag, or
whose tag names no configured pool, is refused. A size counts only when the template writes it as
a number: one built from a parameter or another resource could be anything at deployment.

Every scheduled action is refused, whatever it sets, a desired capacity alone included: it would be
a second writer of the pool's size, at an hour that knows nothing of the sessions. There is no
nightly stop (section 5.1): what ends a forgotten session is its own end.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

POOL_TAG = "rhi-pool"

MINIMUM = "its minimum is not 0"
DESIRED_CAPACITY = "the template sets its desired capacity"
MAXIMUM = "its maximum is not the max_instances of its pool"
UNTAGGED = f"it does not carry one tag {POOL_TAG}"
UNKNOWN_POOL = f"its tag {POOL_TAG} names no configured pool"
SCALING_POLICY = "a scaling policy"
SCHEDULED = "a scheduled action"

GROUP = "AWS::AutoScaling::AutoScalingGroup"
SCHEDULED_ACTION = "AWS::AutoScaling::ScheduledAction"
# What scales a group from outside the scaler: a policy, or a plan, which makes policies and
# moves the group's bounds.
SCALES_A_GROUP = ("AWS::AutoScaling::ScalingPolicy", "AWS::AutoScalingPlans::ScalingPlan")


def number(value: Any) -> int | None:
    """A size as the template writes it, a number or its digits. None for anything else."""
    if type(value) is int:
        return value
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]+", value, re.ASCII) else None


def group_violations(properties: Mapping[str, Any], caps: Mapping[str, int]) -> list[str]:
    """What is refused in one group, given the maximum of each configured pool."""
    found = []
    if number(properties.get("MinSize")) != 0:
        found.append(MINIMUM)
    if "DesiredCapacity" in properties:
        found.append(DESIRED_CAPACITY)
    tags = properties.get("Tags")
    pools = [tag.get("Value") for tag in tags or [] if tag.get("Key") == POOL_TAG] if isinstance(tags, list) else []
    if len(pools) != 1:
        found.append(UNTAGGED)
    elif not isinstance(pools[0], str) or pools[0] not in caps:
        found.append(UNKNOWN_POOL)
    elif number(properties.get("MaxSize")) != caps[pools[0]]:
        found.append(MAXIMUM)
    return found


def violations(templates: Mapping[str, Mapping[str, Any]], caps: Mapping[str, int]) -> list[tuple[str, str, str]]:
    """Every breach of the fleet cap in the templates, as `(template path, logical ID, what)`."""
    found = []
    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            kind = resource.get("Type")
            properties = resource.get("Properties", {})
            if kind == GROUP:
                found += [(path, logical_id, what) for what in group_violations(properties, caps)]
            if kind in SCALES_A_GROUP:
                found.append((path, logical_id, SCALING_POLICY))
            if kind == SCHEDULED_ACTION:
                found.append((path, logical_id, SCHEDULED))
    return found


@pytest.fixture(scope="session")
def caps() -> dict[str, int]:
    """The maximum of each configured pool, in the configuration the policy tests synthesize."""
    return {pool.name: pool.max_instances for pool in config.load({"RHI_CONFIG": "example"}).pools}


def test_every_group_is_capped_by_its_pool(templates: dict[str, dict[str, Any]], caps: dict[str, int]) -> None:
    assert violations(templates, caps) == []


# The pools of the templates built below, and the template's path.
CAPS = {"small": 2, "large": 4}
BUILT = "built.template.json"


def group(cdk: ModuleType, *, tagged: bool = True, **changes: Any) -> Any:
    """A group of the pool `small`, built by the CDK in a stack of its own, with the changes."""
    tag = cdk.aws_autoscaling.CfnAutoScalingGroup.TagPropertyProperty
    tags = [tag(key="Name", value="fleet", propagate_at_launch=True)]
    if tagged:
        tags.append(tag(key=POOL_TAG, value="small", propagate_at_launch=True))
    properties = {"min_size": "0", "max_size": str(CAPS["small"]), "tags": tags} | changes
    return cdk.aws_autoscaling.CfnAutoScalingGroup(cdk.Stack(), "Group", **properties)


def found(cdk: ModuleType, construct: Any) -> list[tuple[str, str]]:
    """What is refused in the template of the construct's stack, as `(logical ID, what)`."""
    template = cdk.assertions.Template.from_stack(cdk.Stack.of(construct)).to_json()
    return [(logical_id, what) for _, logical_id, what in violations({BUILT: template}, CAPS)]


def test_detects_a_raised_cap(cdk: ModuleType) -> None:
    assert found(cdk, group(cdk)) == []
    assert found(cdk, group(cdk, max_size=str(CAPS["small"] + 1))) == [("Group", MAXIMUM)]


def test_detects_a_desired_capacity(cdk: ModuleType) -> None:
    assert found(cdk, group(cdk)) == []
    # Even zero: the next deployment would write it over the scaler's.
    assert found(cdk, group(cdk, desired_capacity="0")) == [("Group", DESIRED_CAPACITY)]
    assert found(cdk, group(cdk, desired_capacity="1")) == [("Group", DESIRED_CAPACITY)]


def test_detects_a_scheduled_action(cdk: ModuleType) -> None:
    def scheduled(**sets: int) -> Any:
        """A group with a scheduled action that sets what is given."""
        pool = group(cdk)
        action = cdk.aws_autoscaling.CfnScheduledAction
        action(pool.stack, "Action", auto_scaling_group_name=pool.ref, recurrence="0 3 * * *", **sets)
        return pool

    assert found(cdk, group(cdk)) == []
    # Even one that sets only a desired capacity, and that to zero, as the nightly stop did.
    assert found(cdk, scheduled(desired_capacity=0)) == [("Action", SCHEDULED)]
    assert found(cdk, scheduled(min_size=1)) == [("Action", SCHEDULED)]


def test_detects_an_untagged_group(cdk: ModuleType) -> None:
    assert found(cdk, group(cdk)) == []
    assert found(cdk, group(cdk, tagged=False)) == [("Group", UNTAGGED)]


def by_hand(**changes: Any) -> dict[str, Any]:
    """A group of the pool `small`, written by hand, with the changes to its properties."""
    properties = {"MinSize": "0", "MaxSize": "2", "Tags": [{"Key": POOL_TAG, "Value": "small"}]} | changes
    return {"Type": GROUP, "Properties": properties}


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        pytest.param({"Group": by_hand(MinSize="1")}, [("Group", MINIMUM)], id="a minimum above 0"),
        pytest.param(
            {"Group": by_hand(MinSize={"Ref": "Minimum"})}, [("Group", MINIMUM)], id="a minimum of a parameter"
        ),
        pytest.param({"Group": by_hand(MaxSize=1)}, [("Group", MAXIMUM)], id="a maximum below the pool's"),
        pytest.param(
            {"Group": by_hand(MaxSize={"Ref": "Maximum"})}, [("Group", MAXIMUM)], id="a maximum of a parameter"
        ),
        pytest.param(
            {"Group": by_hand(MaxSize="4")}, [("Group", MAXIMUM)], id="the maximum of another pool than the tag's"
        ),
        pytest.param(
            {"Group": by_hand(Tags=[{"Key": POOL_TAG, "Value": "unknown"}])},
            [("Group", UNKNOWN_POOL)],
            id="a tag that names no configured pool",
        ),
        pytest.param(
            {"Group": by_hand(Tags=[{"Key": POOL_TAG, "Value": {"Ref": "Pool"}}])},
            [("Group", UNKNOWN_POOL)],
            id="a tag of a parameter",
        ),
        pytest.param(
            {"Group": by_hand(), "Policy": {"Type": "AWS::AutoScaling::ScalingPolicy", "Properties": {}}},
            [("Policy", SCALING_POLICY)],
            id="a scaling policy",
        ),
        pytest.param(
            {"Group": by_hand(), "Plan": {"Type": "AWS::AutoScalingPlans::ScalingPlan", "Properties": {}}},
            [("Plan", SCALING_POLICY)],
            id="a scaling plan",
        ),
        pytest.param(
            {"Group": by_hand(), "Action": {"Type": SCHEDULED_ACTION, "Properties": {"MaxSize": 8}}},
            [("Action", SCHEDULED)],
            id="a scheduled maximum",
        ),
        pytest.param(
            {"Group": by_hand(), "Action": {"Type": SCHEDULED_ACTION, "Properties": {"DesiredCapacity": 0}}},
            [("Action", SCHEDULED)],
            id="a scheduled desired capacity",
        ),
        pytest.param(
            {"Group": by_hand(), "Action": {"Type": SCHEDULED_ACTION}},
            [("Action", SCHEDULED)],
            id="a scheduled action that sets nothing",
        ),
        pytest.param(
            {"Group": by_hand(MinSize="1", MaxSize="3", DesiredCapacity="1")},
            [("Group", MINIMUM), ("Group", DESIRED_CAPACITY), ("Group", MAXIMUM)],
            id="every breach of one group",
        ),
    ],
)
def test_detects_each_breach(resources: dict[str, Any], expected: list[tuple[str, str]]) -> None:
    assert violations({BUILT: {"Resources": {"Group": by_hand()}}}, CAPS) == []
    assert [
        (logical_id, what) for _, logical_id, what in violations({BUILT: {"Resources": resources}}, CAPS)
    ] == expected
