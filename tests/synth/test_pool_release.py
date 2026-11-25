"""A pool's release (`rhi.pools.release`; system design, sections 4.3 and 8.2): the parameter its
instances read, the archive and the engine image it names, and what the instance role may fetch.
The stack starts no instance refresh: a deployment replaces no instance.

The mock pool of the example configuration is read from the app the session synthesizes. What
needs two syntheses to compare, or another pool, builds a stack that holds one pool and its
release, from a copy of the sources when a source file changes. The CDK is imported inside the
tests, after `app_module` has started jsii (see conftest.py). Every digest and name that stands
for a value of a deployment is made at run time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import threading
import tomllib
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config
from rhi_common import pool_config
from rhi_lifecycle import release as agent_release

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    import aws_cdk as cdk

    from rhi_common.pool_config import Pool

REPO = Path(__file__).resolve().parents[2]
BOOT = REPO / "services/engine/boot/rhi-boot.sh"
EXAMPLE = config.load({"RHI_CONFIG": "example"})
MOCK = next(pool for pool in EXAMPLE.pools if pool.name == "mock")
GPU = dataclasses.replace(
    MOCK,
    name="coursework",
    engine="vllm",
    model="example/model",
    revision="a" * 40,
    instance_types=("g7e.2xlarge",),
    prices={"g7e.2xlarge": 3.36},
)

PARAMETER = "/rhi/pools/mock/release"
REFRESH = "Custom::RhiInstanceRefresh"
UNITS = {"rhi-collector.service", "rhi-engine.service", "rhi-lifecycle.service"}
# The units of a GPU instance only, in a folder of their own, which the boot program installs on no other.
GPU_UNITS = {"gpu/rhi-engine.service.d/gpu.conf", "gpu/rhi-dcgm.service", "gpu/rhi-collector.service.d/dcgm.conf"}
# What the sources of a release are, for a copy of them: the lock, the services and their images.
SOURCES = ("pyproject.toml", "uv.lock", "services/common", "services/engine")

REGION = "eu-west-3"
PSEUDO = {
    "AWS::AccountId": "123456789012",
    "AWS::Region": REGION,
    "AWS::Partition": "aws",
    "AWS::URLSuffix": "amazonaws.com",
}
WORKSPACE = "https://aps-workspaces.example.com/workspaces/ws-sample/"
IMAGE_DIGEST = "sha256:" + hashlib.sha256(secrets.token_bytes(16)).hexdigest()


def of_type(template: Mapping[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def sample(value: Any) -> str:
    """A value of a template with each reference replaced by a sample value: a resource's `Ref`
    by `sample-<logical ID>`, the image's digest by a digest, the workspace's endpoint by an
    address of example.com."""
    if isinstance(value, str):
        return value
    ((function, argument),) = value.items()
    if function == "Fn::Join":
        return argument[0].join(sample(part) for part in argument[1])
    if function == "Ref":
        return PSEUDO.get(argument, f"sample-{argument}")
    if function == "Fn::Sub":
        return re.sub(r"\$\{([^}]+)\}", lambda match: PSEUDO[match[1]], argument)
    if function == "Fn::GetAtt" and argument[1] == "imageDetails.0.imageDigest":
        return IMAGE_DIGEST
    if (function, argument[1:]) == ("Fn::GetAtt", ["PrometheusEndpoint"]) or (
        function == "Fn::ImportValue" and "PrometheusEndpoint" in argument
    ):
        return WORKSPACE
    raise AssertionError(f"no sample value for {function}")


def references(value: Any) -> list[Any]:
    """Every `Ref` in a value of a template."""
    if isinstance(value, dict):
        return [value["Ref"]] if list(value) == ["Ref"] else [r for part in value.values() for r in references(part)]
    return [r for part in value for r in references(part)] if isinstance(value, list) else []


@dataclasses.dataclass(frozen=True)
class Release:
    """One pool's release, as synthesis wrote it."""

    template: Mapping[str, Any]
    outdir: Path
    # The asset manifest of the pool's stack.
    assets: Mapping[str, Any]
    pool: str

    @property
    def parameter(self) -> dict[str, Any]:
        (found,) = [
            p
            for p in of_type(self.template, "AWS::SSM::Parameter").values()
            if p["Name"] == f"/rhi/pools/{self.pool}/release"
        ]
        return found

    def tagged(self, kind: str) -> str:
        """The logical ID of the one resource of a kind that is tagged with the pool's name: a
        template may hold other pools, each with its own group and its own instance role."""
        (found,) = [
            name
            for name, properties in of_type(self.template, kind).items()
            if [tag["Value"] for tag in properties.get("Tags", []) if tag["Key"] == "rhi-pool"] == [self.pool]
        ]
        return found

    @property
    def group(self) -> str:
        """The logical ID of the pool's group."""
        return self.tagged("AWS::AutoScaling::AutoScalingGroup")

    @property
    def value(self) -> dict[str, Any]:
        """The parameter's value, with each reference replaced by a sample value."""
        return json.loads(sample(self.parameter["Value"]))

    @property
    def archive(self) -> Path:
        """The file of the release's archive in the cloud assembly."""
        (source,) = [
            file["source"] for file in self.assets["files"].values() if file["source"]["path"].endswith(".tar")
        ]
        assert source["packaging"] == "file"
        return (self.outdir / source["path"]).resolve()

    @property
    def digest_lookup(self) -> tuple[str, str]:
        """The custom resource that reads the engine image's digest from the registry when the stack
        is deployed: its logical ID, and the call it makes."""
        (found,) = [
            (name, sample(properties["Update"]))
            for name, properties in of_type(self.template, "Custom::RhiImageDigest").items()
            if f"rhi-{self.pool}-engine-image" in sample(properties["Update"])
        ]
        return found

    @property
    def image_asset(self) -> tuple[str, dict[str, Any]]:
        """The pool's engine image asset, with its tag: the one pushed under the tag the release
        reads the digest of."""
        (found,) = [(tag, a) for tag, a in self.assets["dockerImages"].items() if tag in self.digest_lookup[1]]
        return found

    @property
    def image(self) -> dict[str, Any]:
        """The source of the pool's engine image asset."""
        call = json.loads(self.digest_lookup[1])
        assert (call["service"], call["action"]) == ("ECR", "describeImages")
        tag, asset = self.image_asset
        (destination,) = asset["destinations"].values()
        assert call["parameters"] == {
            "repositoryName": sample({"Fn::Sub": destination["repositoryName"]}),
            "imageIds": [{"imageTag": tag}],
        }
        return asset["source"]


