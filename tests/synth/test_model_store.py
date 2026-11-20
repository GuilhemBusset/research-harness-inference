"""The model bucket (system design, section 4.4): one private, encrypted, retained bucket with a
fixed name, whose objects are read only through the VPC's S3 gateway endpoint, by each GPU pool's
instance role under its own pool's prefix, and written by no role of the stage.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from types import ModuleType

    import aws_cdk as cdk

    Template = Mapping[str, Any]

EXAMPLE = config.load({"RHI_CONFIG": "example"})
MOCK = next(pool for pool in EXAMPLE.pools if pool.name == "mock")
GPU_POOLS = ("coursework", "upgrade")
# The bucket's name and its ARN, as a template of an environment-agnostic stack writes them.
ACCOUNT, REGION = "${AWS::AccountId}", "${AWS::Region}"
ARN_START = "arn:${AWS::Partition}:s3:::"
OBJECT_READS = {"s3:GetObject", "s3:GetObjectVersion"}
# An S3 action that changes nothing: its name starts with one of these, whatever follows.
NOT_A_WRITE = re.compile(r"s3:(get|list|describe)[a-z*?]*", re.IGNORECASE)
PSEUDO = re.compile(r"\$\{AWS::[A-Za-z]+\}")
SUBSTITUTION = re.compile(r"\$\{[^}]*\}")


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def of_type(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r for name, r in template["Resources"].items() if r["Type"] == kind}


def written(value: Any, template: Template, name: str) -> str:
    """A value as text: a pseudo parameter as `${AWS::...}`, the model bucket (the bucket named
    `name`) by its name or its ARN, another resource of the template as `${<logical ID>}`, and
    anything the test cannot read as `*`, which may be the bucket."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ((function, argument),) = value.items()
        if function == "Fn::Join" and isinstance(argument[0], str):
            return argument[0].join(written(part, template, name) for part in argument[1])
        if function == "Fn::Sub" and isinstance(argument, str):
            return argument if all(PSEUDO.fullmatch(s) for s in SUBSTITUTION.findall(argument)) else "*"
        if function == "Ref" and isinstance(argument, str) and argument.startswith("AWS::"):
            return "${" + argument + "}"
        logical_id = argument if function == "Ref" else argument[0] if function == "Fn::GetAtt" else None
        if isinstance(logical_id, str) and logical_id in template["Resources"]:
            resource = template["Resources"][logical_id]
            named = resource.get("Properties", {}).get("BucketName")
            if resource["Type"] != "AWS::S3::Bucket" or written(named, template, name) != name:
                return "${" + logical_id + "}"
            return name if function == "Ref" else ARN_START + name
    return "*"


def may_name(pattern: str, arn: str) -> bool:
    """Whether a resource pattern of IAM may name the bucket of that ARN or one of its objects."""
    pattern, arn = (text.replace("${AWS::Partition}", "aws") for text in (pattern, arn))
    head = re.split(r"[*?]", pattern, maxsplit=1)[0]
    return pattern == arn or head.startswith(arn + "/") or (head != pattern and arn.startswith(head))


