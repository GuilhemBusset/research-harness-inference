"""The five tables (system design, sections 2 and 8.5): each in its stack, with the keys, the
indexes and the expiry attribute of the shared schema `rhi_common.tables`, which the services read
too, billed on demand. Their protections are the stateful-resource invariant's
(`tests/policy/test_stateful_resources.py`).

No deployment writes an item of the pool-state table (section 11.3): a pool without a mode record
is in its configured mode, and what the admin set there, a disabled pool or a cleared mark, only
the admin changes. What a deployment runs is a custom resource, so `pool_state_writes` looks for
one that writes an item of the table: by an SDK call that its properties name with the table, or
through what serves it, a function or a state machine whose role may write an item of the table
(by its inline policies, a policy resource of any kind that names the role, or a managed policy it
attaches, AWS's included), by a reference to the table or by an ARN pattern that its name matches, or may run one that may (a
provider's framework, which runs a handler or a waiter), reached directly or through what stands
for it (an alias or a version of a function, a topic a function subscribes to). A table created from an import is written
by its deployment too.

The CDK is imported inside the fixtures, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi_admin.main import POOLS_STACK_NAME
from rhi_common.tables import HEARTBEATS, KEYS, POOL_STATE, ROSTER, TABLES, USAGE, AttributeType, KeyAttribute, Table

if TYPE_CHECKING:
    from collections.abc import Callable

    import aws_cdk as cdk

# The stack of the stage, by its attribute, that holds each table: a table that moves is replaced.
STACK_OF = {KEYS: "access", ROSTER: "access", USAGE: "access", POOL_STATE: "pools", HEARTBEATS: "pools"}

Resource = dict[str, Any]
Templates = dict[str, dict[str, Any]]
# A resource of the cloud assembly: the path of its template, and its logical ID in it.
Node = tuple[str, str]

# The IAM actions that write an item, and the SDK calls that do, as a custom resource names them.
ITEM_WRITE_ACTIONS = (
    "dynamodb:putitem",
    "dynamodb:updateitem",
    "dynamodb:deleteitem",
    "dynamodb:batchwriteitem",
    "dynamodb:partiqlinsert",
    "dynamodb:partiqlupdate",
    "dynamodb:partiqldelete",
)
ITEM_WRITE_CALLS = (
    "putitem",
    "updateitem",
    "deleteitem",
    "batchwriteitem",
    "transactwriteitems",
    "executestatement",
    "batchexecutestatement",
    "executetransaction",
)
# What lets a function or a state machine run another.
RUN_ACTIONS = ("lambda:invokefunction", "states:startexecution", "states:startsyncexecution")
# What may serve a custom resource or be run by one, and the property that names its role.
RUNNERS = {"AWS::Lambda::Function": "Role", "AWS::StepFunctions::StateMachine": "RoleArn"}
# What stands for a function, so that a custom resource or a role reaches the function through it:
# an alias or a version of it, or a topic it subscribes to.
STAND_INS = ("AWS::Lambda::Alias", "AWS::Lambda::Version", "AWS::SNS::Topic")
# The AWS managed policies that allow writing an item of any table, as the statement each amounts to.
_EVERYTHING = {"Effect": "Allow", "Action": "*", "Resource": "*"}
_ALL_OF_DYNAMODB = {"Effect": "Allow", "Action": "dynamodb:*", "Resource": "*"}
AWS_MANAGED_WRITERS = {
    "AdministratorAccess": _EVERYTHING,
    "PowerUserAccess": _EVERYTHING,
    "AmazonDynamoDBFullAccess": _ALL_OF_DYNAMODB,
    "AmazonDynamoDBFullAccess_v2": _ALL_OF_DYNAMODB,
    "AmazonDynamoDBFullAccesswithDataPipeline": _ALL_OF_DYNAMODB,
}


def managed_name(value: Any) -> str | None:
    """The name of the AWS managed policy an ARN of `ManagedPolicyArns` names, if it names one."""
    text = pattern(value)
    found = re.fullmatch(r"arn:[^:]*:iam::aws:policy/(?:.*/)?([^/]+)", text)
    return found.group(1) if found else None


def key_schema(partition: KeyAttribute, sort: KeyAttribute | None) -> list[dict[str, str]]:
    schema = [{"AttributeName": partition.name, "KeyType": "HASH"}]
    return schema + ([{"AttributeName": sort.name, "KeyType": "RANGE"}] if sort else [])


def differences(resource: Resource, table: Table) -> list[str]:
    """What of `resource`, a synthesized `AWS::DynamoDB::Table`, differs from the shared schema's
    `table`: its keys, its indexes, the types of their attributes and its expiry attribute."""
    properties = resource["Properties"]
    found = []
    if properties["KeySchema"] != key_schema(table.partition_key, table.sort_key):
        found.append(f"keys {properties['KeySchema']}")
    indexes = {index["IndexName"]: index["KeySchema"] for index in properties.get("GlobalSecondaryIndexes", [])}
    expected = {index.name: key_schema(index.partition_key, index.sort_key) for index in table.indexes}
    if indexes != expected:
        found.append(f"indexes {indexes}")
    keys = [table.partition_key, table.sort_key] + [k for i in table.indexes for k in (i.partition_key, i.sort_key)]
    types = {a["AttributeName"]: a["AttributeType"] for a in properties["AttributeDefinitions"]}
    if types != {key.name: key.type.value for key in keys if key is not None}:
        found.append(f"attribute types {types}")
    if properties.get("TimeToLiveSpecification") != {"AttributeName": table.expiry_attribute, "Enabled": True}:
        found.append(f"expiry {properties.get('TimeToLiveSpecification')}")
    return found


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def template_path(app: cdk.App, cloud_assembly: Path, stack: Any) -> str:
    stage = app.node.find_child("Rhi")
    return (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]


@pytest.fixture(scope="module")
def tables(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[Table, Resource]:
    """Each table of the shared schema, as its stack's template holds it."""
    stage = app.node.find_child("Rhi")
    found = {}
    for table in TABLES:
        stack = getattr(stage, STACK_OF[table])
        construct = stack.tables.by_name[table.name]
        path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
        logical_id = stack.resolve(stack.get_logical_id(construct.node.default_child))
        found[table] = templates[path]["Resources"][logical_id]
    return found


