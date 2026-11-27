"""A pool's release: its engine image, its files and the parameter that names them (system design,
sections 4.3 and 8.2).

A deployment replaces no instance, and needs none replaced: a pool has no instance outside a
session, and a deployment waits while a session is active, so a release finds nothing to reach. At
every start the boot program (`services/engine/boot/rhi-boot.sh`) reads the pool's release from one
SSM parameter, and the lifecycle agent compares it with what the instance holds
(`rhi_lifecycle.release`, which fixes the parameter's keys). This module writes that parameter
when the stack is deployed and publishes what it names: the next session's instances read it at
their start. An instance in service when the parameter changes keeps what it started with until it
leaves, and its heartbeat says which release it serves.

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
`services/engine/units/`), `collector/config.yaml` (the node's collector configuration),
`dcgm/counters.csv` (the fields of the DCGM exporter, which a GPU instance's unit reads) and
`agent/` (the lifecycle agent: `rhi_common`, `rhi_lifecycle` and the libraries of the lifecycle
group at their locked versions, in the layout of a function's bundle, built without Docker). The
libraries are pure Python, so the one archive serves both processor architectures, and the Python
of the agent's unit is the version the bundle is built for (`rhi.packaging.PYTHON`). CDK zips a
folder asset when it publishes it, which would leave the digest unknown at synthesis: the archive
is built at synthesis as one file and published as a file asset, which CDK uploads unchanged to
the bootstrap's bucket, and the digest the parameter names is the SHA-256 of the archive's bytes.
Beside the agent, `contract/` carries the engine contract suite to the node, where the document
`rhi-engine-contract` runs it against the node's own engine (`rhi.pools.contract_check`): the
suite's folder as the repository has it, at `contract/tests/contract/`, the files of
`services/engine/contract/` at its root, and `rhi_common`, `rhi_gateway`, `rhi_mock_engine` and
the libraries of the contract group at their locked versions, in the same layout as the agent's
and pure Python as well.
The archive is reproducible: its entries are sorted, with one modification time, owner and group
0 and fixed modes, and it leaves out what the installer records about its own run. The same
files give the same bytes, so a synthesis that changes nothing in them leaves the parameter as
it was. As for a function's bundle, the libraries are installed only for a stack that CDK
bundles: where the context `aws:cdk:bundling-stacks` excludes the stack, as in the tests, the
archive holds everything but the libraries, the agent's and the suite's, and nothing is downloaded.

**The parameter.** `/rhi/pools/<pool>/release`, the name the launch template puts in the instance
tag `rhi-release-parameter`. Its value is one JSON object: the compared release (`engine_image`,
`model_revision`, `engine_flags` and `files`, with the archive's bucket, key and SHA-256), the
measured start when the configuration gives one, and the instance's settings. Among the settings
is the pool's engine, by its name in `rhi_common.engine_metrics`: the lifecycle agent reads the
engine's `/metrics` under that engine's names, and an instance has no other way to know them.

**The engine flags.** The arguments the engine's container receives. A vllm pool's are what
`rhi.pools.vllm_command` builds from its configuration, with the model at the pool's prefix of the
model bucket of the stack's account and region. A mock pool has none: the mock engine takes no
argument. They never hold the pool's secret.

**The grants.** The pool's instance role may read that parameter and no other, read the archive's
object and no other object of the bucket, and pull the pool's engine image. ECR's authorization
token is granted by AWS on every resource only.
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
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

from rhi import packaging
from rhi.pools import model_store, vllm_command
from rhi.pools.group import release_parameter
from rhi.pools.role import log_group_name
from rhi.pools.scaler import REMOTE_WRITE_PATH
from rhi.pools.secret import secret_name

if TYPE_CHECKING:
    from collections.abc import Iterator

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
DCGM_FIELDS = "services/engine/dcgm/counters.csv"
# The contract suite, under `contract/` as under the repository, and the files that run it.
CONTRACT_SUITE = "tests/contract"
CONTRACT_FILES = "services/engine/contract"
# One modification time for every entry: 1 January 1980, the earliest every archive tool reads.
ARCHIVE_TIME = 315532800
# Left out of the archive: Python's bytecode cache, and what the installer records about its own
# run in each library's metadata, which holds no part of the library.
_CACHE = "__pycache__"
_INSTALLATION_RECORDS = ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json")

DIGEST_RESOURCE_TYPE = "Custom::RhiImageDigest"

PULL_ACTIONS = ("ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage")


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

    Without `libraries` the agent is its two packages alone, the suite its packages and its
    files, and nothing is installed.
    """
    with tempfile.TemporaryDirectory(prefix="rhi-release-") as scratch:
        root = Path(scratch)
        shutil.copytree(repo / UNITS, root / "units")
        (root / "collector").mkdir()
        shutil.copyfile(repo / COLLECTOR_CONFIG, root / "collector" / "config.yaml")
        (root / "dcgm").mkdir()
        shutil.copyfile(repo / DCGM_FIELDS, root / "dcgm" / "counters.csv")
        agent = root / "agent"
        agent.mkdir()
        if libraries:
            # Pure Python: the platform names the wheels uv may take, and each library has one for any.
            packaging.bundle("lifecycle", packaging.PLATFORMS["arm64"], agent, repo=repo)
        else:
            packaging.copy_packages("lifecycle", repo, agent)
        contract = root / "contract"
        contract.mkdir()
        if libraries:
            packaging.bundle("contract", packaging.PLATFORMS["arm64"], contract, repo=repo)
        else:
            packaging.copy_packages("contract", repo, contract)
        shutil.copytree(repo / CONTRACT_SUITE, contract / CONTRACT_SUITE, ignore=shutil.ignore_patterns(_CACHE))
        for file in sorted((repo / CONTRACT_FILES).iterdir()):
            if file.is_file():
                shutil.copyfile(file, contract / file.name)
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


class PoolRelease(Construct):
    """One pool's release: its engine image, as the image asset `image`, its parameter, as
    `parameter`, and the policy that lets the pool's instances fetch the release, as `policy`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pool: Pool,
        files: ReleaseFiles,
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

        # The arguments of the engine's container: a mock pool's engine takes none. The model
        # bucket's name holds the account and the region, which deployment resolves in the parameter.
        stack = cdk.Stack.of(self)
        engine_flags: list[str] = []
        if pool.engine == "vllm":
            engine_flags = vllm_command.arguments(pool, bucket=model_store.bucket_name(stack.account, stack.region))

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
            "engine": pool.engine,
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


def _json(document: object) -> str:
    """A JSON text whose strings may hold references, which deployment resolves in place.

    Every reference here stands for a name, an address or a digest, which JSON writes as it is.
    """
    return json.dumps(document, separators=(",", ":"))
