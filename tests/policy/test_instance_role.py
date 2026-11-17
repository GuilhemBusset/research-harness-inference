"""An instance role reads no parameter and no secret other than its own pool's (system design,
section 4.3; evidence E23).

An instance role is a role EC2 may assume. Its pool is the value of its tag `rhi-pool`. The AWS
managed policy for Session Manager would let every instance read every SSM parameter, the
configuration's included. So an instance role carries no AWS managed policy, whose statements
this test cannot read, and the statements it does carry are held to these rules:

- a statement that may read a parameter names only parameters under `/rhi/pools/<own pool>/`;
- no such statement names a parameter of the configuration, `/rhi/config/`: this is reported on
  its own, because it is the reason for the rule;
- a statement that may read a secret's value names only its own pool's secrets: a secret of the
  app tagged `rhi-pool` with the pool's name or named under `/rhi/pools/<own pool>/`, or the ARN
  of a secret named so.

The statements are those of the role's inline policies and of every policy that names the role,
in any template of the app. An action is read as IAM reads it, wildcards included, and
`NotAction` may be any action. A resource the test cannot resolve, `NotResource`, and a policy it
cannot read count against the role: the rules fail closed.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from functools import cache
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    Template = Mapping[str, Any]
    Templates = Mapping[str, Template]
    # The value each stack exports under a name, with the path of the template that exports it.
    Exports = dict[str, tuple[Any, str]]

POOL_TAG = "rhi-pool"
EC2 = re.compile(r"ec2\.(amazonaws\.com|\$\{AWS::URLSuffix\})")

PARAMETER_READS = ("ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory")
SECRET_READS = ("secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue")
# The parameters of the configuration (`rhi.config.SSM_PREFIX`), in every partition, region and
# account, and the paths above them: `ssm:GetParametersByPath` on a path reads every level below it.
CONFIGURATION_PARAMETERS = tuple(
    "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter" + name
    for name in ("/rhi/config/*", "/rhi/config", "/rhi", "/", "")
)
# The AWS managed policies that give Session Manager what it needs, and more.
SESSION_MANAGER_POLICIES = (
    "AmazonSSMManagedInstanceCore",
    "AmazonEC2RoleforSSM",
    "AmazonSSMManagedEC2InstanceDefaultPolicy",
)
AWS_MANAGED = re.compile(r"arn:[^:]+:iam::aws:policy/(.+/)?(?P<name>[^/]+)")

OTHER_PARAMETER = "may read a parameter other than its own pool's"
CONFIGURATION = "may read the configuration's parameters"
OTHER_SECRET = "may read a secret other than its own pool's"
SESSION_MANAGER_POLICY = "carries the AWS managed Session Manager policy"
OTHER_MANAGED_POLICY = "carries an AWS managed policy, whose statements the test cannot read"
NOT_READ = "holds a policy the test cannot read"

SUBSTITUTION = re.compile(r"\$\{[^}]*\}")


class Finding(NamedTuple):
    template: str
    role: str
    problem: str


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def target(value: Any, path: str, templates: Templates, exports: Exports) -> tuple[str, str] | None:
    """The resource that `Ref` names, through an import, as `(template path, logical ID)`."""
    if isinstance(value, dict) and len(value) == 1:
        ((function, argument),) = value.items()
        if function == "Ref" and isinstance(argument, str) and argument in templates[path].get("Resources", {}):
            return path, argument
        if function == "Fn::ImportValue" and isinstance(argument, str) and argument in exports:
            exported, exporter = exports[argument]
            return target(exported, exporter, templates, exports)
    return None


def written(value: Any, path: str, templates: Templates, exports: Exports) -> str | None:
    """A value as text, with pseudo parameters as `${AWS::...}` and a parameter of the app by its
    name. None when it is built from anything else, which deployment may choose."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((function, argument),) = value.items()
    if function == "Ref" and isinstance(argument, str) and argument.startswith("AWS::"):
        return "${" + argument + "}"
    if function == "Fn::Sub" and isinstance(argument, str):
        return argument if all(s.startswith("${AWS::") for s in SUBSTITUTION.findall(argument)) else None
    if function == "Fn::Join" and isinstance(argument, list) and len(argument) == 2 and isinstance(argument[1], list):
        parts = [written(part, path, templates, exports) for part in argument[1]]
        if isinstance(argument[0], str) and all(part is not None for part in parts):
            return argument[0].join(part for part in parts if part is not None)
        return None
    if function == "Fn::ImportValue" and isinstance(argument, str) and argument in exports:
        exported, exporter = exports[argument]
        return written(exported, exporter, templates, exports)
    found = target(value, path, templates, exports)
    if found is not None:
        resource = templates[found[0]]["Resources"][found[1]]
        name = resource.get("Properties", {}).get("Name")
        # The `Ref` of a parameter is its name.
        if resource.get("Type") == "AWS::SSM::Parameter" and isinstance(name, str):
            return name
    return None