@pytest.fixture(scope="module")
def mock(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> Release:
    """The mock pool's release in the app."""
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[missing-attribute] - the app's stage, typed as a construct
    outdir = Path(stage.outdir)  # ty: ignore[missing-attribute]
    assets = json.loads((outdir / f"{stack.artifact_id}.assets.json").read_text(encoding="utf-8"))
    return Release(
        templates[(outdir / stack.template_file).relative_to(cloud_assembly).as_posix()], outdir, assets, "mock"
    )


def copy_of_the_sources(into: Path) -> Path:
    for source in SOURCES:
        if (REPO / source).is_dir():
            shutil.copytree(REPO / source, into / source, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            into.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / source, into / source)
    return into


def standalone(
    app_module: ModuleType,
    outdir: Path,
    pool: Pool = MOCK,
    *,
    repo: Path = REPO,
    heartbeats: str = "Heartbeats",
    unrelated: bool = False,
    bundled: bool = False,
) -> Release:
    """A stack that holds only this pool's group and its release, synthesized into `outdir`."""
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from aws_cdk import aws_iam as iam
    from aws_cdk import aws_sns as sns

    from rhi.observability.prometheus import PrometheusWorkspace
    from rhi.pools.group import PoolGroup
    from rhi.pools.release import PoolRelease, ReleaseFiles

    cdk = app_module.cdk
    context = {"aws:cdk:bundling-stacks": ["**"] if bundled else []}
    app = cdk.App(outdir=str(outdir), context=context, analytics_reporting=False)
    stack = cdk.Stack(app, "Pools")
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=1)
    ec2_service = iam.ServicePrincipal("ec2.amazonaws.com")
    role = iam.Role(stack, "Role", assumed_by=ec2_service)  # ty: ignore[invalid-argument-type] - jsii, as in role.py
    group = PoolGroup(
        stack,
        "Pool",
        pool=pool,
        subnets=vpc.private_subnets,
        security_group=ec2.SecurityGroup(stack, "Instances", vpc=vpc),
        target_group=elbv2.ApplicationTargetGroup(
            stack, "Targets", vpc=vpc, port=8000, target_type=elbv2.TargetType.INSTANCE
        ),
        role=role,
    )
    key = dynamodb.Attribute(name="pool", type=dynamodb.AttributeType.STRING)
    if unrelated:
        sns.Topic(stack, "Unrelated")
    PoolRelease(
        stack,
        "Release",
        pool=pool,
        files=ReleaseFiles(stack, "ReleaseFiles", repo=repo),
        group=group,
        role=role,
        heartbeats=dynamodb.Table(stack, heartbeats, partition_key=key),
        workspace=PrometheusWorkspace(stack, "Prometheus"),
        repo=repo,
    )
    app.synth()
    template = json.loads((outdir / "Pools.template.json").read_text(encoding="utf-8"))
    assets = json.loads((outdir / "Pools.assets.json").read_text(encoding="utf-8"))
    return Release(template, outdir, assets, pool.name)


