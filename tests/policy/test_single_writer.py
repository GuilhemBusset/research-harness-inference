"""The scaler is the only writer of a group's capacity (system design, section 5.1).

Section 5.1 rests on one writer: the scaler reads a group's capacity, decides and sets it, and a
second writer would undo its decisions without its knowing. So no role of the app but the scaler
function's own may set a group's capacity, whatever else it may do. A user or a group of the app
never may.

What sets a group's capacity: the actions in `CAPACITY_ACTIONS`, which change the desired, minimum
or maximum capacity, or which add or remove instances. An identity policy's `Allow` statement grants
one when an entry of its `Action` matches it (IAM's wildcards, without regard to case), or when its
`NotAction` matches none of them.

Who holds a statement: a role, user or group holds its inline policies; an `AWS::IAM::Policy` (and
the like) is held by those its `Roles`, `Users` and `Groups` name; a managed policy of the app is
held by those it names and by those that list it in their `ManagedPolicyArns`, by `Ref` in its own
template or by import from another. An AWS managed policy is read only when it is one of
`READ_AWS_MANAGED`, which grant no Auto Scaling action; any other counts as a writer, since the test
cannot read it. A holder the test cannot identify counts as well.

The scaler's role is the role of the Lambda function that `src/rhi/pools/scaler.py` created
(`created_by` names it `pools_scaler`), and that module also created the role.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    Templates = dict[str, dict[str, Any]]
    CreatedBy = dict[tuple[str, str], str]
    Holder = tuple[str, str]

SCALER_MODULE = "pools_scaler"

CAPACITY_ACTIONS = (
    "autoscaling:SetDesiredCapacity",
    "autoscaling:UpdateAutoScalingGroup",
    "autoscaling:ExecutePolicy",
    "autoscaling:PutScalingPolicy",
    "autoscaling:PutScheduledUpdateGroupAction",
    "autoscaling:BatchPutScheduledUpdateGroupAction",
    "autoscaling:TerminateInstanceInAutoScalingGroup",
    "autoscaling:AttachInstances",
    "autoscaling:DetachInstances",
    "autoscaling:EnterStandby",
    "autoscaling:ExitStandby",
)

# AWS managed policies the app attaches that grant no Auto Scaling action, by their name.
READ_AWS_MANAGED = frozenset(
    {"service-role/AWSLambdaBasicExecutionRole", "service-role/AWSLambdaVPCAccessExecutionRole"}
)

PRINCIPALS = ("AWS::IAM::Role", "AWS::IAM::User", "AWS::IAM::Group")
POLICIES = (
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
    "AWS::IAM::RolePolicy",
    "AWS::IAM::UserPolicy",
    "AWS::IAM::GroupPolicy",
)
UNREADABLE = ("?", "a holder the test cannot identify")


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def sets_capacity(statement: Any) -> bool:
    """Whether a statement allows an action that sets a group's capacity. What is not written out
    as a statement counts."""
    if not isinstance(statement, dict):
        return True
    if statement.get("Effect") != "Allow":
        return False

    def matched(action: str, patterns: Any) -> bool:
        return any(
            isinstance(pattern, str) and fnmatch.fnmatchcase(action.lower(), pattern.lower())
            for pattern in as_list(patterns)
        )

    if "NotAction" in statement:
        return not all(matched(action, statement["NotAction"]) for action in CAPACITY_ACTIONS)
    patterns = as_list(statement.get("Action", []))
    unreadable = any(not isinstance(pattern, str) for pattern in patterns)
    return unreadable or any(matched(action, patterns) for action in CAPACITY_ACTIONS)


def documents_set_capacity(documents: list[Any]) -> bool:
    return any(
        sets_capacity(statement)
        for document in documents
        for statement in (as_list(document.get("Statement", [])) if isinstance(document, dict) else [document])
    )


def exported(templates: Templates, name: Any) -> Holder | None:
    """The template and logical ID that an export of the app refers to by `Ref`."""
    for path, template in templates.items():
        for output in template.get("Outputs", {}).values():
            value = output.get("Value")
            if output.get("Export", {}).get("Name") == name and isinstance(value, dict) and "Ref" in value:
                return path, value["Ref"]
    return None


def holder(templates: Templates, path: str, reference: Any) -> Holder:
    """The principal a policy names, by `Ref` in its own template."""
    if isinstance(reference, dict) and isinstance(reference.get("Ref"), str):
        resource = templates[path].get("Resources", {}).get(reference["Ref"], {})
        if resource.get("Type") in PRINCIPALS:
            return path, reference["Ref"]
    return UNREADABLE


def writers(templates: Templates) -> Iterator[Holder]:
    """Every role, user or group of the app that may set a group's capacity."""
    writing_policies: set[Holder] = set()
    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            properties = resource.get("Properties", {})
            kind = resource.get("Type")
            if kind in PRINCIPALS:
                inline = [p.get("PolicyDocument") if isinstance(p, dict) else p for p in properties.get("Policies", [])]
                if documents_set_capacity(as_list(inline)):
                    yield path, logical_id
            elif kind in POLICIES and documents_set_capacity([properties.get("PolicyDocument")]):
                writing_policies.add((path, logical_id))
                for key in ("Roles", "Users", "Groups", "RoleName", "UserName", "GroupName"):
                    for reference in as_list(properties.get(key, [])):
                        yield holder(templates, path, reference)

    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") not in PRINCIPALS:
                continue
            for attached in as_list(resource.get("Properties", {}).get("ManagedPolicyArns", [])):
                if isinstance(attached, dict) and isinstance(attached.get("Ref"), str):
                    policy: Holder | None = (path, attached["Ref"])
                elif isinstance(attached, dict) and "Fn::ImportValue" in attached:
                    policy = exported(templates, attached["Fn::ImportValue"])
                else:
                    policy = None
                if policy is not None:
                    if policy in writing_policies:
                        yield path, logical_id
                elif not aws_managed_names(attached):
                    yield path, logical_id


