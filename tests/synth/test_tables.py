"""The five tables (system design, sections 2 and 8.5): each in its stack, with the keys, the
indexes and the expiry attribute of the shared schema `rhi_common.tables`, which the services read
too, billed on demand. Their protections are the stateful-resource invariant's
(`tests/policy/test_stateful_resources.py`).

The CDK is imported inside the fixtures, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi_common.tables import HEARTBEATS, KEYS, POOL_STATE, ROSTER, TABLES, USAGE, AttributeType, KeyAttribute, Table

if TYPE_CHECKING:
    import aws_cdk as cdk

# The stack of the stage, by its attribute, that holds each table: a table that moves is replaced.
STACK_OF = {KEYS: "access", ROSTER: "access", USAGE: "access", POOL_STATE: "pools", HEARTBEATS: "pools"}

Resource = dict[str, Any]


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
