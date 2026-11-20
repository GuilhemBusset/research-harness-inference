"""A pool's release: its engine image, its files, the parameter that names them, and the instance
refresh that brings them to every instance (system design, sections 4.3 and 8.2).

A deployment does not reach an existing instance by itself: AWS leaves instances alone when a
launch template changes, and user data runs only at an instance's first boot (evidence E10). At
every start the boot program (`services/engine/boot/rhi-boot.sh`) reads the pool's release from one
SSM parameter, and the lifecycle agent compares it with what the instance holds
(`rhi_lifecycle.release`, which fixes the parameter's keys). This module writes that parameter
when the stack is deployed, publishes what it names, and replaces the instances when it changes.

**The engine image.** Each pool's engine image is built from its engine's Dockerfile (`ENGINES`)
as an image asset, which the pipeline's asset step builds and pushes to the bootstrap's
repository under the hash of its sources. The mock's build context is the one `rhi.packaging`
gives a Python service. The vLLM image holds no package of this project: its build context is its
own folder, `services/engine/vllm/`, whole. It is built for
the processor architecture of the pool's instance types (`architecture`); a pool whose types mix
the two architectures is refused at synthesis. The release names the image by the registry's
digest, which only the registry knows: a custom resource reads it for the asset's tag when the
stack is deployed.

**The files.** One tar archive, in the layout the boot program unpacks: `units/` (the unit files of
`services/engine/units/`), `collector/config.yaml` (the node's collector configuration) and
`agent/` (the lifecycle agent: `rhi_common`, `rhi_lifecycle` and the libraries of the lifecycle
group at their locked versions, in the layout of a function's bundle, built without Docker). The
libraries are pure Python, so the one archive serves both processor architectures, and the Python
of the agent's unit is the version the bundle is built for (`rhi.packaging.PYTHON`). CDK zips a
folder asset when it publishes it, which would leave the digest unknown at synthesis: the archive
is built at synthesis as one file and published as a file asset, which CDK uploads unchanged to
the bootstrap's bucket, and the digest the parameter names is the SHA-256 of the archive's bytes.
The archive is reproducible: its entries are sorted, with one modification time, owner and group
0 and fixed modes, and it leaves out what the installer records about its own run. The same
files give the same bytes, so a synthesis that changes nothing in them starts no refresh. As for
a function's bundle, the libraries are installed only for a stack that CDK bundles: where the
context `aws:cdk:bundling-stacks` excludes the stack, as in the tests, the archive holds
everything but the libraries and nothing is downloaded.

**The parameter.** `/rhi/pools/<pool>/release`, the name the launch template puts in the instance
tag `rhi-release-parameter`. Its value is one JSON object: the compared release (`engine_image`,
`model_revision`, `engine_flags` and `files`, with the archive's bucket, key and SHA-256), the
measured start when the configuration gives one, and the instance's settings.

**The engine flags.** The arguments the engine's container receives. A vllm pool's are what
`rhi.pools.vllm_command` builds from its configuration, with the model at the pool's prefix of the
model bucket of the stack's account and region; the mock takes none. They never hold the pool's
secret.

**The grants.** The pool's instance role may read that parameter and no other, read the archive's
object and no other object of the bucket, and pull the pool's engine image. ECR's authorization
token is granted by AWS on every resource only.

**The refresh.** A custom resource per pool carries one property, `Trigger`: the compared release
and the machine image ID the launch template resolves at deployment. CloudFormation calls the
resource when it is created and when that property changes, and at no other time: a deployment
that changes only the measured start or a setting starts no refresh, and the next start reads
them anyway. The call starts an instance refresh of the pool's group, which replaces instances in
service first and then those in the warm pool, with `ScaleInProtectedInstances` set to `Refresh`:
every instance is protected from scale-in, and a refresh waits an hour on a protected instance
and then fails otherwise. The termination hook still lets each one drain. The deployment waits
for the refresh, up to `REFRESH_TIMEOUT`, the longest CloudFormation waits for a custom resource,
and fails when the refresh does. The handlers are `rhi/pools/refresh_handler.py`, carried in the
template as it stands.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

from rhi import packaging
from rhi.pools import model_store, vllm_command
from rhi.pools.group import POOL_TAG, release_parameter
from rhi.pools.role import log_group_name
from rhi.pools.scaler import REMOTE_WRITE_PATH, STACK_ID_TAG
from rhi.pools.secret import secret_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from aws_cdk import aws_dynamodb as dynamodb

    from rhi.observability.prometheus import PrometheusWorkspace
    from rhi.pools.group import PoolGroup
    from rhi_common.pool_config import Pool

# The names of each group's lifecycle hooks, which the agent completes: fixed here, and given to
# the hooks by the issue that creates them.
LAUNCH_HOOK = "rhi-launch"
TERMINATION_HOOK = "rhi-termination"

ARM64 = "arm64"
AMD64 = "amd64"
PLATFORMS = {ARM64: ecr_assets.Platform.LINUX_ARM64, AMD64: ecr_assets.Platform.LINUX_AMD64}


@dataclass(frozen=True)
class Engine:
    """Where an engine's image is built from: its Dockerfile, under the repository, and the
    service of `rhi.packaging` whose packages and libraries the image holds, or None for an image
    that holds no package of the repository, whose build context is the Dockerfile's folder."""

    dockerfile: str
    service: str | None