# What a pattern is read as: a character, `*` (any text), `?` (one character), or a field, one
# value with no `:` in it, which a pseudo parameter is.
ANY, ONE, FIELD = "*", "?", "${}"


def tokens(pattern: str) -> list[str]:
    pieces = SUBSTITUTION.split(pattern)
    return [token for n, piece in enumerate(pieces) for token in ([FIELD] if n else []) + list(piece)]


def overlaps(pattern: str, other: str) -> bool:
    """Whether two resource patterns of IAM, with pseudo parameters, may name the same text."""
    first, second = tokens(pattern), tokens(other)

    def accepts(token: str, char: str) -> bool:
        return token in (ANY, ONE) or (token == FIELD and char != ":") or token == char

    @cache
    def from_(i: int, j: int) -> bool:
        a = first[i] if i < len(first) else None
        b = second[j] if j < len(second) else None
        if a is None and b is None:
            return True
        # A run that stands for no text.
        if (a in (ANY, FIELD) and from_(i + 1, j)) or (b in (ANY, FIELD) and from_(i, j + 1)):
            return True
        if a is None or b is None or (a in (ANY, FIELD) and b in (ANY, FIELD)):
            # Two runs read together: one of them may as well stand for no text, above.
            return False
        # One character both take, and what is left of each.
        if len(a) == 1 and a not in (ANY, ONE):
            shared = accepts(b, a)
        elif len(b) == 1 and b not in (ANY, ONE):
            shared = accepts(a, b)
        else:
            shared = True
        return shared and from_(i if a in (ANY, FIELD) else i + 1, j if b in (ANY, FIELD) else j + 1)

    return from_(0, 0)


def under(pool: str, service: str, kind: str) -> re.Pattern[str]:
    """An ARN of the service whose name is under the pool's prefix, written out up to it. `kind`
    is what an ARN of the service writes before a name that starts with `/`."""
    return re.compile(rf"arn:[^:*?]+:{service}:[^:]*:[^:]*:{re.escape(kind)}/rhi/pools/{re.escape(pool)}/.+")


def without_substitutions(text: str) -> str:
    return SUBSTITUTION.sub("x", text)


def resources(statement: Mapping[str, Any]) -> list[Any]:
    """The resources a statement names; `NotResource` is none that can be resolved."""
    if "Resource" not in statement:
        return [None]
    named = []
    for resource in as_list(statement["Resource"]):
        if isinstance(resource, dict) and list(resource) == ["Fn::If"] and len(resource["Fn::If"]) == 3:
            named.extend(as_list(resource["Fn::If"][1]) + as_list(resource["Fn::If"][2]))
        else:
            named.append(resource)
    return named


def may(statement: Mapping[str, Any], reads: tuple[str, ...]) -> bool:
    """Whether a statement that allows may call one of the reads."""
    if "Action" not in statement:
        return True
    return any(
        not isinstance(action, str) or fnmatchcase(read.lower(), action.lower())
        for action in as_list(statement["Action"])
        for read in reads
    )


def own_secret(resource: Any, pool: str | None, path: str, templates: Templates, exports: Exports) -> bool:
    if pool is None:
        return False
    found = target(resource, path, templates, exports)
    if found is not None:
        secret = templates[found[0]]["Resources"][found[1]]
        properties = secret.get("Properties", {})
        name = properties.get("Name")
        tags = {tag.get("Key"): tag.get("Value") for tag in properties.get("Tags", []) if isinstance(tag, dict)}
        return secret.get("Type") == "AWS::SecretsManager::Secret" and (
            tags.get(POOL_TAG) == pool or (isinstance(name, str) and name.startswith(f"/rhi/pools/{pool}/"))
        )
    text = written(resource, path, templates, exports)
    return (
        text is not None and under(pool, "secretsmanager", "secret:").fullmatch(without_substitutions(text)) is not None
    )


def problems(statement: Any, pool: str | None, path: str, templates: Templates, exports: Exports) -> list[str]:
    """What one statement of an instance role of `pool` allows that it may not."""
    if not isinstance(statement, dict) or "Effect" not in statement:
        return [NOT_READ]
    if statement["Effect"] == "Deny":
        return []
    found = []
    if may(statement, PARAMETER_READS):
        texts = [written(resource, path, templates, exports) for resource in resources(statement)]
        if pool is None or any(
            text is None or under(pool, "ssm", "parameter").fullmatch(without_substitutions(text)) is None
            for text in texts
        ):
            found.append(OTHER_PARAMETER)
        if any(text is None or any(overlaps(text, name) for name in CONFIGURATION_PARAMETERS) for text in texts):
            found.append(CONFIGURATION)
    if may(statement, SECRET_READS) and not all(
        own_secret(resource, pool, path, templates, exports) for resource in resources(statement)
    ):
        found.append(OTHER_SECRET)
    return found


