"""Both load balancers' access logs, in one bucket that keeps them 90 days (system design,
sections 6.6 and 10): each load balancer under a prefix of its own, the bucket private and
encrypted with S3-managed keys, its policy open to Elastic Load Balancing's log-delivery service
alone, and each load balancer deployed after that policy.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import aws_cdk as cdk

LOAD_BALANCER = "AWS::ElasticLoadBalancingV2::LoadBalancer"
LOG_DELIVERY = {"Service": "logdelivery.elasticloadbalancing.amazonaws.com"}


@dataclass(frozen=True)
class Stage:
    network: dict[str, Any]
    pools: dict[str, Any]
    manifest: dict[str, Any]
    network_id: str
    pools_id: str


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r for name, r in template["Resources"].items() if r["Type"] == kind}


def attributes(balancer: dict[str, Any]) -> dict[str, Any]:
    return {a["Key"]: a["Value"] for a in balancer["Properties"]["LoadBalancerAttributes"]}


@pytest.fixture(scope="module")
def stage(app: cdk.App) -> Stage:
    rhi = app.node.find_child("Rhi")
    outdir = Path(rhi.outdir)  # ty: ignore[missing-attribute] - the app's stage, typed as a construct

    def read(stack: Any) -> dict[str, Any]:
        return json.loads((outdir / stack.template_file).read_text(encoding="utf-8"))

    network, pools = rhi.network, rhi.pools  # ty: ignore[missing-attribute]
    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    return Stage(read(network), read(pools), manifest, network.artifact_id, pools.artifact_id)


def bucket_of(stage: Stage) -> tuple[str, dict[str, Any]]:
    [(name, bucket)] = of_type(stage.network, "AWS::S3::Bucket").items()
    return name, bucket


def test_both_load_balancers_log_to_the_bucket_under_different_prefixes(stage: Stage) -> None:
    name, _ = bucket_of(stage)
    [public] = of_type(stage.network, LOAD_BALANCER).values()
    [internal] = of_type(stage.pools, LOAD_BALANCER).values()
    public_logs, internal_logs = attributes(public), attributes(internal)
    assert public_logs["access_logs.s3.enabled"] == internal_logs["access_logs.s3.enabled"] == "true"
    assert public_logs["access_logs.s3.bucket"] == {"Ref": name}
    # The internal load balancer imports the network stack's export of the bucket's name.
    [export] = [
        output["Export"]["Name"] for output in stage.network["Outputs"].values() if output["Value"] == {"Ref": name}
    ]
    assert internal_logs["access_logs.s3.bucket"] == {"Fn::ImportValue": export}
    assert public_logs["access_logs.s3.prefix"] != internal_logs["access_logs.s3.prefix"]


def test_the_bucket_is_private_encrypted_by_s3_and_expires_objects_after_90_days(stage: Stage) -> None:
    _, bucket = bucket_of(stage)
    properties = bucket["Properties"]
    [rule] = properties["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled"
    assert rule["ExpirationInDays"] == 90
    assert not {"Prefix", "TagFilters", "ObjectSizeGreaterThan", "ObjectSizeLessThan"} & rule.keys()
    # Unversioned: an expired object is gone, not kept as a noncurrent version.
    assert "VersioningConfiguration" not in properties
    [encryption] = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert encryption["ServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
    assert all(properties["PublicAccessBlockConfiguration"].values())
    assert len(properties["PublicAccessBlockConfiguration"]) == 4
    assert bucket["DeletionPolicy"] == "Retain"


def test_the_policy_lets_only_the_log_delivery_service_write_under_the_prefixes(stage: Stage) -> None:
    name, _ = bucket_of(stage)
    [policy] = of_type(stage.network, "AWS::S3::BucketPolicy").values()
    assert policy["Properties"]["Bucket"] == {"Ref": name}
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert statements
    assert all(s["Principal"] == LOG_DELIVERY for s in statements)
    allowed = [s for s in statements if s["Effect"] == "Allow"]
    [public] = of_type(stage.network, LOAD_BALANCER).values()
    [internal] = of_type(stage.pools, LOAD_BALANCER).values()
    resources = [r for s in allowed for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    for balancer in (public, internal):
        prefix = attributes(balancer)["access_logs.s3.prefix"]
        # Under the prefix, below `AWSLogs/` and the stack's own account, the one account named.
        assert {
            "Fn::Join": ["", [{"Fn::GetAtt": [name, "Arn"]}, f"/{prefix}/AWSLogs/", {"Ref": "AWS::AccountId"}, "/*"]]
        } in resources


def test_the_policy_comes_first(stage: Stage) -> None:
    [policy] = of_type(stage.network, "AWS::S3::BucketPolicy")
    [public] = of_type(stage.network, LOAD_BALANCER).values()
    assert policy in public["DependsOn"]
    assert stage.network_id in stage.manifest["artifacts"][stage.pools_id]["dependencies"]