# Every engine a pool may name (`rhi_common.pool_config.ENGINES`).
ENGINES = {
    "mock": Engine("services/engine/mock/Dockerfile", "mock-engine"),
    "vllm": Engine("services/engine/vllm/Dockerfile", None),
}

# What the archive holds beside the agent, under the repository.
UNITS = "services/engine/units"
COLLECTOR_CONFIG = "services/engine/collector/config.yaml"
# One modification time for every entry: 1 January 1980, the earliest every archive tool reads.
ARCHIVE_TIME = 315532800
# Left out of the archive: Python's bytecode cache, and what the installer records about its own
# run in each library's metadata, which holds no part of the library.
_CACHE = "__pycache__"
_INSTALLATION_RECORDS = ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json")

REFRESH_HANDLER = Path(__file__).with_name("refresh_handler.py")
REFRESH_RESOURCE_TYPE = "Custom::RhiInstanceRefresh"
DIGEST_RESOURCE_TYPE = "Custom::RhiImageDigest"
# The longest CloudFormation waits for a custom resource.
REFRESH_TIMEOUT = cdk.Duration.hours(1)
REFRESH_QUERY_INTERVAL = cdk.Duration.seconds(30)
# The start may wait for a refresh still running to be cancelled (`refresh_handler.CANCEL_SECONDS`).
START_TIMEOUT = cdk.Duration.minutes(14)
QUERY_TIMEOUT = cdk.Duration.minutes(1)
LOG_RETENTION = logs.RetentionDays.ONE_MONTH

PULL_ACTIONS = ("ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage")
REFRESH_ACTIONS = ("autoscaling:StartInstanceRefresh", "autoscaling:CancelInstanceRefresh")


class ReleaseError(ValueError):
    """A pool's release was refused at synthesis. The message names the pool, never a value."""


def architecture(pool: Pool) -> str:
    """The processor architecture of the pool's instance types: `arm64` or `amd64`.

    A type is arm64 when its family's name has a g among the letters after its generation number
    (`t4g`, `m7gd`), and amd64 otherwise (`g7e`). A pool whose types mix the two is refused.
    """
    found = set()
    for instance_type in pool.instance_types:
        family = instance_type.split(".", 1)[0]
        after_generation = re.sub(r"^[^0-9]*[0-9]+", "", family, flags=re.ASCII)
        found.add(ARM64 if "g" in after_generation else AMD64)
    if len(found) != 1:
        message = f"pool {pool.name!r}: its instance types mix the arm64 and amd64 processor architectures"
        raise ReleaseError(message)
    return found.pop()