def is_an_instance_role(resource: Mapping[str, Any]) -> bool:
    if resource.get("Type") != "AWS::IAM::Role":
        return False
    trust = resource.get("Properties", {}).get("AssumeRolePolicyDocument", {})
    return any(
        isinstance(statement, dict)
        and isinstance(statement.get("Principal"), dict)
        and any(
            (text := written(service, "", {"": {}}, {})) is not None and EC2.fullmatch(text)
            for service in as_list(statement["Principal"].get("Service", []))
        )
        for statement in as_list(trust.get("Statement", []))
    )


def instance_roles(templates: Templates) -> list[tuple[str, str]]:
    """Every instance role of the templates, as `(template path, logical ID)`."""
    return [
        (path, logical_id)
        for path, template in templates.items()
        for logical_id, resource in template.get("Resources", {}).items()
        if is_an_instance_role(resource)
    ]


def violations(templates: Templates) -> list[Finding]:
    """Every rule an instance role of the templates breaks."""
    exports: Exports = {
        output["Export"]["Name"]: (output.get("Value"), path)
        for path, template in templates.items()
        for output in template.get("Outputs", {}).values()
        if isinstance(output.get("Export", {}).get("Name"), str)
    }
    found = []
    for role in instance_roles(templates):
        path, logical_id = role
        properties = templates[path]["Resources"][logical_id].get("Properties", {})
        tags = {tag.get("Key"): tag.get("Value") for tag in properties.get("Tags", []) if isinstance(tag, dict)}
        pool = tags.get(POOL_TAG) if isinstance(tags.get(POOL_TAG), str) else None
        # Each document, with the template it is read in.
        documents = [
            (policy.get("PolicyDocument") if isinstance(policy, dict) else None, path)
            for policy in as_list(properties.get("Policies", []))
        ]
        for arn in as_list(properties.get("ManagedPolicyArns", [])):
            policy = target(arn, path, templates, exports)
            text = written(arn, path, templates, exports)
            managed = AWS_MANAGED.fullmatch(without_substitutions(text)) if text is not None else None
            if (
                policy is not None
                and templates[policy[0]]["Resources"][policy[1]].get("Type") == "AWS::IAM::ManagedPolicy"
            ):
                continue  # Read below, as a policy that names the role.
            if managed is not None and managed["name"] in SESSION_MANAGER_POLICIES:
                found.append(Finding(path, logical_id, SESSION_MANAGER_POLICY))
            elif managed is not None:
                found.append(Finding(path, logical_id, OTHER_MANAGED_POLICY))
            else:
                found.append(Finding(path, logical_id, NOT_READ))
        for other, template in templates.items():
            for policy_id, resource in template.get("Resources", {}).items():
                if resource.get("Type") not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                    continue
                policy_properties = resource.get("Properties", {})
                named = [
                    target(name, other, templates, exports) for name in as_list(policy_properties.get("Roles", []))
                ]
                attached = [
                    target(arn, path, templates, exports) for arn in as_list(properties.get("ManagedPolicyArns", []))
                ]
                if role in named or (other, policy_id) in attached:
                    documents.append((policy_properties.get("PolicyDocument"), other))
        for document, where in documents:
            statements = as_list(document.get("Statement")) if isinstance(document, dict) else [None]
            found.extend(
                Finding(path, logical_id, problem)
                for statement in statements
                for problem in problems(statement, pool, where, templates, exports)
            )
    return list(dict.fromkeys(found))


def test_the_apps_instance_roles_read_only_their_own_pools(templates: Templates) -> None:
    assert instance_roles(templates), "the app has no instance role"
    assert violations(templates) == []


def built(cdk: ModuleType, build: Any) -> Templates:
    """The template of a stack, built in the test, that holds the mock pool's instance role, which
    `build(stack, role)` gives what it reads."""
    stack = cdk.Stack()
    role = cdk.aws_iam.Role(stack, "Role", assumed_by=cdk.aws_iam.ServicePrincipal("ec2.amazonaws.com"))
    cdk.Tags.of(role).add(POOL_TAG, "mock")
    build(stack, role)
    return {"built.template.json": cdk.assertions.Template.from_stack(stack).to_json()}


def problems_of(templates: Templates) -> list[str]:
    return [finding.problem for finding in violations(templates)]


