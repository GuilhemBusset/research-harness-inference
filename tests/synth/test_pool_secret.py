"""A pool's bearer secret (system design, sections 4.1 and 4.3): one generated secret per pool,
read by its own pool's instance role and by no other pool's, and its value in no template.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    import aws_cdk as cdk

EXAMPLE = config.load({"RHI_CONFIG": "example"})
(MOCK,) = EXAMPLE.pools
SECRET = "AWS::SecretsManager::Secret"
SECRET_READS = {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def of_type(template: Mapping[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


def pool_of(properties: Mapping[str, Any]) -> str:
    (pool,) = [tag["Value"] for tag in properties["Tags"] if tag["Key"] == "rhi-pool"]
    return pool


def secret_named(resource: Any) -> str:
    """The name of the secret a statement's resource names: its ARN, written with pseudo
    parameters, ends with the name and the six characters Secrets Manager adds to it."""
    parts = resource["Fn::Join"][1]
    text = "".join(part if isinstance(part, str) else "${}" for part in parts)
    assert text.startswith("arn:${}:secretsmanager:${}:${}:secret:")
    assert text.endswith("-??????")
    return text.split(":secret:", 1)[1].removesuffix("-??????")


def secret_readers(template: Mapping[str, Any]) -> dict[str, set[str]]:
    """For each secret of the template, by its logical ID, the logical IDs of the roles whose
    policies may read it."""
    by_name = {secret["Name"]: name for name, secret in of_type(template, SECRET).items()}
    readers: dict[str, set[str]] = {name: set() for name in by_name.values()}
    for policy in of_type(template, "AWS::IAM::Policy").values():
        for statement in as_list(policy["PolicyDocument"]["Statement"]):
            if not set(as_list(statement["Action"])) & SECRET_READS:
                continue
            for resource in as_list(statement["Resource"]):
                for role in policy["Roles"]:
                    readers[by_name[secret_named(resource)]].add(role["Ref"])
    return readers


def value_holders(templates: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Where a template holds a secret's value: a value written in the template, a dynamic
    reference that CloudFormation resolves to the value, or a build environment variable that
    CodeBuild fills with it."""
    found = []
    for path, template in templates.items():
        text = json.dumps(template)
        if "{{resolve:secretsmanager" in text:
            found.append(f"{path}: a dynamic reference to a secret")
        for name, properties in of_type(template, SECRET).items():
            if "SecretString" in properties or "GenerateSecretString" not in properties:
                found.append(f"{path}: {name} is not generated")
        for name, project in of_type(template, "AWS::CodeBuild::Project").items():
            variables = project.get("Environment", {}).get("EnvironmentVariables", [])
            if any(variable.get("Type") == "SECRETS_MANAGER" for variable in variables):
                found.append(f"{path}: {name} reads a secret into its environment")
            if "secrets-manager" in json.dumps(project.get("Source", {}).get("BuildSpec", "")):
                found.append(f"{path}: {name}'s build spec reads a secret")
    return found


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    return templates[path]


def test_one_generated_secret_for_the_mock_pool_exposed_by_the_stack(app: cdk.App, pools: dict[str, Any]) -> None:
    ((name, secret),) = of_type(pools, SECRET).items()
    assert secret["Name"] == "/rhi/pools/mock/bearer"
    assert pool_of(secret) == "mock"
    generated = secret["GenerateSecretString"]
    assert generated["ExcludePunctuation"] is True
    assert generated["PasswordLength"] >= 32
    # Another construct reaches the secret through the stack, by the pool's name.
    stack = app.node.find_child("Rhi").pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    assert stack.resolve(stack.secrets["mock"].secret.secret_arn) == {"Ref": name}


def test_no_template_of_the_app_holds_a_secrets_value(templates: dict[str, dict[str, Any]]) -> None:
    assert value_holders(templates) == []


def test_detects_a_secrets_value_written_out(app_module: ModuleType) -> None:
    import aws_cdk as cdk
    from aws_cdk import aws_iam as iam
    from aws_cdk import aws_ssm as ssm

    from rhi.pools.secret import PoolSecret

    stack = cdk.Stack()
    role = iam.Role(stack, "Role", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))  # ty: ignore[invalid-argument-type]
    secret = PoolSecret(stack, "Secret", pool="mock", role=role).secret
    ssm.StringParameter(stack, "Leak", string_value=secret.secret_value.unsafe_unwrap())
    template = cdk.assertions.Template.from_stack(stack).to_json()
    assert value_holders({"built": template}) == ["built: a dynamic reference to a secret"]


def test_each_pools_secret_is_read_by_its_own_instance_role_and_by_no_other_pools(
    app_module: ModuleType, tmp_path: Path
) -> None:
    """Two pools: the app passes cdk-nag, and each secret is read by its own pool's instance role
    and by no other pool's."""
    pools = (MOCK, dataclasses.replace(MOCK, name="second"))
    configured = dataclasses.replace(EXAMPLE, pools=pools)
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    context = json.loads(cdk_json.read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(configured, outdir=str(tmp_path), context=context)
    app.synth()  # Fails on a finding no file acknowledges.
    stage = app.node.find_child("Rhi")
    template = json.loads((Path(stage.outdir) / stage.pools.template_file).read_text(encoding="utf-8"))
    secrets = of_type(template, SECRET)
    assert sorted(pool_of(secret) for secret in secrets.values()) == ["mock", "second"]
    instance_roles = {
        name: pool_of(role)
        for name, role in of_type(template, "AWS::IAM::Role").items()
        if "ec2.amazonaws.com" in json.dumps(role["AssumeRolePolicyDocument"])
    }
    readers = {
        pool_of(secrets[name]): {instance_roles[role] for role in roles}
        for name, roles in secret_readers(template).items()
    }
    assert readers == {"mock": {"mock"}, "second": {"second"}}


def test_another_stack_may_be_allowed_to_read_a_pools_secret(app_module: ModuleType) -> None:
    """The gateway's role, in a stack of its own, reads a pool's secret once granted, by the
    secret's name."""
    import aws_cdk as cdk
    from aws_cdk import aws_iam as iam

    from rhi.pools.secret import PoolSecret

    app = cdk.App()
    pools = cdk.Stack(app, "Pools")
    role = iam.Role(pools, "Instances", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))  # ty: ignore[invalid-argument-type]
    secret = PoolSecret(pools, "Secret", pool="mock", role=role)
    gateway = cdk.Stack(app, "Gateway")
    reader = iam.Role(gateway, "Task", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))  # ty: ignore[invalid-argument-type]
    secret.grant_read(reader)
    reading = cdk.assertions.Template.from_stack(gateway).to_json()
    (policy,) = of_type(reading, "AWS::IAM::Policy").values()
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert set(as_list(statement["Action"])) == SECRET_READS
    assert secret_named(statement["Resource"]) == "/rhi/pools/mock/bearer"
    # The grant names the secret without a reference to the pools stack.
    assert "Fn::ImportValue" not in json.dumps(reading)