def _members(root: Path) -> Iterator[tuple[str, Path]]:
    """Every folder and file under `root`, by its path under it, in the order of those paths as text."""
    for name, path in sorted((path.relative_to(root).as_posix(), path) for path in root.rglob("*")):
        relative = Path(name)
        if _CACHE in relative.parts:
            continue
        if relative.parent.name.endswith(".dist-info") and relative.name in _INSTALLATION_RECORDS:
            continue
        yield name, path


def build_archive(into: Path, *, repo: Path = packaging.REPO, libraries: bool = True) -> str:
    """Write the release's archive to the file `into`, and give the SHA-256 digest of its bytes.

    Without `libraries` the agent is its two packages alone, and nothing is installed.
    """
    with tempfile.TemporaryDirectory(prefix="rhi-release-") as scratch:
        root = Path(scratch)
        shutil.copytree(repo / UNITS, root / "units")
        (root / "collector").mkdir()
        shutil.copyfile(repo / COLLECTOR_CONFIG, root / "collector" / "config.yaml")
        agent = root / "agent"
        agent.mkdir()
        if libraries:
            # Pure Python: the platform names the wheels uv may take, and each library has one for any.
            packaging.bundle("lifecycle", packaging.PLATFORMS["arm64"], agent, repo=repo)
        else:
            packaging.copy_packages("lifecycle", repo, agent)
        with tarfile.open(into, "w", format=tarfile.GNU_FORMAT) as archive:
            for name, path in _members(root):
                entry = tarfile.TarInfo(name)
                entry.mtime = ARCHIVE_TIME
                if path.is_dir():
                    entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                    archive.addfile(entry)
                else:
                    entry.size, entry.mode = path.stat().st_size, 0o644
                    with path.open("rb") as stream:
                        archive.addfile(entry, stream)
    return hashlib.sha256(into.read_bytes()).hexdigest()


class ReleaseFiles(Construct):
    """The archive every pool's release names, as the file asset `asset`, and the SHA-256 digest
    of its bytes, as `sha256`."""

    def __init__(self, scope: Construct, construct_id: str, *, repo: Path = packaging.REPO) -> None:
        super().__init__(scope, construct_id)
        # Outside the repository, and removed when the process ends: CDK copies the file into the
        # cloud assembly when the asset is created.
        folder = Path(tempfile.mkdtemp(prefix="rhi-release-files-"))
        atexit.register(shutil.rmtree, folder, ignore_errors=True)
        archive = folder / "release.tar"
        self.sha256 = build_archive(archive, repo=repo, libraries=cdk.Stack.of(self).bundling_required)
        # A file, not a folder: CDK publishes it as it is.
        self.asset = s3_assets.Asset(self, "Archive", path=str(archive))