@pytest.fixture(scope="module")
def alone(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Release:
    """The mock pool's release in a stack of its own, which the syntheses that change one thing are compared with."""
    return standalone(app_module, tmp_path_factory.mktemp("alone"))


# The parameter.


def test_the_agent_reads_the_parameter(app: cdk.App, mock: Release) -> None:
    assert mock.parameter["Name"] == PARAMETER
    assert mock.parameter["Type"] == "String"
    assert not PARAMETER.startswith(config.SSM_PREFIX)
    assert config.SSM_PREFIX == "/rhi/config/"

    current = agent_release.parse_current(sample(mock.parameter["Value"]))
    stack = app.node.find_child("Rhi").pools  # ty: ignore[missing-attribute] - the app's stage, typed as a construct
    group = mock.group
    heartbeats = stack.resolve(stack.get_logical_id(stack.tables.heartbeats.node.default_child))
    assert current.settings == agent_release.Settings(
        pool="mock",
        group=f"sample-{group}",
        launch_hook="rhi-launch",
        termination_hook="rhi-termination",
        log_group="/rhi/pools/mock",
        workspace_url=WORKSPACE + "api/v1/remote_write",
        heartbeat_table=f"sample-{heartbeats}",
        secret_id="/rhi/pools/mock/bearer",
    )
    # The log group and the secret the settings name are the pool's own, by their fixed names.
    assert "/rhi/pools/mock" in [g.get("LogGroupName") for g in of_type(mock.template, "AWS::Logs::LogGroup").values()]
    assert "/rhi/pools/mock/bearer" in [
        s["Name"] for s in of_type(mock.template, "AWS::SecretsManager::Secret").values()
    ]

    # The compared release: the image by the registry's digest, in the repository the asset is
    # pushed to; no model and no flag for the mock; the files by the digest of the archive.
    (destination,) = mock.image_asset[1]["destinations"].values()
    repository = sample({"Fn::Sub": destination["repositoryName"]})
    assert current.release == agent_release.Release(
        engine_image=f"123456789012.dkr.ecr.{REGION}.amazonaws.com/{repository}@{IMAGE_DIGEST}",
        model_revision=None,
        engine_flags=(),
        files_sha256=hashlib.sha256(mock.archive.read_bytes()).hexdigest(),
    )
    # The example gives the mock pool no measured start.
    assert current.measured_start_seconds is None
    assert "measured_start_seconds" not in mock.value


def test_the_files_are_the_archive_published_as_it_is(mock: Release) -> None:
    (file,) = [f for f in mock.assets["files"].values() if f["source"]["path"].endswith(".tar")]
    (destination,) = file["destinations"].values()
    files = mock.value["files"]
    assert file["source"]["packaging"] == "file"
    assert files["bucket"] == sample({"Fn::Sub": destination["bucketName"]})
    assert files["key"] == destination["objectKey"]
    assert files["sha256"] == hashlib.sha256(mock.archive.read_bytes()).hexdigest()


def test_a_gpu_pool_names_its_model_revision_and_its_measured_start(app_module: ModuleType, tmp_path: Path) -> None:
    measured = standalone(app_module, tmp_path, dataclasses.replace(GPU, measured_start_seconds=200))
    current = agent_release.parse_current(sample(measured.parameter["Value"]))
    assert measured.parameter["Name"] == "/rhi/pools/coursework/release"
    assert current.release.model_revision == GPU.revision
    assert current.measured_start_seconds == 200
    assert current.settings.pool == "coursework"


# The archive.


def members(archive: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(archive) as tar:
        return {member.name: member for member in tar.getmembers()}


def test_the_archive_holds_the_units_the_collector_configuration_and_the_agent(mock: Release) -> None:
    found = members(mock.archive)
    files = {name for name, member in found.items() if member.isfile()}
    assert {name for name in files if name.startswith("units/")} == {f"units/{unit}" for unit in UNITS | GPU_UNITS}
    assert {name for name in files if name.startswith("collector/")} == {"collector/config.yaml"}
    # The fields of the DCGM exporter, where its unit mounts them from.
    assert {name for name in files if name.startswith("dcgm/")} == {"dcgm/counters.csv"}
    # The agent: the two packages, as `python -m rhi_lifecycle` finds them in its working directory.
    for package in (REPO / "services/common/rhi_common", REPO / "services/engine/lifecycle/rhi_lifecycle"):
        sources = {p.relative_to(package.parent).as_posix() for p in package.rglob("*.py")}
        assert sources and {f"agent/{source}" for source in sources} <= files
    assert "agent/rhi_lifecycle/__main__.py" in files
    assert {name.split("/")[0] for name in found} == {"units", "collector", "dcgm", "agent"}
    assert not [name for name in found if "__pycache__" in name]
    # A file of the archive is the repository's, byte for byte.
    with tarfile.open(mock.archive) as tar:
        unit = tar.extractfile("units/rhi-lifecycle.service")
        assert unit is not None
        assert unit.read() == (REPO / "services/engine/units/rhi-lifecycle.service").read_bytes()


def test_the_archive_is_reproducible(app_module: ModuleType, mock: Release, tmp_path: Path) -> None:
    from rhi.pools.release import ARCHIVE_TIME, build_archive

    found = members(mock.archive)
    assert list(found) == sorted(found)
    assert {member.mtime for member in found.values()} == {ARCHIVE_TIME}
    assert {(m.uid, m.gid, m.uname, m.gname) for m in found.values()} == {(0, 0, "", "")}
    assert {(member.type, member.mode) for member in found.values()} == {
        (tarfile.DIRTYPE, 0o755),
        (tarfile.REGTYPE, 0o644),
    }
    # The same files, copied elsewhere at another time and with other modes, give the same bytes.
    copy = copy_of_the_sources(tmp_path / "copy")
    for path in copy.rglob("*"):
        os.utime(path, (1, 1))
        path.chmod(0o700)
    digest = build_archive(tmp_path / "again.tar", repo=copy, libraries=False)
    assert (tmp_path / "again.tar").read_bytes() == mock.archive.read_bytes()
    assert digest == mock.value["files"]["sha256"]


def test_a_bundled_stack_carries_the_agents_libraries(
    app_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where CDK bundles the stack, the libraries of the lifecycle group are installed beside the
    agent, as for a function's bundle. The step that installs them is replaced by a stand-in."""
    from rhi import packaging

    asked = []

    def install(command: Sequence[str]) -> None:
        requirements = Path(command[command.index("--requirements") + 1]).read_text(encoding="utf-8")
        target = Path(command[command.index("--target") + 1])
        asked.append((list(command), requirements))
        # What an installer leaves: the library, its metadata, the record of its own run, a script.
        for file in ("lib/__init__.py", "lib-1.0.dist-info/METADATA", "lib-1.0.dist-info/RECORD", "bin/lib", ".lock"):
            (target / file).parent.mkdir(parents=True, exist_ok=True)
            (target / file).write_text(str(target), encoding="utf-8")

    monkeypatch.setattr(packaging, "install", install)
    bundled = standalone(app_module, tmp_path, bundled=True)
    ((command, requirements),) = asked
    assert command[command.index("--python-version") + 1] == packaging.PYTHON == "3.13"
    lifecycle = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["dependency-groups"]["lifecycle"]
    assert lifecycle and all(pin in requirements for pin in lifecycle)

    files = {name for name, member in members(bundled.archive).items() if member.isfile()}
    assert {"agent/lib/__init__.py", "agent/lib-1.0.dist-info/METADATA", "agent/rhi_lifecycle/__main__.py"} <= files
    assert not files & {"agent/lib-1.0.dist-info/RECORD", "agent/bin/lib", "agent/.lock"}
    # Sorted as text, a library's metadata folder before the files of the library itself.
    assert list(members(bundled.archive)) == sorted(members(bundled.archive))
    assert bundled.value["files"]["sha256"] == hashlib.sha256(bundled.archive.read_bytes()).hexdigest()


def without_a_pure_wheel(lock: Mapping[str, Any], group: str) -> list[str]:
    """The libraries of a dependency group, with what they need, that `uv.lock` holds no pure-Python wheel for."""
    packages = {package["name"]: package for package in lock["package"]}
    (project,) = [package for package in lock["package"] if group in package.get("dev-dependencies", {})]
    pending = list(project["dev-dependencies"][group])
    seen: set[str] = set()
    while pending:
        dependency = pending.pop()
        package = packages[dependency["name"]]
        for extra in dependency.get("extra", []):
            pending += package["optional-dependencies"][extra]
        if package["name"] not in seen:
            seen.add(package["name"])
            pending += package.get("dependencies", [])
    return sorted(
        name
        for name in seen
        if not any(
            re.fullmatch(r".*-(py3|py2\.py3)-none-any\.whl", wheel["url"]) for wheel in packages[name].get("wheels", [])
        )
    )


def test_every_library_of_the_lifecycle_group_has_a_pure_python_wheel() -> None:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    assert without_a_pure_wheel(lock, "lifecycle") == []
    # A library that is compiled for a platform, or that has no wheel at all, is found.
    made_up = {
        "package": [
            {"name": "project", "dev-dependencies": {"lifecycle": [{"name": "pure"}, {"name": "compiled"}]}},
            {
                "name": "pure",
                "dependencies": [{"name": "source-only"}],
                "wheels": [{"url": "https://files.example.com/pure-1.0-py3-none-any.whl"}],
            },
            {
                "name": "compiled",
                "wheels": [{"url": "https://files.example.com/compiled-1.0-cp313-cp313-manylinux_2_28_aarch64.whl"}],
            },
            {"name": "source-only", "sdist": {"url": "https://files.example.com/source-only-1.0.tar.gz"}},
        ]
    }
    assert without_a_pure_wheel(made_up, "lifecycle") == ["compiled", "source-only"]


# The engine image.


def test_the_mock_image_is_arm64(app_module: ModuleType, mock: Release, tmp_path: Path) -> None:
    from rhi.pools.release import ReleaseError

    assert mock.image["platform"] == "linux/arm64"
    # A GPU pool, on the type the design gives the coursework pool.
    assert standalone(app_module, tmp_path / "gpu", GPU).image["platform"] == "linux/amd64"
    # The mock engine on that type is built for it all the same: the types decide, not the engine.
    on_gpu_type = dataclasses.replace(MOCK, instance_types=GPU.instance_types, prices=GPU.prices)
    assert standalone(app_module, tmp_path / "mock", on_gpu_type).image["platform"] == "linux/amd64"

    mixed = dataclasses.replace(
        MOCK, name="mixed", instance_types=("t4g.small", "g7e.2xlarge"), prices={"t4g.small": 0.02, "g7e.2xlarge": 3.36}
    )
    with pytest.raises(ReleaseError, match="'mixed'.*mix"):
        standalone(app_module, tmp_path / "mixed", mixed)


@pytest.mark.parametrize(
    ("instance_type", "expected"),
    [("t4g.small", "arm64"), ("m7gd.large", "arm64"), ("g5g.xlarge", "arm64"), ("g7e.2xlarge", "amd64")]
    + [("g6.xlarge", "amd64"), ("p5en.48xlarge", "amd64"), ("m7i.large", "amd64")],
)
def test_the_architecture_is_read_from_the_letters_after_the_generation(
    app_module: ModuleType, instance_type: str, expected: str
) -> None:
    from rhi.pools.release import architecture

    assert architecture(dataclasses.replace(MOCK, instance_types=(instance_type,))) == expected


def files_of(folder: Path) -> dict[str, bytes]:
    """Every file under a folder, by its path under it."""
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def test_each_engine_has_a_dockerfile_and_the_mock_is_built_in_the_context_of_its_packaging(mock: Release) -> None:
    from rhi.pools.release import ENGINES

    # Every engine a pool may name has a Dockerfile, so every configured pool gets its image.
    assert set(ENGINES) == set(pool_config.ENGINES)
    assert all((REPO / engine.dockerfile).is_file() for engine in ENGINES.values())

    context = (mock.outdir / mock.image["directory"]).resolve()
    assert (context / "Dockerfile").read_bytes() == (REPO / "services/engine/mock/Dockerfile").read_bytes()
    assert {path.name for path in context.iterdir()} == {
        "Dockerfile",
        "requirements.txt",
        "rhi_common",
        "rhi_mock_engine",
    }


def granted(template: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """The effect and the actions of every statement of every policy of a template, sorted."""
    statements = [
        statement
        for policy in of_type(template, "AWS::IAM::Policy").values()
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    return sorted((statement["Effect"], sorted(as_list(statement["Action"]))) for statement in statements)


def test_a_vllm_pool_carries_its_engine_flags(app_module: ModuleType, alone: Release, tmp_path: Path) -> None:
    from rhi.network.security import ENGINE_PORT
    from rhi.pools import model_store, vllm_command

    # A vllm pool as a configuration gives it, with the exception that trusts its model's code.
    exception = {"gpu_hours_per_day": 16, "trust_remote_code": True, "trust_remote_code_reason": "its model needs it"}
    entry = config.pool_entry(GPU) | exception
    (pool,) = pool_config.parse_pools({"coursework": entry})
    release = standalone(app_module, tmp_path, pool)

    # The arguments the module builds, with the model bucket of the account and the region the
    # stack is deployed to, in the parameter the agent reads.
    bucket = model_store.bucket_name(PSEUDO["AWS::AccountId"], REGION)
    flags = vllm_command.arguments(pool, bucket=bucket)
    assert flags[0] == f"s3://{bucket}/{model_store.pool_prefix('coursework')}{pool.revision}/"
    assert flags[flags.index("--port") + 1] == str(ENGINE_PORT)
    assert "--trust-remote-code" in flags
    assert release.value["engine_flags"] == flags
    assert agent_release.parse_current(sample(release.parameter["Value"])).release.engine_flags == tuple(flags)

    # The image: built from the vLLM image's folder, whole, and from nothing else.
    context = (release.outdir / release.image["directory"]).resolve()
    assert files_of(context) == files_of(REPO / "services/engine/vllm")
    assert {"Dockerfile", "entrypoint.sh"} <= set(files_of(context))

    # The flags and the folder bring no role and no action: what the roles of this stack may do
    # is what they may do in the mock pool's, action for action.
    assert len(of_type(release.template, "AWS::IAM::Role")) == len(of_type(alone.template, "AWS::IAM::Role"))
    assert granted(release.template) == granted(alone.template)

    # The mock pool's release is as it was: no flag, and its image from its packaging's context.
    assert alone.value["engine_flags"] == []
    mock_context = (alone.outdir / alone.image["directory"]).resolve()
    assert {path.name for path in mock_context.iterdir()} == {
        "Dockerfile",
        "requirements.txt",
        "rhi_common",
        "rhi_mock_engine",
    }


def test_a_mock_pool_s_release_gives_its_engine_no_flag_whatever_start_delay_its_entry_still_gives(
    app_module: ModuleType, mock: Release, alone: Release, tmp_path: Path
) -> None:
    for release in (mock, alone):
        assert release.value["engine_flags"] == []
    # The retired field is read into nothing: the pool's release is the one of an entry without it.
    (pool,) = pool_config.parse_pools({"mock": config.pool_entry(MOCK) | {"start_delay_seconds": 400}})
    delayed = standalone(app_module, tmp_path / "delayed", pool)
    assert delayed.value["engine_flags"] == []
    assert agent_release.parse_current(sample(delayed.parameter["Value"])).release.engine_flags == ()
    assert delayed.parameter["Value"] == alone.parameter["Value"]


# The grants.


def test_the_instance_role_may_fetch_its_release_and_nothing_else(mock: Release) -> None:
    role = mock.tagged("AWS::IAM::Role")
    assert "ec2.amazonaws.com" in json.dumps(mock.template["Resources"][role]["Properties"]["AssumeRolePolicyDocument"])
    statements = [
        statement
        for policy in of_type(mock.template, "AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    assert all(statement["Effect"] == "Allow" for statement in statements)

    def granting(service: str) -> list[dict[str, Any]]:
        return [s for s in statements if any(a.startswith(f"{service}:") for a in as_list(s["Action"]))]

    # That parameter, by the reference whose value is its name, and no other.
    (parameter_id,) = [
        name for name, p in of_type(mock.template, "AWS::SSM::Parameter").items() if p["Name"] == PARAMETER
    ]
    (parameter,) = [s for s in granting("ssm") if "Parameter" in json.dumps(s["Action"])]
    assert parameter["Action"] == "ssm:GetParameter"
    assert references(parameter["Resource"]) == ["AWS::Partition", "AWS::Region", "AWS::AccountId", parameter_id]
    assert sample(parameter["Resource"]) == f"arn:aws:ssm:{REGION}:123456789012:parametersample-{parameter_id}"

    # That object of the bucket, and no other: no pattern, and no action on the bucket.
    (archive,) = granting("s3")
    files = mock.value["files"]
    assert archive["Action"] == "s3:GetObject"
    assert sample(archive["Resource"]) == f"arn:aws:s3:::{files['bucket']}/{files['key']}"
    assert "*" not in sample(archive["Resource"]) and "?" not in sample(archive["Resource"])

    # The pull, on the repository the image is pushed to; the token, as AWS grants it.
    pull, token = sorted(granting("ecr"), key=lambda s: s["Resource"] == "*")
    repository = mock.value["engine_image"].split("@")[0].split("/", 1)[1]
    assert sorted(pull["Action"]) == [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    ]
    assert sample(pull["Resource"]) == f"arn:aws:ecr:{REGION}:123456789012:repository/{repository}"
    assert (token["Action"], token["Resource"]) == ("ecr:GetAuthorizationToken", "*")


# What changes a release, and what a deployment leaves alone.


def changed(tmp_path: Path, file: str) -> Path:
    """A copy of the sources that differs from them in one file."""
    copy = copy_of_the_sources(tmp_path / "sources")
    with (copy / file).open("a", encoding="utf-8") as stream:
        stream.write("\n# A change.\n")
    return copy


@pytest.mark.parametrize("file", ["services/engine/mock/Dockerfile", "services/engine/units/rhi-collector.service"])
def test_a_change_to_what_an_instance_holds_changes_the_release(
    app_module: ModuleType, alone: Release, tmp_path: Path, file: str
) -> None:
    other = standalone(app_module, tmp_path / "out", repo=changed(tmp_path, file))
    # The image the release names is the one whose digest the lookup reads, in each.
    for release in (other, alone):
        parts = release.parameter["Value"]["Fn::Join"][1]
        assert {"Fn::GetAtt": [release.digest_lookup[0], "imageDetails.0.imageDigest"]} in parts
    if file.endswith("Dockerfile"):
        # Another image, pushed under another tag, whose digest the deployment reads for the release.
        assert other.image["directory"] != alone.image["directory"]
        assert other.digest_lookup != alone.digest_lookup
        assert other.value["files"] == alone.value["files"]
    else:
        # Other files, by their digest, in the parameter's value itself.
        assert other.value["files"]["sha256"] != alone.value["files"]["sha256"]
        assert other.parameter["Value"] != alone.parameter["Value"]
        assert other.digest_lookup == alone.digest_lookup


def test_what_is_outside_the_comparison_leaves_the_compared_release_as_it_was(
    app_module: ModuleType, alone: Release, tmp_path: Path
) -> None:
    """The measured start and a setting are in the parameter, and outside what an instance serves."""
    unrelated = standalone(app_module, tmp_path / "unrelated", unrelated=True)
    assert "AWS::SNS::Topic" in {r["Type"] for r in unrelated.template["Resources"].values()}
    assert "AWS::SNS::Topic" not in {r["Type"] for r in alone.template["Resources"].values()}
    assert unrelated.parameter["Value"] == alone.parameter["Value"]

    measured = standalone(app_module, tmp_path / "measured", dataclasses.replace(MOCK, measured_start_seconds=120))
    assert measured.value["measured_start_seconds"] == 120

    setting = standalone(app_module, tmp_path / "setting", heartbeats="OtherHeartbeats")
    assert setting.value["settings"]["heartbeat_table"] != alone.value["settings"]["heartbeat_table"]

    compared = ("engine_image", "model_revision", "engine_flags", "files")
    for other in (measured, setting):
        assert other.parameter["Value"] != alone.parameter["Value"]
        assert {key: other.value[key] for key in compared} == {key: alone.value[key] for key in compared}


def test_the_stack_starts_no_instance_refresh(mock: Release) -> None:
    # A deployment replaces no instance: the pools stack holds no custom resource for a refresh,
    # no state machine that would wait for one, and no role that may start, cancel or describe one.
    kinds = {resource["Type"] for resource in mock.template["Resources"].values()}
    assert REFRESH not in kinds and "AWS::StepFunctions::StateMachine" not in kinds
    documents = [policy["PolicyDocument"] for policy in of_type(mock.template, "AWS::IAM::Policy").values()] + [
        policy["PolicyDocument"]
        for role in of_type(mock.template, "AWS::IAM::Role").values()
        for policy in role.get("Policies", [])
    ]
    actions = [
        action
        for document in documents
        for statement in document["Statement"]
        for action in as_list(statement.get("Action", []))
    ]
    assert actions and not [action for action in actions if "instancerefresh" in action.lower()]


def test_each_pool_keeps_its_release_parameter_and_the_grant_to_read_it(mock: Release) -> None:
    # The mock pool's and the GPU pool's: one parameter each, which that pool's instance role reads.
    assert len(EXAMPLE.pools) > 1
    for pool in EXAMPLE.pools:
        release = dataclasses.replace(mock, pool=pool.name)
        (parameter_id,) = [
            name
            for name, p in of_type(mock.template, "AWS::SSM::Parameter").items()
            if p["Name"] == release.parameter["Name"]
        ]
        role = release.tagged("AWS::IAM::Role")
        reads = [
            statement
            for policy in of_type(mock.template, "AWS::IAM::Policy").values()
            if {"Ref": role} in policy["Roles"]
            for statement in policy["PolicyDocument"]["Statement"]
            if statement["Action"] == "ssm:GetParameter" and parameter_id in references(statement["Resource"])
        ]
        assert len(reads) == 1


# The boot program.

AWS = '''#!/usr/bin/env python3
"""The AWS CLI, for the two calls of the boot program: one region, one parameter and one object."""
import argparse
import json
import os
import shutil
import sys

parser = argparse.ArgumentParser()
parser.add_argument("call", nargs=2)
parser.add_argument("--region", required=True)
parser.add_argument("--name")
parser.add_argument("--output", choices=["json"])
parser.add_argument("--only-show-errors", action="store_true")
parser.add_argument("paths", nargs="*")
arguments = parser.parse_intermixed_args()
with open(os.path.join(os.environ["RHI_TEST_STATE"], "cloud.json"), encoding="utf-8") as stream:
    cloud = json.load(stream)
if arguments.region != cloud["region"]:
    sys.exit(254)
if arguments.call == ["ssm", "get-parameter"] and arguments.output and arguments.name in cloud["parameters"]:
    value = cloud["parameters"][arguments.name]
    print(json.dumps({"Parameter": {"Name": arguments.name, "Type": "String", "Value": value}}))
elif arguments.call == ["s3", "cp"] and len(arguments.paths) == 2 and arguments.paths[0] in cloud["objects"]:
    shutil.copyfile(cloud["objects"][arguments.paths[0]], arguments.paths[1])
else:
    sys.exit(254)
'''
SYSTEMCTL = """#!/bin/bash
# systemctl: it records each call.
printf '%s\\n' "$*" >>"$RHI_TEST_STATE/systemctl.log"
"""


class Metadata(ThreadingHTTPServer):
    """The instance metadata, as IMDSv2 serves it: a session token first, then reads that carry it."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(("127.0.0.1", 0), MetadataRequest)
        self.token = secrets.token_urlsafe(24)
        self.values = values


class MetadataRequest(BaseHTTPRequestHandler):
    def answer(self, status: int, body: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def metadata(self) -> Metadata:
        assert isinstance(self.server, Metadata)
        return self.server

    def do_PUT(self) -> None:
        self.answer(200, self.metadata().token) if self.path == "/latest/api/token" else self.answer(404)

    def do_GET(self) -> None:
        value = self.metadata().values.get(self.path.removeprefix("/latest/meta-data/"))
        if self.headers.get("X-aws-ec2-metadata-token") != self.metadata().token:
            self.answer(401)
        elif value is None:
            self.answer(404)
        else:
            self.answer(200, value)

    def log_message(self, format: str, *args: Any) -> None:
        """Nothing is written to the test's output."""


def boot(release: Release, folder: Path) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """The boot program, run as an instance runs it with the release's parameter and archive, in a
    root under `folder`: its run, the root, and the folder where the stand-ins keep their state."""
    root, state, programs = folder / "root", folder / "state", folder / "bin"
    for made in (root, state, programs):
        made.mkdir(parents=True)
    for name, text in (("aws", AWS), ("systemctl", SYSTEMCTL)):
        (programs / name).write_text(text, encoding="utf-8")
        (programs / name).chmod(0o755)
    parameter = release.parameter["Name"]
    value = sample(release.parameter["Value"])
    files = json.loads(value)["files"]
    cloud = {
        "region": REGION,
        "parameters": {parameter: value},
        "objects": {f"s3://{files['bucket']}/{files['key']}": str(release.archive)},
    }
    (state / "cloud.json").write_text(json.dumps(cloud), encoding="utf-8")

    metadata = Metadata({"tags/instance/rhi-release-parameter": parameter, "placement/region": REGION})
    threading.Thread(target=metadata.serve_forever, daemon=True).start()
    try:
        ran = subprocess.run(
            ["bash", str(BOOT)],
            env={
                "PATH": f"{programs}{os.pathsep}{os.environ['PATH']}",
                "RHI_BOOT_ROOT": str(root),
                "RHI_BOOT_METADATA": f"http://127.0.0.1:{metadata.server_address[1]}",
                "RHI_TEST_STATE": str(state),
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        metadata.shutdown()
        metadata.server_close()
    return ran, root, state


def settings_written(root: Path) -> dict[str, str]:
    """The settings the boot program wrote for the units, by variable."""
    lines = (root / "run/rhi/release.env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


def test_the_boot_program_accepts_the_archive(mock: Release, tmp_path: Path) -> None:
    ran, root, state = boot(mock, tmp_path)
    value = sample(mock.parameter["Value"])

    # The digest the parameter names is the archive's: the program went on to unpack and start it.
    assert (ran.returncode, ran.stderr) == (0, "")
    # The units of every instance: the folder of the test's root lists no NVIDIA device.
    assert {path.name for path in (root / "run/systemd/system").iterdir()} == UNITS
    assert (root / "run/rhi/engine-flags").read_text(encoding="utf-8") == ""
    collector = root / "opt/rhi/release/collector/config.yaml"
    assert collector.read_bytes() == (REPO / "services/engine/collector/config.yaml").read_bytes()
    assert (root / "opt/rhi/release/agent/rhi_lifecycle/__main__.py").is_file()
    assert (root / "opt/rhi/release/agent/rhi_common/__init__.py").is_file()
    assert (state / "systemctl.log").read_text(encoding="utf-8").splitlines() == [
        "daemon-reload",
        "start --no-block rhi-collector.service rhi-lifecycle.service",
    ]
    # The settings the units read are the parameter's.
    settings = settings_written(root)
    assert settings["RHI_ENGINE_IMAGE"] == json.loads(value)["engine_image"]
    assert (settings["RHI_LAUNCH_HOOK"], settings["RHI_TERMINATION_HOOK"]) == ("rhi-launch", "rhi-termination")
    assert settings["RHI_ENGINE"] == "mock"


def test_a_vllm_pools_instance_heartbeats_its_load_while_it_serves(app_module: ModuleType, tmp_path: Path) -> None:
    from prometheus_client import CollectorRegistry

    from rhi_common.engine_metrics import VLLM, Quantity, metric_names
    from rhi_common.records import InstanceHeartbeat
    from rhi_lifecycle import __main__ as agent
    from rhi_lifecycle import serving
    from rhi_lifecycle.host import Host

    # A vllm pool's release, as synthesis writes it, on an instance the boot program starts with it.
    release = standalone(app_module, tmp_path / "synthesis", GPU)
    assert release.value["settings"]["engine"] == "vllm"
    ran, root, _ = boot(release, tmp_path / "instance")
    assert (ran.returncode, ran.stderr) == (0, "")
    # The agent's program, given what the boot program wrote, reads the engine under vLLM's names.
    environment = agent.Environment.read(settings_written(root))
    assert environment.engine == VLLM
    held = agent_release.read_held(root / "var/lib/rhi/release.json")
    assert held is not None

    # In service, a heartbeat reads vLLM's `/metrics` and writes the instance's row, which the
    # scaler and the smoke test `release-heartbeat` read: the stage `serving` and the engine's load.
    # Read under the mock's names, the same answer gives no load and no row is written.
    names = metric_names(VLLM)
    running, waiting = names[Quantity.RUNNING_REQUESTS], names[Quantity.WAITING_REQUESTS]
    exposition = "".join(
        f'# TYPE {name} gauge\n{name}{{engine="0",model_name="coursework"}} {count}\n'
        for name, count in ((running, 2.0), (waiting, 1.0))
    ).encode()
    tables: list[str] = []
    written: list[dict[str, Any]] = []

    def table(name: str) -> types.SimpleNamespace:
        tables.append(name)
        return types.SimpleNamespace(put_item=lambda Item: written.append(Item))

    def fetch(request: Any, timeout: float | None) -> tuple[int, bytes] | None:
        return (200, exposition) if request.full_url.endswith("/metrics") else None

    duties = serving.Serving(
        held,
        environment.settings,
        "the-instance",
        environment.engine,
        dynamodb=types.SimpleNamespace(Table=table),
        secrets=None,
        host=Host(),
        fetch=fetch,
        registry=CollectorRegistry(),
        clock=lambda: 1_800_000_000.0,
    )
    assert duties.beat()
    assert tables == [environment.settings.heartbeat_table]
    (row,) = [InstanceHeartbeat.from_item(item) for item in written]
    assert (row.pool, row.stage, row.running, row.waiting) == ("coursework", serving.STAGE, 2, 1)
