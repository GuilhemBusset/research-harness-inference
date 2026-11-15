"""No wildcard IAM in an identity policy of the app (system design, section 8.4, control 2).

A merge to `main` is deployment rights in the account, so no policy a merge deploys may allow
every action, and none may allow an action on every resource unless AWS grants that action no
other way and the module that created the policy says so, with its reason, in its file of
`acknowledged/`.

What is read: the identity policies, which are `AWS::IAM::Policy`, `AWS::IAM::ManagedPolicy` and
the inline policies of `AWS::IAM::Role`, and the same of a user or a group. A resource policy,
such as a KMS key policy, names its own resource as `*` and is not read. An action with a wildcard
in its name (`s3:GetObject*`) is not every action: only `*` is.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType

    Templates = dict[str, dict[str, Any]]
    CreatedBy = dict[tuple[str, str], str]
    Acknowledged = dict[str, dict[str, Any]]

# The resources whose `PolicyDocument` is an identity policy, and those that hold inline ones.
POLICIES = (
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
    "AWS::IAM::RolePolicy",
    "AWS::IAM::UserPolicy",
    "AWS::IAM::GroupPolicy",
)
WITH_INLINE_POLICIES = ("AWS::IAM::Role", "AWS::IAM::User", "AWS::IAM::Group")

EVERY_ACTION = "allows every action"
NOT_WRITTEN_OUT = "holds a statement the test cannot read"


def on_every_resource(action: str) -> str:
    return f"allows {action} on every resource"


class Finding(NamedTuple):
    template: str
    logical_id: str
    problem: str


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def statements(resource: Mapping[str, Any]) -> list[Any]:
    """The statements of the identity policies a resource holds. What is not written out as a
    statement, such as a condition function in place of a document, is given as it is."""
    properties = resource.get("Properties", {})
    if resource.get("Type") in POLICIES:
        documents = [properties.get("PolicyDocument")]
    elif resource.get("Type") in WITH_INLINE_POLICIES:
        documents = [
            policy.get("PolicyDocument") if isinstance(policy, dict) else policy
            for policy in as_list(properties.get("Policies", []))
        ]
    else:
        return []
    return [
        statement
        for document in documents
        for statement in (as_list(document["Statement"]) if isinstance(document, dict) else [document])
    ]


def problems(statement: Any, admitted: Mapping[str, Any]) -> list[str]:
    """What one statement allows that it may not. `admitted` is the `[wildcard_actions]` table of
    the module that created the resource holding the statement."""
    if not isinstance(statement, dict) or "Effect" not in statement:
        return [NOT_WRITTEN_OUT]
    if statement.get("Effect") == "Deny":
        return []
    if "Action" not in statement:
        # `NotAction`: every action but those it names.
        return [EVERY_ACTION]
    actions = as_list(statement["Action"])
    found = []
    if "*" in actions:
        found.append(EVERY_ACTION)
    if not all(isinstance(action, str) for action in actions):
        found.append(NOT_WRITTEN_OUT)
    # A statement with no `Resource` has `NotResource`: every resource but those it names.
    if "*" in as_list(statement.get("Resource", "*")):
        found.extend(
            on_every_resource(action)
            for action in actions
            if isinstance(action, str) and action != "*" and action not in admitted
        )
    return found


def violations(templates: Templates, created_by: CreatedBy, acknowledged: Acknowledged) -> list[Finding]:
    """Every wildcard of the templates' identity policies that no acknowledgement admits."""
    return [
        Finding(path, logical_id, problem)
        for path, template in templates.items()
        for logical_id, resource in template.get("Resources", {}).items()
        for statement in statements(resource)
        for problem in problems(
            statement, acknowledged.get(created_by[path, logical_id], {}).get("wildcard_actions", {})
        )
    ]


def test_no_identity_policy_of_the_app_holds_a_wildcard(
    templates: Templates, created_by: CreatedBy, acknowledged: Acknowledged
) -> None:
    assert violations(templates, created_by, acknowledged) == []


def test_the_app_is_read_whole(templates: Templates, created_by: CreatedBy, module_names: set[str]) -> None:
    # The stage's assembly is nested in the app's: its templates are read too.
    assert any("/" in path for path in templates)
    resources = {
        (path, logical_id) for path, template in templates.items() for logical_id in template.get("Resources", {})
    }
    assert resources == set(created_by)
    assert set(created_by.values()) <= module_names