class InstanceRefresher(Construct):
    """What runs the pools' instance refreshes: the two functions of `refresh_handler.py`, their
    role, as `role`, and the provider that calls them and waits, whose `service_token` a pool's
    custom resource names."""

    def __init__(self, scope: Construct, construct_id: str, *, pools: Sequence[Pool]) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        # Its own role, with no AWS managed policy: what the functions may do is listed here, whole.
        lambda_service = iam.ServicePrincipal("lambda.amazonaws.com")
        self.role = iam.Role(self, "Role", assumed_by=lambda_service)  # ty: ignore[invalid-argument-type] - jsii passes `grantee` by position (rhi.pools.role)
        log_group = logs.LogGroup(self, "Logs", retention=LOG_RETENTION, removal_policy=cdk.RemovalPolicy.DESTROY)
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[log_group.log_group_arn]
            )
        )
        # Every group, by the pattern of its ARN, narrowed to this stack's pools by the groups' tags.
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=list(REFRESH_ACTIONS),
                resources=[
                    stack.format_arn(
                        service="autoscaling",
                        resource="autoScalingGroup",
                        resource_name="*:autoScalingGroupName/*",
                        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                    )
                ],
                conditions={
                    "StringEquals": {
                        f"autoscaling:ResourceTag/{STACK_ID_TAG}": stack.stack_id,
                        f"autoscaling:ResourceTag/{POOL_TAG}": [pool.name for pool in pools],
                    }
                },
            )
        )
        # AWS grants the reading of refreshes on every resource only.
        self.role.add_to_policy(iam.PolicyStatement(actions=["autoscaling:DescribeInstanceRefreshes"], resources=["*"]))
        code = lambda_.Code.from_inline(REFRESH_HANDLER.read_text(encoding="utf-8"))
        start, query = (
            lambda_.Function(
                self,
                name,
                runtime=packaging.RUNTIME,
                handler=f"index.{handler}",
                code=code,
                role=self.role,  # ty: ignore[invalid-argument-type] - as above
                log_group=log_group,
                timeout=timeout,
            )
            for name, handler, timeout in (
                ("Start", "on_event", START_TIMEOUT),
                ("Query", "is_complete", QUERY_TIMEOUT),
            )
        )
        # The CDK's provider framework: three functions of its own and a state machine that calls
        # `Query` until the refresh has ended. Their roles and their one log group are made here,
        # so that they hold no AWS managed policy and write to no log group but that one. The
        # function that starts the state machine has a role of its own: the state machine names the
        # two others, so one role for the three would depend on itself.
        framework_logs = logs.LogGroup(
            self, "FrameworkLogs", retention=LOG_RETENTION, removal_policy=cdk.RemovalPolicy.DESTROY
        )
        framework_roles = [
            iam.Role(self, name, assumed_by=lambda_service)  # ty: ignore[invalid-argument-type] - as above
            for name in ("FrameworkStartRole", "FrameworkQueryRole")
        ]
        for framework_role in framework_roles:
            framework_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[framework_logs.log_group_arn]
                )
            )
        self.provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=start,  # ty: ignore[invalid-argument-type] - as above
            is_complete_handler=query,  # ty: ignore[invalid-argument-type] - as above
            query_interval=REFRESH_QUERY_INTERVAL,
            total_timeout=REFRESH_TIMEOUT,
            framework_on_event_role=framework_roles[0],  # ty: ignore[invalid-argument-type] - as above
            framework_complete_and_timeout_role=framework_roles[1],  # ty: ignore[invalid-argument-type] - as above
            log_group=framework_logs,
        )
        self.service_token = self.provider.service_token
        # Each function and the state machine name their role, and the CDK makes them depend on the
        # role as well, which cfn-lint warns about (W3005): each keeps its dependency on the
        # role's policy alone.
        waiter = self.provider.node.find_child("waiter-state-machine")
        waiter_role = waiter.node.find_child("Role")
        assert isinstance(waiter_role, iam.Role)
        for resource, role in (
            (start, self.role),
            (query, self.role),
            (self.provider.node.find_child("framework-onEvent"), framework_roles[0]),
            (self.provider.node.find_child("framework-isComplete"), framework_roles[1]),
            (self.provider.node.find_child("framework-onTimeout"), framework_roles[1]),
            (waiter, waiter_role),
        ):
            policy = role.node.find_child("DefaultPolicy").node.default_child
            named = resource.node.find_child("Resource")
            assert isinstance(policy, cdk.CfnResource) and isinstance(named, cdk.CfnResource)
            named.add_override("DependsOn", [stack.get_logical_id(policy)])


