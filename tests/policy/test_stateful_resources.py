"""The stateful-resource invariant: each kind of state keeps its protections and its logical ID
(system design, sections 8.4 and 8.5).

There is one environment, so a replaced table is lost state: a rename by a coding agent would
replace the key table and void every student's key mid-course. Each kind of state the stage holds
keeps the protections its type has, and every stateful resource is recorded, by its stack and its
logical ID, in `stateful_logical_ids.json`. A resource that is not recorded for its stack, which is
what a renamed construct becomes, fails, and so does a recorded resource that the stage no longer
holds. Each issue that adds state records it.

The invariant reads the stage's templates: the stacks of the app's nested assemblies, by their
stack name. The pipeline's own stack is a stack of the app's top assembly, and its artifact bucket
is CDK Pipelines'.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
RECORD = Path(__file__).resolve().parent / "stateful_logical_ids.json"

POINT_IN_TIME_RECOVERY = "point-in-time recovery"
DELETION_PROTECTION = "deletion protection"
RETAIN_ON_DELETION = "retain on deletion"
RETAIN_ON_REPLACEMENT = "retain on replacement"

NOT_RECORDED = "not recorded for its stack"
NO_LONGER_HELD = "recorded, and no longer held by its stack"

Resource = dict[str, Any]


def enabled(value: Any) -> bool:
    return value is True or value == "true"


def table_recovery(resource: Resource) -> bool:
    specification = resource.get("Properties", {}).get("PointInTimeRecoverySpecification") or {}
    return enabled(specification.get("PointInTimeRecoveryEnabled"))


def table_deletion_protection(resource: Resource) -> bool:
    return enabled(resource.get("Properties", {}).get("DeletionProtectionEnabled"))


def replicas(resource: Resource) -> list[Resource]:
    return resource.get("Properties", {}).get("Replicas") or []


def global_table_recovery(resource: Resource) -> bool:
    """A global table (CDK's `TableV2`) carries its protections on each replica."""
    return bool(replicas(resource)) and all(
        enabled((replica.get("PointInTimeRecoverySpecification") or {}).get("PointInTimeRecoveryEnabled"))
        for replica in replicas(resource)
    )


def global_table_deletion_protection(resource: Resource) -> bool:
    return bool(replicas(resource)) and all(
        enabled(replica.get("DeletionProtectionEnabled")) for replica in replicas(resource)
    )


def user_pool_deletion_protection(resource: Resource) -> bool:
    return resource.get("Properties", {}).get("DeletionProtection") == "ACTIVE"


def retained_on_deletion(resource: Resource) -> bool:
    return resource.get("DeletionPolicy") == "Retain"


def retained_on_replacement(resource: Resource) -> bool:
    return resource.get("UpdateReplacePolicy") == "Retain"


RETAINED = {RETAIN_ON_DELETION: retained_on_deletion, RETAIN_ON_REPLACEMENT: retained_on_replacement}

# The kinds of state, by CloudFormation type, each with the protections its type has (the issue's
# table): a protection's name and whether a resource has it.
STATEFUL: dict[str, dict[str, Callable[[Resource], bool]]] = {
    "AWS::DynamoDB::Table": {
        POINT_IN_TIME_RECOVERY: table_recovery,
        DELETION_PROTECTION: table_deletion_protection,
        **RETAINED,
    },
    "AWS::DynamoDB::GlobalTable": {
        POINT_IN_TIME_RECOVERY: global_table_recovery,
        DELETION_PROTECTION: global_table_deletion_protection,
        **RETAINED,
    },
    # A Managed Prometheus workspace has neither point-in-time recovery nor deletion protection.
    "AWS::APS::Workspace": dict(RETAINED),
    "AWS::S3::Bucket": dict(RETAINED),
    "AWS::Cognito::UserPool": {DELETION_PROTECTION: user_pool_deletion_protection, **RETAINED},
}


def violations(
    stacks: Mapping[str, Mapping[str, Any]], recorded: Mapping[str, list[str]]
) -> list[tuple[str, str, str]]:
    """Every breach of the invariant, as `(stack name, logical ID, what)`, given the stage's
    templates by stack name and the record."""
    found = []
    held: set[tuple[str, str]] = set()
    for stack, template in stacks.items():
        for logical_id, resource in template.get("Resources", {}).items():
            protections = STATEFUL.get(resource.get("Type"))
            if protections is None:
                continue
            held.add((stack, logical_id))
            found += [
                (stack, logical_id, f"lacks {protection}")
                for protection, has in protections.items()
                if not has(resource)
            ]
            if logical_id not in recorded.get(stack, []):
                found.append((stack, logical_id, NOT_RECORDED))
    found += [
        (stack, logical_id, NO_LONGER_HELD)
        for stack, logical_ids in recorded.items()
        for logical_id in logical_ids
        if (stack, logical_id) not in held
    ]
    return found


def stage_stacks(assembly: Any) -> dict[str, dict[str, Any]]:
    """The template of every stack of the app's stages (its nested assemblies), by stack name."""
    return {
        stack.stack_name: stack.template
        for nested in assembly.nested_assemblies
        for stack in nested.nested_assembly.stacks_recursively
    }


def read_record(path: Path) -> dict[str, list[str]]:
    record = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(record, dict), f"{path.name} is not an object of stacks"
    for stack, logical_ids in record.items():
        assert isinstance(logical_ids, list), f"{path.name}: {stack} is not a list of logical IDs"
        assert all(isinstance(logical_id, str) for logical_id in logical_ids), f"{path.name}: {stack}"
    return record


@pytest.fixture(scope="module")
def stage(cdk: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """The stage's templates, by stack name, from the app of `app.py` built as `conftest.py`
    builds it: the example configuration, the context of `cdk.json`, nothing bundled."""
    spec = importlib.util.spec_from_file_location("app", REPO / "app.py")
    assert spec is not None and spec.loader is not None
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    outdir = tmp_path_factory.mktemp("cdk.out")
    app = app_module.build(config.load({"RHI_CONFIG": "example"}), outdir=str(outdir), context=context)
    stacks = stage_stacks(app.synth())
    assert stacks, "the app has no stage stack"
    return stacks


def test_the_stage_keeps_its_state(stage: dict[str, dict[str, Any]]) -> None:
    assert violations(stage, read_record(RECORD)) == []


# The stack that holds the state built below, in the stage `Stage`.
STACK = "Stage-State"
# The kinds of state built below, and the CloudFormation type of each.
KINDS = {
    "table": "AWS::DynamoDB::Table",
    "global table": "AWS::DynamoDB::GlobalTable",
    "workspace": "AWS::APS::Workspace",
    "bucket": "AWS::S3::Bucket",
    "user pool": "AWS::Cognito::UserPool",
}


def build(cdk: ModuleType, kind: str, *, construct_id: str = "State", without: str | None = None) -> Any:
    """A stage with one stack that holds one resource of the kind, with every protection of its
    type but `without`, and its cloud assembly. Beside the stage, a stack of the app's top
    assembly holds an unprotected bucket, as the pipeline's own stack holds its artifact bucket."""
    app = cdk.App(analytics_reporting=False)
    cdk.aws_s3.Bucket(cdk.Stack(app, "Pipeline"), "Artifacts", removal_policy=cdk.RemovalPolicy.DESTROY)
    stack = cdk.Stack(cdk.Stage(app, "Stage"), "State")
    retain = cdk.RemovalPolicy.RETAIN
    recovery = {
        "point_in_time_recovery_specification": {"point_in_time_recovery_enabled": without != POINT_IN_TIME_RECOVERY}
    }
    protected = without != DELETION_PROTECTION
    key = cdk.aws_dynamodb.Attribute(name="id", type=cdk.aws_dynamodb.AttributeType.STRING)
    if kind == "table":
        state = cdk.aws_dynamodb.Table(
            stack, construct_id, partition_key=key, deletion_protection=protected, removal_policy=retain, **recovery
        )
    elif kind == "global table":
        state = cdk.aws_dynamodb.TableV2(
            stack, construct_id, partition_key=key, deletion_protection=protected, removal_policy=retain, **recovery
        )
    elif kind == "workspace":
        state = cdk.aws_aps.CfnWorkspace(stack, construct_id)
        state.apply_removal_policy(retain)
    elif kind == "bucket":
        state = cdk.aws_s3.Bucket(stack, construct_id, removal_policy=retain)
    else:
        state = cdk.aws_cognito.UserPool(stack, construct_id, deletion_protection=protected, removal_policy=retain)
    options = (state if isinstance(state, cdk.CfnResource) else state.node.default_child).cfn_options
    if without == RETAIN_ON_DELETION:
        options.deletion_policy = cdk.CfnDeletionPolicy.DELETE
    if without == RETAIN_ON_REPLACEMENT:
        options.update_replace_policy = cdk.CfnDeletionPolicy.DELETE
    return app.synth()


def state_of(stacks: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The logical IDs of the stateful resources of the stack `STACK`."""
    resources = stacks[STACK]["Resources"]
    return [logical_id for logical_id, resource in resources.items() if resource["Type"] in STATEFUL]


@pytest.mark.parametrize(
    ("kind", "protection"),
    [
        pytest.param(kind, protection, id=f"{kind}: {protection}")
        for kind, kind_type in KINDS.items()
        for protection in STATEFUL[kind_type]
    ],
)
def test_detects_a_missing_protection(cdk: ModuleType, kind: str, protection: str) -> None:
    protected = stage_stacks(build(cdk, kind))
    [logical_id] = state_of(protected)
    assert protected[STACK]["Resources"][logical_id]["Type"] == KINDS[kind]
    recorded = {STACK: [logical_id]}
    assert violations(protected, recorded) == []
    unprotected = stage_stacks(build(cdk, kind, without=protection))
    assert violations(unprotected, recorded) == [(STACK, logical_id, f"lacks {protection}")]


def test_detects_a_rename(cdk: ModuleType) -> None:
    before = stage_stacks(build(cdk, "table", construct_id="Keys"))
    [recorded_id] = state_of(before)
    recorded = {STACK: [recorded_id]}
    assert violations(before, recorded) == []
    after = stage_stacks(build(cdk, "table", construct_id="StudentKeys"))
    [renamed_id] = state_of(after)
    assert violations(after, recorded) == [(STACK, renamed_id, NOT_RECORDED), (STACK, recorded_id, NO_LONGER_HELD)]


@pytest.mark.parametrize("kind", KINDS)
def test_detects_an_unrecorded_resource(cdk: ModuleType, kind: str) -> None:
    stacks = stage_stacks(build(cdk, kind))
    [logical_id] = state_of(stacks)
    assert violations(stacks, {}) == [(STACK, logical_id, NOT_RECORDED)]
    # Recorded for another stack is not recorded for its own.
    assert violations(stacks, {"Stage-Other": [logical_id]}) == [
        (STACK, logical_id, NOT_RECORDED),
        ("Stage-Other", logical_id, NO_LONGER_HELD),
    ]


def test_detects_a_recorded_resource_the_stage_no_longer_holds(cdk: ModuleType) -> None:
    with_table = stage_stacks(build(cdk, "table"))
    [logical_id] = state_of(with_table)
    recorded = {STACK: [logical_id]}
    assert violations(with_table, recorded) == []
    without_table = stage_stacks(build(cdk, "bucket", construct_id="Other"))
    [bucket] = state_of(without_table)
    assert violations(without_table, {STACK: [logical_id, bucket]}) == [(STACK, logical_id, NO_LONGER_HELD)]
    assert violations(with_table, recorded | {"Stage-Gone": [logical_id]}) == [
        ("Stage-Gone", logical_id, NO_LONGER_HELD)
    ]