def test_each_stack_holds_its_tables(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> None:
    stage = app.node.find_child("Rhi")
    for name in set(STACK_OF.values()):
        stack = getattr(stage, name)
        path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
        held = [r for r in templates[path]["Resources"].values() if r["Type"] == "AWS::DynamoDB::Table"]
        assert len(held) == len([table for table, of in STACK_OF.items() if of == name]), name
        assert sorted(stack.tables.by_name) == sorted(table.name for table, of in STACK_OF.items() if of == name)


def test_the_schema_is_the_shared_one(tables: dict[Table, Resource]) -> None:
    assert set(tables) == set(TABLES)
    for table, resource in tables.items():
        assert differences(resource, table) == [], table.name


def test_every_table_is_billed_on_demand(tables: dict[Table, Resource]) -> None:
    for table, resource in tables.items():
        assert resource["Properties"]["BillingMode"] == "PAY_PER_REQUEST", table.name


def test_a_key_or_index_that_differs_from_the_schema_is_found(tables: dict[Table, Resource]) -> None:
    other = KeyAttribute("other", AttributeType.STRING)
    (by_student,) = KEYS.indexes
    for changed in [
        dataclasses.replace(KEYS, partition_key=other),
        dataclasses.replace(KEYS, sort_key=other),
        dataclasses.replace(KEYS, indexes=(dataclasses.replace(by_student, sort_key=other),)),
        dataclasses.replace(KEYS, indexes=(dataclasses.replace(by_student, name="other"),)),
        dataclasses.replace(KEYS, indexes=()),
        dataclasses.replace(USAGE, sort_key=KeyAttribute("day", AttributeType.NUMBER)),
        dataclasses.replace(USAGE, expiry_attribute="other"),
    ]:
        resource = tables[KEYS] if changed.name == KEYS.name else tables[USAGE]
        assert differences(resource, changed) != [], changed


class Assembly:
    """The templates of a cloud assembly, as far as who may write what: references, across stacks
    by their exports too, and the statements of each role."""

    def __init__(self, templates: Templates) -> None:
        self.templates = templates
        self.exports = {
            output["Export"]["Name"]: (path, output["Value"])
            for path, template in templates.items()
            for output in template.get("Outputs", {}).values()
            if isinstance(output.get("Export", {}).get("Name"), str)
        }

    def resources(self) -> list[tuple[Node, Resource]]:
        return [((path, name), r) for path, t in self.templates.items() for name, r in t.get("Resources", {}).items()]

    def references(self, value: Any, path: str) -> set[Node | str]:
        """The resources a value refers to, an import as what its stack exports; `*` for every resource."""
        if isinstance(value, str):
            return {"*"} if value == "*" else set()
        if isinstance(value, list):
            return set().union(*(self.references(part, path) for part in value))
        if not isinstance(value, dict):
            return set()
        if isinstance(value.get("Ref"), str):
            return set() if value["Ref"].startswith("AWS::") else {(path, value["Ref"])}
        if "Fn::GetAtt" in value:
            attribute = value["Fn::GetAtt"]
            return {(path, attribute[0] if isinstance(attribute, list) else attribute.split(".")[0])}
        if "Fn::ImportValue" in value and isinstance(value["Fn::ImportValue"], str):
            exported = self.exports.get(value["Fn::ImportValue"])
            return set() if exported is None else self.references(exported[1], exported[0])
        if "Fn::Sub" in value:
            text, variables = (as_list(value["Fn::Sub"]) + [{}])[:2]
            named = {(path, name) for name in re.findall(r"\$\{([A-Za-z0-9]+)[.}]", text)}
            return named | self.references(variables, path)
        return set().union(*(self.references(part, path) for part in value.values()))

    def names(self, value: Any, path: str, role: Node) -> bool:
        """Whether a value names the role: by a reference to it, or by its name written out."""
        role_name = self.templates[role[0]]["Resources"].get(role[1], {}).get("Properties", {}).get("RoleName")
        written = {part for part in as_list(value) if isinstance(part, str)}
        return role in self.references(value, path) or (isinstance(role_name, str) and role_name in written)

    def statements(self, role: Node) -> list[tuple[str, dict[str, Any]]]:
        """Each statement of a role's policies, with the path of the template that holds it: its
        inline policies, a policy resource that names it (`AWS::IAM::Policy`, `AWS::IAM::RolePolicy`
        or `AWS::IAM::ManagedPolicy`), and a managed policy it attaches, an AWS one included."""
        path, name = role
        properties = self.templates[path]["Resources"].get(name, {}).get("Properties", {})
        found = [
            (path, statement)
            for policy in properties.get("Policies", [])
            for statement in as_list(policy["PolicyDocument"]["Statement"])
        ]
        arns = as_list(properties.get("ManagedPolicyArns", []))
        found += [(path, AWS_MANAGED_WRITERS[p]) for p in map(managed_name, arns) if p in AWS_MANAGED_WRITERS]
        attached = self.references(arns, path)
        for node, resource in self.resources():
            policy = resource.get("Properties", {})
            if resource["Type"] == "AWS::IAM::RolePolicy":
                named = self.names(policy.get("RoleName"), node[0], role)
            elif resource["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                named = node in attached or self.names(policy.get("Roles", []), node[0], role)
            else:
                continue
            if named:
                found += [(node[0], statement) for statement in as_list(policy["PolicyDocument"]["Statement"])]
        return found

    def allows(
        self, runner: Node, resource: Resource, actions: tuple[str, ...], on: set[Node], arns: tuple[str, ...] = ()
    ) -> bool:
        """Whether the runner's role may make one of the actions on one of the resources `on`, or on
        one of the `arns` by a pattern."""
        roles = self.references(resource["Properties"].get(RUNNERS[resource["Type"]]), runner[0])
        for role in (role for role in roles if isinstance(role, tuple)):
            for path, statement in self.statements(role):
                granted = [action.lower() for action in as_list(statement.get("Action", []))]
                if statement.get("Effect") != "Allow" or not any(fnmatch(a, g) for a in actions for g in granted):
                    continue
                reached = self.references(statement.get("Resource", []), path)
                if "*" in reached or reached & on:
                    return True
                patterns = [pattern(part) for part in as_list(statement.get("Resource", []))]
                if any(fnmatch(arn, text) for arn in arns for text in patterns):
                    return True
        return False


# A value only a deployment knows, such as another resource's name, in an ARN pattern: nothing matches it.
UNKNOWN = "\0"


def pattern(value: Any) -> str:
    """The text a value of a template makes, as a pattern: a pseudo parameter such as the account or
    the region matches anything, a value only a deployment knows matches nothing."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or len(value) != 1:
        return UNKNOWN
    if isinstance(value.get("Ref"), str):
        return "*" if value["Ref"].startswith("AWS::") else UNKNOWN
    if "Fn::Join" in value:
        separator, parts = (as_list(value["Fn::Join"]) + [None])[:2]
        if not isinstance(separator, str) or not isinstance(parts, list):
            return UNKNOWN
        return separator.join(pattern(part) for part in parts)
    if "Fn::Sub" in value:
        text, variables = (as_list(value["Fn::Sub"]) + [{}])[:2]
        if not isinstance(text, str) or not isinstance(variables, dict):
            return UNKNOWN

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in variables:
                return pattern(variables[name])
            return "*" if name.startswith("AWS::") else UNKNOWN

        return re.sub(r"\$\{([^}!]+)\}", substitute, text)
    return UNKNOWN


def pool_state_writes(templates: Templates, table: Node) -> list[str]:
    """What, in the templates, writes an item of the table when it is deployed."""
    assembly = Assembly(templates)
    # The table's ARN, as CloudFormation names it: the stack's name, its logical ID and a suffix.
    arn = f"arn:aws:dynamodb:us-east-1:123456789012:table/{POOLS_STACK_NAME}-{table[1]}-EXAMPLE"
    runners = {node: r for node, r in assembly.resources() if r["Type"] in RUNNERS}
    writers = {node for node, r in runners.items() if assembly.allows(node, r, ITEM_WRITE_ACTIONS, {table}, (arn,))}
    stand_ins = {node: r for node, r in assembly.resources() if r["Type"] in STAND_INS}
    subscriptions = [(node, r) for node, r in assembly.resources() if r["Type"] == "AWS::SNS::Subscription"]
    grown = True
    while grown:
        more = {
            node
            for node, r in runners.items()
            if node not in writers and assembly.allows(node, r, RUN_ACTIONS, writers)
        }
        more |= {
            node
            for node, r in stand_ins.items()
            if node not in writers and assembly.references(r.get("Properties", {}), node[0]) & writers
        }
        for node, r in subscriptions:
            properties = r.get("Properties", {})
            if assembly.references(properties.get("Endpoint"), node[0]) & writers:
                topics = assembly.references(properties.get("TopicArn"), node[0])
                more |= {topic for topic in topics if isinstance(topic, tuple) and topic not in writers}
        writers |= more
        grown = bool(more)

    found = []
    for node, resource in assembly.resources():
        properties = resource.get("Properties", {})
        if node == table and "ImportSourceSpecification" in properties:
            found.append(f"{node}: the table is created from an import")
        if not (resource["Type"].startswith("Custom::") or resource["Type"] == "AWS::CloudFormation::CustomResource"):
            continue
        if assembly.references(properties.get("ServiceToken"), node[0]) & writers:
            found.append(f"{node}: served by what may write an item of the table")
        text = json.dumps(properties).lower()
        if table in assembly.references(properties, node[0]) and any(call in text for call in ITEM_WRITE_CALLS):
            found.append(f"{node}: names a call that writes an item of the table")
    return found


@pytest.fixture(scope="module")
def pool_state(app: cdk.App, cloud_assembly: Path) -> Node:
    """The pool-state table in the assembly."""
    stack = app.node.find_child("Rhi").pools  # ty: ignore[unresolved-attribute]
    construct = stack.tables.by_name[POOL_STATE.name]
    return template_path(app, cloud_assembly, stack), stack.resolve(stack.get_logical_id(construct.node.default_child))


def test_no_deployment_writes_pool_state(templates: Templates, pool_state: Node) -> None:
    assert pool_state_writes(templates, pool_state) == []


def _role(name: str, actions: list[str], resources: list[Any]) -> dict[str, Resource]:
    """A role and its policy, which allows the actions on the resources."""
    statement = {"Effect": "Allow", "Action": actions, "Resource": resources}
    return {
        f"{name}Role": {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {}}},
        f"{name}Policy": {
            "Type": "AWS::IAM::Policy",
            "Properties": {"PolicyDocument": {"Statement": [statement]}, "Roles": [{"Ref": f"{name}Role"}]},
        },
    }


def _runner(name: str, kind: str, actions: list[str], resources: list[Any]) -> dict[str, Resource]:
    role = {"Fn::GetAtt": [f"{name}Role", "Arn"]}
    return {name: {"Type": kind, "Properties": {RUNNERS[kind]: role}}, **_role(name, actions, resources)}


def _served_by(runner: str) -> Resource:
    return {"Type": "Custom::Injected", "Properties": {"ServiceToken": {"Fn::GetAtt": [runner, "Arn"]}}}


FUNCTION, MACHINE = "AWS::Lambda::Function", "AWS::StepFunctions::StateMachine"


def _sdk_call(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    create = {
        "Fn::Join": [
            "",
            ['{"service":"DynamoDB","action":"putItem","parameters":{"TableName":"', {"Ref": table}, '"}}'],
        ]
    }
    return {"Injected": {"Type": "Custom::AWS", "Properties": {"ServiceToken": "any", "Create": create}}}


def _the_scaler(table: str, scaler: str) -> dict[str, Resource]:
    del table
    return {"Injected": _served_by(scaler)}


def _a_provider(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    handler = {"Fn::GetAtt": ["Handler", "Arn"]}
    return {
        **_runner("Handler", FUNCTION, ["dynamodb:PutItem"], [{"Fn::GetAtt": [table, "Arn"]}]),
        **_runner("Framework", FUNCTION, ["lambda:InvokeFunction"], [handler, {"Fn::Join": ["", [handler, ":*"]]}]),
        "Injected": _served_by("Framework"),
    }


def _a_provider_s_waiter(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    return {
        **_runner("Handler", FUNCTION, ["dynamodb:*"], [{"Fn::GetAtt": [table, "Arn"]}]),
        **_runner("Waiter", MACHINE, ["lambda:InvokeFunction"], [{"Fn::GetAtt": ["Handler", "Arn"]}]),
        **_runner("Framework", FUNCTION, ["states:StartExecution"], [{"Ref": "Waiter"}]),
        "Injected": _served_by("Framework"),
    }


def _an_alias_of_the_scaler(table: str, scaler: str) -> dict[str, Resource]:
    del table
    version = {"Type": "AWS::Lambda::Version", "Properties": {"FunctionName": {"Ref": scaler}}}
    alias = {"FunctionName": {"Ref": scaler}, "FunctionVersion": {"Fn::GetAtt": ["Version", "Version"]}, "Name": "live"}
    token = {"ServiceToken": {"Ref": "Alias"}}
    return {
        "Version": version,
        "Alias": {"Type": "AWS::Lambda::Alias", "Properties": alias},
        "Injected": {"Type": "Custom::Injected", "Properties": token},
    }


def _a_topic_the_scaler_subscribes_to(table: str, scaler: str) -> dict[str, Resource]:
    del table
    subscription = {"Protocol": "lambda", "TopicArn": {"Ref": "Topic"}, "Endpoint": {"Fn::GetAtt": [scaler, "Arn"]}}
    return {
        "Topic": {"Type": "AWS::SNS::Topic", "Properties": {}},
        "Subscription": {"Type": "AWS::SNS::Subscription", "Properties": subscription},
        "Injected": {"Type": "Custom::Injected", "Properties": {"ServiceToken": {"Ref": "Topic"}}},
    }


def _a_provider_that_invokes_an_alias(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    alias = {"FunctionName": {"Ref": "Handler"}, "FunctionVersion": "$LATEST", "Name": "live"}
    return {
        **_runner("Handler", FUNCTION, ["dynamodb:PutItem"], [{"Fn::GetAtt": [table, "Arn"]}]),
        "Alias": {"Type": "AWS::Lambda::Alias", "Properties": alias},
        **_runner("Framework", FUNCTION, ["lambda:InvokeFunction"], [{"Ref": "Alias"}]),
        "Injected": _served_by("Framework"),
    }


def _a_handler_whose_role(policies: dict[str, Resource], role: Resource | None = None) -> dict[str, Resource]:
    """A function serving the custom resource, whose role is given its permissions by `policies`."""
    function = {"Type": FUNCTION, "Properties": {"Role": {"Fn::GetAtt": ["HandlerRole", "Arn"]}}}
    role = role or {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {}}}
    return {"Handler": function, "HandlerRole": role, **policies, "Injected": _served_by("Handler")}


def _a_role_policy(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    statement = {"Effect": "Allow", "Action": "dynamodb:PutItem", "Resource": {"Fn::GetAtt": [table, "Arn"]}}
    policy = {"PolicyName": "Seed", "RoleName": {"Ref": "HandlerRole"}, "PolicyDocument": {"Statement": [statement]}}
    return _a_handler_whose_role({"HandlerPolicy": {"Type": "AWS::IAM::RolePolicy", "Properties": policy}})


def _a_policy_that_names_the_role(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    statement = {"Effect": "Allow", "Action": "dynamodb:PutItem", "Resource": {"Fn::GetAtt": [table, "Arn"]}}
    policy = {"PolicyName": "Seed", "Roles": ["seed-role"], "PolicyDocument": {"Statement": [statement]}}
    role = {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {}, "RoleName": "seed-role"}}
    return _a_handler_whose_role({"HandlerPolicy": {"Type": "AWS::IAM::Policy", "Properties": policy}}, role)


def _an_aws_managed_policy(table: str, scaler: str) -> dict[str, Resource]:
    del table, scaler
    arn = {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::aws:policy/AmazonDynamoDBFullAccess"]]}
    role = {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {}, "ManagedPolicyArns": [arn]}}
    return _a_handler_whose_role({}, role)


def _table_name_arns(table: str) -> tuple[Any, Any]:
    """An ARN pattern that the pool-state table's name matches, built the two ways a template does."""
    name = f"table/{POOLS_STACK_NAME}-{table}-*"
    by_sub = {"Fn::Sub": f"arn:${{AWS::Partition}}:dynamodb:${{AWS::Region}}:${{AWS::AccountId}}:{name}"}
    parts = [
        "arn:",
        {"Ref": "AWS::Partition"},
        ":dynamodb:",
        {"Ref": "AWS::Region"},
        ":",
        {"Ref": "AWS::AccountId"},
        f":{name}",
    ]
    return by_sub, {"Fn::Join": ["", parts]}


def _a_name_pattern_by_sub(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    return {
        **_runner("Handler", FUNCTION, ["dynamodb:PutItem"], [_table_name_arns(table)[0]]),
        "Injected": _served_by("Handler"),
    }


def _a_name_pattern_by_join(table: str, scaler: str) -> dict[str, Resource]:
    del scaler
    return {
        **_runner("Handler", FUNCTION, ["dynamodb:UpdateItem"], [_table_name_arns(table)[1]]),
        "Injected": _served_by("Handler"),
    }


def test_a_pattern_finds_only_the_tables_it_names() -> None:
    arn = "arn:aws:dynamodb:us-east-1:123456789012:table/Stack-Table-EXAMPLE"
    another = {"Fn::Join": ["", [{"Fn::GetAtt": ["Other", "Arn"]}, "*"]]}
    assert [fnmatch(arn, pattern(value)) for value in ("*", "arn:aws:dynamodb:*:*:table/*", another)] == [
        True,
        True,
        False,
    ]
    assert not fnmatch(
        arn, pattern({"Fn::Sub": "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${Other}"})
    )


@pytest.mark.parametrize(
    "injected",
    [
        _sdk_call,
        _the_scaler,
        _a_provider,
        _a_provider_s_waiter,
        _a_name_pattern_by_sub,
        _a_name_pattern_by_join,
        _an_alias_of_the_scaler,
        _a_topic_the_scaler_subscribes_to,
        _a_provider_that_invokes_an_alias,
        _a_role_policy,
        _a_policy_that_names_the_role,
        _an_aws_managed_policy,
    ],
)
def test_a_deployment_that_writes_pool_state_is_found(
    app: cdk.App, templates: Templates, pool_state: Node, injected: Callable[[str, str], dict[str, Resource]]
) -> None:
    stack = app.node.find_child("Rhi").pools  # ty: ignore[unresolved-attribute]
    scaler = stack.resolve(stack.get_logical_id(stack.scaler.function.node.default_child))
    changed = copy.deepcopy(templates)
    changed[pool_state[0]]["Resources"].update(injected(pool_state[1], scaler))
    assert pool_state_writes(changed, pool_state) != []


def test_a_write_from_another_stack_or_an_import_is_found(
    app: cdk.App, cloud_assembly: Path, templates: Templates, pool_state: Node
) -> None:
    stage = app.node.find_child("Rhi")
    pools = stage.pools  # ty: ignore[unresolved-attribute]
    scaler = pools.resolve(pools.get_logical_id(pools.scaler.function.node.default_child))
    (export,) = [
        output["Export"]["Name"]
        for output in templates[pool_state[0]]["Outputs"].values()
        if output["Value"] == {"Fn::GetAtt": [scaler, "Arn"]}
    ]
    changed = copy.deepcopy(templates)
    access = template_path(app, cloud_assembly, stage.access)  # ty: ignore[unresolved-attribute]
    changed[access]["Resources"]["Injected"] = {
        "Type": "Custom::Injected",
        "Properties": {"ServiceToken": {"Fn::ImportValue": export}},
    }
    assert pool_state_writes(changed, pool_state) != []

    changed = copy.deepcopy(templates)
    changed[pool_state[0]]["Resources"][pool_state[1]]["Properties"]["ImportSourceSpecification"] = {}
    assert pool_state_writes(changed, pool_state) != []