class PoolRelease(Construct):
    """One pool's release: its engine image, as the image asset `image`, its parameter, as
    `parameter`, the policy that lets the pool's instances fetch the release, as `policy`, and the
    custom resource that refreshes the pool's group, as `refresh`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pool: Pool,
        files: ReleaseFiles,
        refresher: InstanceRefresher,
        group: PoolGroup,
        role: iam.Role,
        heartbeats: dynamodb.ITable,
        workspace: PrometheusWorkspace,
        repo: Path = packaging.REPO,
    ) -> None:
        super().__init__(scope, construct_id)
        platform = PLATFORMS[architecture(pool)]
        engine = ENGINES[pool.engine]
        dockerfile = repo / engine.dockerfile
        self.image = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=(
                str(dockerfile.parent)
                if engine.service is None
                else packaging.image_context(engine.service, dockerfile, repo=repo)
            ),
            platform=platform,
        )
        repository = self.image.repository

        # The registry's digest of the image the asset step pushed under the asset's tag, read
        # when the stack is deployed and again when the tag changes.
        digest_call = cr.AwsSdkCall(
            service="ECR",
            action="describeImages",
            parameters={"repositoryName": repository.repository_name, "imageIds": [{"imageTag": self.image.image_tag}]},
            physical_resource_id=cr.PhysicalResourceId.of(f"rhi-{pool.name}-engine-image"),
            output_paths=["imageDetails.0.imageDigest"],
        )
        self.digest = cr.AwsCustomResource(
            self,
            "ImageDigest",
            resource_type=DIGEST_RESOURCE_TYPE,
            on_update=digest_call,
            install_latest_aws_sdk=False,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [iam.PolicyStatement(actions=["ecr:DescribeImages"], resources=[repository.repository_arn])]
            ),
        )
        engine_image = f"{repository.repository_uri}@{self.digest.get_response_field('imageDetails.0.imageDigest')}"

        # The arguments of the engine's container. The model bucket's name holds the account and
        # the region, which deployment resolves in the parameter and in the trigger.
        stack = cdk.Stack.of(self)
        engine_flags = (
            vllm_command.arguments(pool, bucket=model_store.bucket_name(stack.account, stack.region))
            if pool.engine == "vllm"
            else []
        )

        # What an instance must hold, compared by the agent.
        document: dict[str, object] = {
            "engine_image": engine_image,
            "model_revision": pool.revision,
            "engine_flags": engine_flags,
            "files": {
                "bucket": files.asset.s3_bucket_name,
                "key": files.asset.s3_object_key,
                "sha256": files.sha256,
            },
        }
        # Outside the comparison: the measured start, and what the programs of the instance read.
        if pool.measured_start_seconds is not None:
            document["measured_start_seconds"] = pool.measured_start_seconds
        document["settings"] = {
            "pool": pool.name,
            "group": group.group.ref,
            "launch_hook": LAUNCH_HOOK,
            "termination_hook": TERMINATION_HOOK,
            "log_group": log_group_name(pool.name),
            "workspace_url": cdk.Fn.join("", [workspace.workspace.attr_prometheus_endpoint, REMOTE_WRITE_PATH]),
            "heartbeat_table": heartbeats.table_name,
            "secret_id": secret_name(pool.name),
        }
        self.parameter = ssm.StringParameter(
            self,
            "Parameter",
            parameter_name=release_parameter(pool.name),
            description=f"The current release of the pool {pool.name}.",
            string_value=_json(document),
        )

        self.policy = iam.Policy(
            self,
            "Policy",
            statements=[
                iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[self.parameter.parameter_arn]),
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[files.asset.bucket.arn_for_objects(files.asset.s3_object_key)],
                ),
                iam.PolicyStatement(actions=list(PULL_ACTIONS), resources=[repository.repository_arn]),
                # AWS grants the registry's authorization token on every resource only.
                iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
            ],
            roles=[role],  # ty: ignore[invalid-argument-type] - jsii passes `grantee` by position (rhi.pools.role)
        )

        # What a change must bring to every instance: the compared release (the image by its digest
        # and by the tag it was pushed under, the model revision, the flags and the files' digest)
        # and the machine image ID the launch template resolves at deployment.
        trigger = {
            "engine_image": engine_image,
            "image_tag": self.image.image_tag,
            "model_revision": pool.revision,
            "engine_flags": engine_flags,
            "files_sha256": files.sha256,
            "machine_image": group.launch_template.image_id,
        }
        self.refresh = cdk.CustomResource(
            self,
            "Refresh",
            service_token=refresher.service_token,
            resource_type=REFRESH_RESOURCE_TYPE,
            properties={"Group": group.group.ref, "Trigger": _json(trigger)},
        )
        # The release is written, and the instances may fetch it, before any instance is replaced.
        self.refresh.node.add_dependency(self.parameter, self.policy)


def _json(document: object) -> str:
    """A JSON text whose strings may hold references, which deployment resolves in place.

    Every reference here stands for a name, an address or a digest, which JSON writes as it is.
    """
    return json.dumps(document, separators=(",", ":"))