def test_detects_the_managed_policy(cdk: ModuleType) -> None:
    iam = cdk.aws_iam
    managed = iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
    assert problems_of(built(cdk, lambda _, role: role.add_managed_policy(managed))) == [SESSION_MANAGER_POLICY]
    # A policy written in the stack, with the five actions of the session, is read instead.
    session = iam.PolicyStatement(
        actions=["ssm:UpdateInstanceInformation", "ssmmessages:OpenDataChannel"], resources=["*"]
    )
    assert (
        problems_of(built(cdk, lambda stack, role: iam.Policy(stack, "Session", statements=[session], roles=[role])))
        == []
    )
    another = iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy")
    assert problems_of(built(cdk, lambda _, role: role.add_managed_policy(another))) == [OTHER_MANAGED_POLICY]


def test_detects_another_pools_secret(cdk: ModuleType) -> None:
    def reads(own: bool) -> Any:
        def build(stack: Any, role: Any) -> None:
            mine = cdk.aws_secretsmanager.Secret(stack, "Mine", secret_name="/rhi/pools/mock/key")
            theirs = cdk.aws_secretsmanager.Secret(stack, "Theirs")
            cdk.Tags.of(theirs).add(POOL_TAG, "other")
            (mine if own else theirs).grant_read(role)

        return build

    assert problems_of(built(cdk, reads(own=True))) == []
    assert problems_of(built(cdk, reads(own=False))) == [OTHER_SECRET]


# The ARN of a parameter, and of a secret, by name.
def parameter(name: str) -> dict[str, str]:
    return {"Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter" + name}


def secret(name: str) -> dict[str, str]:
    return {"Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:" + name}


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        pytest.param(
            {"Action": "ssm:GetParameter", "Resource": parameter("/rhi/pools/mock/release")}, [], id="its own"
        ),
        pytest.param(
            {"Action": "ssm:GetParameter", "Resource": {"Fn::Join": ["", [parameter(""), {"Ref": "Release"}]]}},
            [],
            id="its own, by reference",
        ),
        pytest.param(
            {"Action": "ssm:GetParameters", "Resource": parameter("/rhi/pools/other/release")},
            [OTHER_PARAMETER],
            id="another pool's",
        ),
        pytest.param(
            {"Action": "ssm:GetParameter", "Resource": parameter("/rhi/pools/mock*")},
            [OTHER_PARAMETER],
            id="a prefix of its pool's name",
        ),
        pytest.param(
            {"Action": "ssm:GetParametersByPath", "Resource": parameter("/rhi/config/domain")},
            [OTHER_PARAMETER, CONFIGURATION],
            id="the configuration's",
        ),
        pytest.param(
            {"Action": "ssm:GetParametersByPath", "Resource": parameter("/rhi")},
            [OTHER_PARAMETER, CONFIGURATION],
            id="a path above the configuration's",
        ),
        pytest.param({"Action": "ssm:Get*", "Resource": "*"}, [OTHER_PARAMETER, CONFIGURATION], id="every parameter"),
        pytest.param(
            {"NotAction": "iam:*", "Resource": parameter("/rhi/*")},
            [OTHER_PARAMETER, CONFIGURATION, OTHER_SECRET],
            id="every action but some",
        ),
        pytest.param(
            {"Action": "ssm:GetParameter", "Resource": {"Ref": "Chosen"}},
            [OTHER_PARAMETER, CONFIGURATION],
            id="a parameter chosen at deployment",
        ),
        pytest.param(
            {"Action": "secretsmanager:GetSecretValue", "Resource": secret("/rhi/pools/mock/key-AbCdEf")},
            [],
            id="its own secret, by ARN",
        ),
        pytest.param(
            {"Action": "secretsmanager:*", "Resource": secret("/rhi/pools/other/key-AbCdEf")},
            [OTHER_SECRET],
            id="another pool's secret, by ARN",
        ),
        pytest.param({"Action": "ssm:PutParameter", "Resource": "*"}, [], id="no read"),
    ],
)
def test_detects_a_read_of_another_pools_parameter_or_secret(statement: dict[str, Any], expected: list[str]) -> None:
    role = {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "ec2.amazonaws.com"}}
                ]
            },
            "Tags": [{"Key": POOL_TAG, "Value": "mock"}],
            "Policies": [{"PolicyName": "Built", "PolicyDocument": {"Statement": [{"Effect": "Allow"} | statement]}}],
        },
    }
    release = {"Type": "AWS::SSM::Parameter", "Properties": {"Name": "/rhi/pools/mock/release", "Type": "String"}}
    template = {"Parameters": {"Chosen": {"Type": "String"}}, "Resources": {"Role": role, "Release": release}}
    assert problems_of({"built.template.json": template}) == expected