# One statement in each kind of identity policy the issue names.
HOLDERS = {
    "a policy": lambda statement: {
        "Type": "AWS::IAM::Policy",
        "Properties": {"PolicyName": "Policy", "PolicyDocument": {"Statement": [statement]}, "Roles": ["Role"]},
    },
    "a managed policy": lambda statement: {
        "Type": "AWS::IAM::ManagedPolicy",
        "Properties": {"PolicyDocument": {"Statement": statement}},
    },
    "an inline policy of a role": lambda statement: {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": {"Statement": []},
            "Policies": [{"PolicyName": "Inline", "PolicyDocument": {"Statement": [statement]}}],
        },
    },
}
BUCKET = {"Fn::GetAtt": ["Bucket", "Arn"]}
DESCRIBE = "ec2:DescribeInstances"


def found_in(resource: dict[str, Any], admitted: Mapping[str, str] | None = None) -> list[str]:
    """The problems of one resource, built in the test and created by the module `stage`."""
    acknowledged = {"stage": {"wildcard_actions": admitted or {}}}
    templates = {"built.template.json": {"Resources": {"Built": resource}}}
    return [
        finding.problem for finding in violations(templates, {("built.template.json", "Built"): "stage"}, acknowledged)
    ]


@pytest.mark.parametrize("holder", HOLDERS)
@pytest.mark.parametrize(
    ("statement", "admitted", "expected"),
    [
        pytest.param({"Effect": "Allow", "Action": "*", "Resource": BUCKET}, {}, [EVERY_ACTION], id="every action"),
        pytest.param(
            {"Effect": "Allow", "Action": ["s3:GetObject", "*"], "Resource": "*"},
            {"*": "No reason admits every action.", "s3:GetObject": "It is listed for the test."},
            [EVERY_ACTION],
            id="every action, although its module lists it",
        ),
        pytest.param(
            {"Effect": "Allow", "NotAction": "iam:*", "Resource": BUCKET},
            {},
            [EVERY_ACTION],
            id="every action but some",
        ),
        pytest.param(
            {"Effect": "Allow", "Action": DESCRIBE, "Resource": "*"},
            {},
            [on_every_resource(DESCRIBE)],
            id="an action on every resource",
        ),
        pytest.param(
            {"Effect": "Allow", "Action": [DESCRIBE, "ec2:DescribeImages"], "Resource": [BUCKET, "*"]},
            {DESCRIBE: "AWS grants it only on every resource."},
            [on_every_resource("ec2:DescribeImages")],
            id="an action on every resource beside an acknowledged one",
        ),
        pytest.param(
            {"Effect": "Allow", "Action": DESCRIBE, "NotResource": BUCKET},
            {},
            [on_every_resource(DESCRIBE)],
            id="an action on every resource but some",
        ),
        pytest.param(
            {"Fn::If": ["Condition", {}, {}]}, {}, [NOT_WRITTEN_OUT], id="a statement that is not written out"
        ),
    ],
)
def test_detects_a_wildcard(
    holder: str, statement: dict[str, Any], admitted: dict[str, str], expected: list[str]
) -> None:
    assert found_in(HOLDERS[holder](statement), admitted) == expected


@pytest.mark.parametrize(
    "resource",
    [
        pytest.param(
            HOLDERS["a policy"]({"Effect": "Allow", "Action": ["s3:GetObject*", "s3:List*"], "Resource": BUCKET}),
            id="actions with a wildcard in their names, on one resource",
        ),
        pytest.param(HOLDERS["a policy"]({"Effect": "Deny", "Action": "*", "Resource": "*"}), id="a denial"),
        pytest.param(
            {
                "Type": "AWS::KMS::Key",
                "Properties": {"KeyPolicy": {"Statement": [{"Effect": "Allow", "Action": "kms:*", "Resource": "*"}]}},
            },
            id="a resource policy",
        ),
    ],
)
def test_admits_what_is_not_a_wildcard(resource: dict[str, Any]) -> None:
    assert found_in(resource) == []