def aws_managed_names(attached: Any) -> set[str]:
    """The names of `READ_AWS_MANAGED` that an attached ARN, written out, joined or substituted,
    ends with."""
    if isinstance(attached, dict) and "Fn::Join" in attached:
        attached = "".join(part for part in as_list(attached["Fn::Join"][1]) if isinstance(part, str))
    elif isinstance(attached, dict) and isinstance(attached.get("Fn::Sub"), str):
        attached = attached["Fn::Sub"]
    if not isinstance(attached, str):
        return set()
    return {name for name in READ_AWS_MANAGED if attached.endswith(f":iam::aws:policy/{name}")}


def scaler_roles(templates: Templates, created_by: CreatedBy) -> set[Holder]:
    """The roles of the Lambda functions the scaler's module created, which it created too."""
    roles = set()
    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") != "AWS::Lambda::Function" or created_by.get((path, logical_id)) != SCALER_MODULE:
                continue
            role = resource.get("Properties", {}).get("Role")
            if isinstance(role, dict) and "Fn::GetAtt" in role:
                role_id = role["Fn::GetAtt"][0]
                if created_by.get((path, role_id)) == SCALER_MODULE:
                    roles.add((path, role_id))
    return roles


def second_writers(templates: Templates, created_by: CreatedBy) -> list[Holder]:
    """Whoever may set a group's capacity and is not the scaler's role."""
    allowed = scaler_roles(templates, created_by)
    return sorted(set(writers(templates)) - allowed)


def test_only_the_scaler_may_set_a_group_s_capacity(templates: Templates, created_by: CreatedBy) -> None:
    assert scaler_roles(templates, created_by), "the app has no scaler function"
    assert second_writers(templates, created_by) == []


# A template built in the test: the scaler's function and role, and the policies each case adds.
SCALER = "Pools.template.json"


def scaler_template(*others: tuple[str, dict[str, Any]]) -> tuple[Templates, CreatedBy]:
    statement = {"Effect": "Allow", "Action": "autoscaling:SetDesiredCapacity", "Resource": "arn:group"}
    resources = {
        "ScalerRole": {"Type": "AWS::IAM::Role", "Properties": {}},
        "ScalerPolicy": {
            "Type": "AWS::IAM::Policy",
            "Properties": {"PolicyDocument": {"Statement": [statement]}, "Roles": [{"Ref": "ScalerRole"}]},
        },
        "ScalerFunction": {
            "Type": "AWS::Lambda::Function",
            "Properties": {"Role": {"Fn::GetAtt": ["ScalerRole", "Arn"]}},
        },
        **dict(others),
    }
    created_by = {
        (SCALER, logical_id): SCALER_MODULE for logical_id in ("ScalerRole", "ScalerPolicy", "ScalerFunction")
    }
    created_by |= {(SCALER, logical_id): "pools_group" for logical_id, _ in others}
    return {SCALER: {"Resources": resources}}, created_by


def test_detects_a_second_writer() -> None:
    alone = scaler_template()
    assert second_writers(*alone) == []

    for action in ("autoscaling:SetDesiredCapacity", "AutoScaling:Set*", "autoscaling:*", "*"):
        policy = {"Statement": [{"Effect": "Allow", "Action": [action], "Resource": "*"}]}
        inline = scaler_template(
            ("Other", {"Type": "AWS::IAM::Role", "Properties": {"Policies": [{"PolicyDocument": policy}]}})
        )
        assert second_writers(*inline) == [(SCALER, "Other")], action

    attached = scaler_template(
        ("Other", {"Type": "AWS::IAM::Role", "Properties": {"ManagedPolicyArns": [{"Ref": "Writes"}]}}),
        (
            "Writes",
            {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": {"Effect": "Allow", "NotAction": "s3:*", "Resource": "*"},
                    }
                },
            },
        ),
    )
    assert second_writers(*attached) == [(SCALER, "Other")]

    # Another role of the scaler's own module, which no function of it runs as, is a second writer too.
    templates, created_by = scaler_template()
    resources = templates[SCALER]["Resources"]
    resources["Spare"] = {"Type": "AWS::IAM::Role", "Properties": {}}
    resources["ScalerPolicy"]["Properties"]["Roles"].append({"Ref": "Spare"})
    created_by[SCALER, "Spare"] = SCALER_MODULE
    assert second_writers(templates, created_by) == [(SCALER, "Spare")]


def test_reading_a_group_or_suspending_its_processes_is_not_writing_its_capacity() -> None:
    actions = ["autoscaling:Describe*", "autoscaling:SuspendProcesses", "autoscaling:CreateOrUpdateTags"]
    policy = {"Statement": [{"Effect": "Allow", "Action": actions, "Resource": "*"}]}
    denied = {"Statement": [{"Effect": "Deny", "Action": "autoscaling:*", "Resource": "*"}]}
    basic = {
        "Fn::Join": [
            "",
            ["arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
        ]
    }
    other = {"Policies": [{"PolicyDocument": policy}, {"PolicyDocument": denied}], "ManagedPolicyArns": [basic]}
    templates, created_by = scaler_template(("Other", {"Type": "AWS::IAM::Role", "Properties": other}))
    assert second_writers(templates, created_by) == []