def allowed(template: Template) -> Iterator[tuple[str, list[Any], dict[str, Any]]]:
    """Every statement of the template's identity policies that allows, with the logical ID of the
    resource that holds it and the roles it is attached to."""
    for logical_id, resource in template["Resources"].items():
        properties = resource.get("Properties", {})
        if resource["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            documents = [(properties.get("Roles", []), properties["PolicyDocument"])]
        elif resource["Type"] == "AWS::IAM::Role":
            documents = [([{"Ref": logical_id}], policy["PolicyDocument"]) for policy in properties.get("Policies", [])]
        else:
            continue
        for roles, document in documents:
            for statement in as_list(document["Statement"]):
                if statement["Effect"] == "Allow":
                    yield logical_id, roles, statement


def on_the_bucket(statement: Mapping[str, Any], template: Template, name: str) -> bool:
    """Whether a statement may name the bucket or one of its objects. `NotResource` may."""
    return "Resource" not in statement or any(
        may_name(written(resource, template, name), ARN_START + name) for resource in as_list(statement["Resource"])
    )


def s3_actions(statement: Mapping[str, Any]) -> list[str]:
    """The actions of a statement that are S3's, or patterns that take one. `NotAction` may be any."""
    if "Action" not in statement:
        return ["NotAction"]
    return [a for a in as_list(statement["Action"]) if fnmatchcase("s3", a.split(":")[0].lower())]


def writers(templates: Mapping[str, Template], name: str) -> list[tuple[str, str, str]]:
    """Every write action an identity policy allows on the bucket, as `(template, logical ID of the
    policy's resource, action)`: an action of S3 that is not a read by its name."""
    return [
        (path, logical_id, action)
        for path, template in templates.items()
        for logical_id, _, statement in allowed(template)
        if on_the_bucket(statement, template, name)
        for action in s3_actions(statement)
        if not NOT_A_WRITE.fullmatch(action)
    ]


@pytest.fixture(scope="module")
def store(app_module: ModuleType) -> ModuleType:
    """`rhi.pools.model_store`, imported once the app's module has started jsii."""
    from rhi.pools import model_store

    return model_store


@pytest.fixture(scope="module")
def name(store: ModuleType) -> str:
    """The bucket's name with the account and the region left to deployment."""
    return store.bucket_name(ACCOUNT, REGION)


@pytest.fixture(scope="module")
def stage(app: cdk.App) -> Any:
    return app.node.find_child("Rhi")


@pytest.fixture(scope="module")
def pools(stage: Any, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The template of the pools stack of the example's stage."""
    return templates[(Path(stage.outdir) / stage.pools.template_file).relative_to(cloud_assembly).as_posix()]


@pytest.fixture(scope="module")
def bucket(stage: Any, pools: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The one bucket of the pools stack, which is the one the stack holds as its model store."""
    ((logical_id, resource),) = of_type(pools, "AWS::S3::Bucket").items()
    held = stage.pools.model_store.bucket.node.default_child
    assert stage.pools.resolve(held.logical_id) == logical_id
    return logical_id, resource


@pytest.fixture(scope="module")
def configured(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """The templates of the stage of a configuration with the mock pool and two GPU pools, by
    file name. Its synthesis runs cdk-nag, which fails on a finding no file acknowledges."""
    gpus = tuple(
        dataclasses.replace(
            MOCK,
            name=pool,
            engine="vllm",
            model="example/model",
            revision="a" * 40,
            instance_types=("g7e.2xlarge",),
            prices={"g7e.2xlarge": 3.36},
        )
        for pool in GPU_POOLS
    )
    configuration = dataclasses.replace(
        EXAMPLE,
        pools=(MOCK, *gpus),
        default_pool=GPU_POOLS[0],
        allowed_instance_types=(*MOCK.instance_types, "g7e.2xlarge"),
    )
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    context = json.loads(cdk_json.read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(configuration, outdir=str(tmp_path_factory.mktemp("cdk.out")), context=context)
    app.synth()
    outdir = Path(app.node.find_child("Rhi").outdir)
    found = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in outdir.glob("*.template.json")}
    assert len(found) > 1, "the stage's templates were not found"
    return found


def test_the_one_bucket_is_private_encrypted_and_retained(bucket: tuple[str, dict[str, Any]]) -> None:
    _, resource = bucket
    properties = resource["Properties"]
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    (rule,) = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] in ("AES256", "aws:kms")
    assert (resource["DeletionPolicy"], resource["UpdateReplacePolicy"]) == ("Retain", "Retain")


def test_the_name_is_the_prefix_with_the_account_and_region_left_to_deployment(
    store: ModuleType, bucket: tuple[str, dict[str, Any]]
) -> None:
    assert store.bucket_name("123456789012", "eu-west-1") == f"{store.NAME_PREFIX}-123456789012-eu-west-1"
    _, resource = bucket
    # In the template, the prefix is the only text: the account and the region are pseudo
    # parameters, which CloudFormation fills at deployment.
    assert resource["Properties"]["BucketName"] == {
        "Fn::Join": ["", [f"{store.NAME_PREFIX}-", {"Ref": "AWS::AccountId"}, "-", {"Ref": "AWS::Region"}]]
    }


def test_an_object_read_from_outside_the_endpoint_is_refused_and_no_write_is(
    stage: Any,
    cloud_assembly: Path,
    templates: dict[str, dict[str, Any]],
    pools: dict[str, Any],
    bucket: tuple[str, Any],
) -> None:
    logical_id, _ = bucket
    (policy,) = of_type(pools, "AWS::S3::BucketPolicy").values()
    assert policy["Properties"]["Bucket"] == {"Ref": logical_id}
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    # The policy allows nothing: it only refuses.
    assert {statement["Effect"] for statement in statements} == {"Deny"}
    by_endpoint = [s for s in statements if "vpc" in json.dumps(s.get("Condition", {})).lower()]
    (refusal,) = by_endpoint
    # Every object, whoever asks. No write and no listing is among the actions.
    assert refusal["Principal"] == {"AWS": "*"}
    assert refusal["Resource"] == {"Fn::Join": ["", [{"Fn::GetAtt": [logical_id, "Arn"]}, "/*"]]}
    assert set(as_list(refusal["Action"])) == OBJECT_READS
    # Refused unless the request came through one endpoint: the network stack's S3 gateway endpoint.
    ((operator, condition),) = refusal["Condition"].items()
    assert operator == "StringNotEquals"
    ((key, endpoint),) = condition.items()
    assert key.lower() == "aws:sourcevpce"
    network = templates[(Path(stage.outdir) / stage.network.template_file).relative_to(cloud_assembly).as_posix()]
    export = endpoint["Fn::ImportValue"]
    (exported,) = [o["Value"] for o in network["Outputs"].values() if o.get("Export", {}).get("Name") == export]
    (gateway_endpoint,) = of_type(network, "AWS::EC2::VPCEndpoint").items()
    assert exported == {"Ref": gateway_endpoint[0]}
    assert gateway_endpoint[1]["Properties"]["VpcEndpointType"] == "Gateway"
    assert gateway_endpoint[1]["Properties"]["ServiceName"]["Fn::Join"][1][-1] == ".s3"


def test_each_gpu_pool_reads_its_own_prefix_only(configured: dict[str, dict[str, Any]], name: str) -> None:
    (template,) = [t for t in configured.values() if of_type(t, "AWS::S3::Bucket")]
    arn = ARN_START + name
    instance_roles = {
        next(tag["Value"] for tag in role["Properties"]["Tags"] if tag["Key"] == "rhi-pool"): logical_id
        for logical_id, role in of_type(template, "AWS::IAM::Role").items()
        if "ec2.amazonaws.com" in json.dumps(role["Properties"]["AssumeRolePolicyDocument"])
    }
    assert sorted(instance_roles) == sorted((MOCK.name, *GPU_POOLS))
    # What each instance role may do on the bucket: the action, the resource and the condition.
    may: dict[str, list[tuple[str, str, Any]]] = {pool: [] for pool in instance_roles}
    for pool, role in instance_roles.items():
        for _, roles, statement in allowed(template):
            if {"Ref": role} in roles and on_the_bucket(statement, template, name):
                may[pool] += [
                    (action, written(statement.get("Resource"), template, name), statement.get("Condition"))
                    for action in s3_actions(statement)
                ]
    for pool in GPU_POOLS:
        assert sorted(may[pool], key=str) == [
            ("s3:GetObject", f"{arn}/{pool}/*", None),
            ("s3:ListBucket", arn, {"StringLike": {"s3:prefix": f"{pool}/*"}}),
        ]
    assert may[MOCK.name] == []


def test_no_role_of_the_stage_has_a_write_action_on_the_bucket(
    configured: dict[str, dict[str, Any]], name: str
) -> None:
    assert writers(configured, name) == []


# In a seeded statement's resource, the place of the bucket's ARN as text.
ARN = "<the bucket's ARN>"


@pytest.mark.parametrize(
    ("action", "resource", "found"),
    [
        ("s3:PutObject", {"Fn::Join": ["", [{"Fn::GetAtt": ["Bucket", "Arn"]}, "/coursework/*"]]}, True),
        ("s3:DeleteObject", f"{ARN}/*", True),
        ("s3:*", "*", True),
        ("*", {"Fn::ImportValue": "another-stack:Bucket"}, True),
        ("s3:GetObject", {"Fn::GetAtt": ["Bucket", "Arn"]}, False),
        ("s3:PutObject", f"{ARN}-logs/*", False),
        ("dynamodb:PutItem", "*", False),
    ],
)
def test_a_write_action_on_the_bucket_is_found(action: str, resource: Any, found: bool, name: str) -> None:
    if isinstance(resource, str):
        resource = resource.replace(ARN, ARN_START + name)
    statement = {"Effect": "Allow", "Action": action, "Resource": resource}
    template = {
        "Resources": {
            "Bucket": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": name}},
            "Policy": {"Type": "AWS::IAM::Policy", "Properties": {"PolicyDocument": {"Statement": [statement]}}},
        }
    }
    assert writers({"a.template.json": template}, name) == ([("a.template.json", "Policy", action)] if found else [])