# The source tree of an app built in the test: a construct module in a folder, and the stage.
MODULES = {
    "pools/group.py": "from constructs import Construct\n\n\nclass Pool(Construct):\n    pass\n",
    "stage.py": "import aws_cdk as cdk\n\n\nclass Endpoint(cdk.Stage):\n    pass\n",
    "pipeline/stack.py": "import aws_cdk as cdk\n\n\nclass Pipeline(cdk.Stack):\n    pass\n",
}


def load(source: Path, module: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Writes one module of `MODULES` under `source` and imports it from there."""
    path = source / module
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MODULES[module], encoding="utf-8")
    name = "built_in_the_test_" + module.removesuffix(".py").replace("/", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    # A class names its module, and the module names its file.
    monkeypatch.setitem(sys.modules, name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


def describing_role(cdk: ModuleType, scope: Any) -> None:
    """A role whose default policy allows one action on every resource."""
    role = cdk.aws_iam.Role(scope, "Role", assumed_by=cdk.aws_iam.ServicePrincipal("ec2.amazonaws.com"))
    role.add_to_policy(cdk.aws_iam.PolicyStatement(actions=[DESCRIBE], resources=["*"]))


def default_policies(templates: Templates, created_by: CreatedBy) -> dict[str, tuple[str, str]]:
    """Where the one `AWS::IAM::Policy` each module created is, by module."""
    policies = {
        module: resource
        for resource, module in created_by.items()
        if templates[resource[0]]["Resources"][resource[1]]["Type"] == "AWS::IAM::Policy"
    }
    assert len(policies) == sum(
        resource["Type"] == "AWS::IAM::Policy"
        for template in templates.values()
        for resource in template["Resources"].values()
    )
    return policies


def test_an_acknowledged_action_is_admitted_for_its_module_only(
    cdk: ModuleType,
    read_app: Callable[[Any, Path], tuple[Templates, CreatedBy]],
    read_acknowledged: Callable[[Path], Acknowledged],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "rhi"
    stage = load(source, "stage.py", monkeypatch)
    group = load(source, "pools/group.py", monkeypatch)
    folder = tmp_path / "acknowledged"
    folder.mkdir()
    (folder / "pools_group.toml").write_text(
        f'[wildcard_actions]\n"{DESCRIBE}" = "AWS grants it only on every resource."\n', encoding="utf-8"
    )

    # The same role twice in one stack: in a construct of the module whose file lists the action,
    # and beside it, where the nearest construct of the source tree is the stage.
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    stack = cdk.Stack(stage.Endpoint(app, "Stage"), "Stack")
    describing_role(cdk, group.Pool(stack, "Pool"))
    describing_role(cdk, stack)
    templates, created_by = read_app(app, source)
    policies = default_policies(templates, created_by)

    assert set(policies) == {"pools_group", "stage"}
    found = violations(templates, created_by, read_acknowledged(folder))
    assert found == [Finding(*policies["stage"], on_every_resource(DESCRIBE))]


def test_a_resource_belongs_to_the_module_of_its_nearest_construct(
    cdk: ModuleType,
    read_app: Callable[[Any, Path], tuple[Templates, CreatedBy]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "rhi"
    stage = load(source, "stage.py", monkeypatch)
    pipeline = load(source, "pipeline/stack.py", monkeypatch)

    # A stack of the app itself, as the pipeline's will be, and a stack of a stage.
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    describing_role(cdk, pipeline.Pipeline(app, "Pipeline"))
    describing_role(cdk, cdk.Stack(stage.Endpoint(app, "Stage"), "Stack"))
    templates, created_by = read_app(app, source)

    # A role and its default policy in each template, each with the module of its stack. The
    # stage's template is in the folder of its assembly.
    modules = {path: sorted(module for (at, _), module in created_by.items() if at == path) for path in templates}
    assert [found for path, found in modules.items() if "/" not in path] == [["pipeline_stack", "pipeline_stack"]]
    assert [found for path, found in modules.items() if "/" in path] == [["stage", "stage"]]


def test_a_resource_that_no_module_created_fails_the_read(
    cdk: ModuleType, read_app: Callable[[Any, Path], tuple[Templates, CreatedBy]], tmp_path: Path
) -> None:
    source = tmp_path / "src" / "rhi"
    source.mkdir(parents=True)
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    describing_role(cdk, cdk.Stack(app, "Stack"))
    with pytest.raises(AssertionError, match="no construct of the source tree"):
        read_app(app, source)
