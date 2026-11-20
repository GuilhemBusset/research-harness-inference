"""Service dependencies and packaging (system design, sections 8.1 and 8.2; `src/rhi/packaging.py`).

The libraries a service imports are pinned in the dependency group named after it. A function's
bundle and an image's build context hold `rhi_common`, the service's package and that group at its
versions in `uv.lock`, and nothing else of the repository.

CDK runs in a second interpreter (`synthesize`), whose `PATH` holds `node` and `uv` and nothing
else: no `docker`, so a bundle that CDK tried to build in a container would fail the test. There
the step that installs wheels is replaced by a stand-in that records its arguments, so no test
downloads anything.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"
LOCK = REPO / "uv.lock"

# Each service's group and the libraries it holds (issue E1-16, the table of its Context).
GROUPS = {
    "gateway": {"starlette", "httpx", "uvicorn", "prometheus-client", "boto3"},
    "mock-engine": {"starlette", "uvicorn", "prometheus-client"},
    "scaler": {"boto3", "protobuf", "cramjam"},
    "reporter": {"boto3", "pyjwt", "cryptography"},
    "issuer": {"boto3"},
    "lifecycle": {"boto3", "prometheus-client"},
    "admin": {"boto3"},
    "cli": {"keyring"},
    # Not a service: what the spikes under spikes/ import, the two SDKs their scripts call the endpoint
    # with, the solver of the task spike S3 gives each harness, and the load generator's HTTP client.
    "spikes": {"openai", "anthropic", "boto3", "highspy", "httpx"},
}
# The name each library is imported by, where it is not the name it is installed by.
IMPORT_NAMES = {"prometheus-client": "prometheus_client", "protobuf": "google.protobuf", "pyjwt": "jwt"}

# One exact version of one library, with or without extras: no range, no wildcard, no marker.
PIN = re.compile(r"(?P<name>[A-Za-z0-9._-]+)(\[[A-Za-z0-9._,-]+\])?==(?P<version>\d[A-Za-z0-9.!+]*)")

# The two functions the bundling test deploys: the service, its architecture as CDK names it and as
# Lambda does, and the platform uv must install its wheels for.
FUNCTIONS = [
    ("scaler", "ARM_64", "arm64", "aarch64-manylinux_2_28"),
    ("reporter", "X86_64", "x86_64", "x86_64-manylinux_2_28"),
]
PACKAGES = {
    "common": "services/common/rhi_common",
    "gateway": "services/gateway/rhi_gateway",
    "scaler": "services/scaler/rhi_scaler",
    "reporter": "services/reporter/rhi_reporter",
}


# The one file the stand-in for the install step leaves where it would install the wheels.
STAND_IN = "wheels-would-be-here"


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def groups(pyproject: Path) -> dict[str, list[Any]]:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["dependency-groups"]


def unpinned(pyproject: Path) -> list[str]:
    """The requirements of the service groups and of the dev group that are not one exact version."""
    declared = groups(pyproject)
    return [
        f"{group}: {requirement}"
        for group in (*GROUPS, "dev")
        for requirement in declared[group]
        if isinstance(requirement, str) and not PIN.fullmatch(requirement)
    ]


def lock_packages() -> dict[str, dict[str, Any]]:
    return {package["name"]: package for package in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]}


def locked(group: str) -> dict[str, tuple[str, set[str]]]:
    """Every library `uv.lock` holds for a group, with what its libraries need: name -> (version, hashes)."""
    packages = lock_packages()
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"]
    pending = list(packages[project]["dev-dependencies"][group])
    found: dict[str, tuple[str, set[str]]] = {}
    while pending:
        dependency = pending.pop()
        package = packages[dependency["name"]]
        for extra in dependency.get("extra", []):
            pending += package["optional-dependencies"][extra]
        if package["name"] not in found:
            files = [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]
            found[package["name"]] = (package["version"], {file["hash"] for file in files})
            pending += package.get("dependencies", [])
    return found


def parse_requirements(text: str) -> dict[str, tuple[str, set[str]]]:
    """A requirements file with hashes, read the same way: name -> (version, hashes)."""
    found = {}
    for line in text.replace("\\\n", " ").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement, *hashes = line.split("--hash=")
        pin = PIN.fullmatch(requirement.split(";")[0].strip())
        assert pin, f"not one exact version: {requirement!r}"
        found[normalized(pin["name"])] = (pin["version"], {digest.strip() for digest in hashes})
    return found


def source_files(*packages: str) -> set[str]:
    """The files of some of the repository's packages, as a bundle or a build context holds them at its root."""
    return {
        file.relative_to((REPO / package).parent).as_posix()
        for package in packages
        for file in (REPO / package).rglob("*")
        if file.is_file() and "__pycache__" not in file.parts
    }


def files_under(folder: Path) -> set[str]:
    return {file.relative_to(folder).as_posix() for file in folder.rglob("*") if file.is_file()}


def option(command: Sequence[str], name: str) -> str:
    return command[command.index(name) + 1]


def synthesize(
    tmp_path: Path,
    *,
    context: dict[str, Any] | None = None,
    functions: Sequence[tuple[str, str]] = (),
    image_repos: Sequence[Path] = (),
    dockerfile: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Synthesize a stack that uses `rhi.packaging`, in an interpreter that cannot find docker.

    Returns the cloud assembly's directory and what `in_the_second_interpreter` reported.
    """
    tools = tmp_path / "bin"
    tools.mkdir()
    for tool in ("node", "uv"):
        found = shutil.which(tool)
        assert found, f"{tool} is not on the PATH"
        (tools / tool).symlink_to(found)
    environment = os.environ | {
        "PATH": str(tools),
        "PYTHONPATH": str(REPO / "src"),
        # jsii refuses a package cache below a directory that another user owns, which is what the
        # implement loop's sandbox gives it. Without the cache it unpacks the CDK where it runs.
        "JSII_RUNTIME_PACKAGE_CACHE": "disabled",
    }

    outdir = tmp_path / "cdk.out"
    request = {
        "outdir": str(outdir),
        "context": context or {},
        "functions": functions,
        "image_repos": [str(repo) for repo in image_repos],
        "dockerfile": str(dockerfile),
    }
    subprocess.run([sys.executable, __file__, json.dumps(request)], env=environment, check=True)
    return outdir, json.loads((outdir / "report.json").read_text(encoding="utf-8"))


def in_the_second_interpreter(request: dict[str, Any]) -> None:
    """What `synthesize` runs: the stack, with a stand-in for the step that installs wheels."""
    # Imported only here: the tests' own interpreter never starts jsii.
    import aws_cdk as cdk
    from aws_cdk import aws_ecr_assets, aws_lambda

    from rhi import packaging

    installs = []

    def stand_in(command: Sequence[str]) -> None:
        requirements_file = Path(option(command, "--requirements"))
        installs.append({"command": list(command), "requirements": requirements_file.read_text(encoding="utf-8")})
        # Where the wheels would go, so that the bundle shows which directory the step was given.
        Path(option(command, "--target"), STAND_IN).touch()

    packaging.install = stand_in  # ty: ignore[invalid-assignment] - a test double for the install step

    app = cdk.App(outdir=request["outdir"], context=request["context"])
    stack = cdk.Stack(app, "Services")
    for service, architecture in request["functions"]:
        aws_lambda.Function(
            stack,
            service,
            runtime=packaging.RUNTIME,
            architecture=getattr(aws_lambda.Architecture, architecture),
            handler="index.handler",
            code=packaging.function_code(service, getattr(aws_lambda.Architecture, architecture)),
        )
    image_hashes = [
        aws_ecr_assets.DockerImageAsset(
            stack,
            f"Image{number}",
            directory=packaging.image_context("gateway", Path(request["dockerfile"]), repo=Path(repo)),
        ).asset_hash
        for number, repo in enumerate(request["image_repos"])
    ]
    app.synth()

    report = {"installs": installs, "image_hashes": image_hashes, "docker": shutil.which("docker")}
    Path(request["outdir"], "report.json").write_text(json.dumps(report), encoding="utf-8")


def lambda_functions(outdir: Path) -> list[dict[str, Any]]:
    template = json.loads((outdir / "Services.template.json").read_text(encoding="utf-8"))
    return [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    ]


def test_a_function_on_each_architecture_gets_its_bundle(tmp_path: Path) -> None:
    outdir, report = synthesize(tmp_path, functions=[(service, cdk_name) for service, cdk_name, _, _ in FUNCTIONS])

    assert report["docker"] is None
    python = (REPO / ".python-version").read_text(encoding="utf-8").strip()
    functions = {function["Architectures"][0]: function for function in lambda_functions(outdir)}
    installs = {option(install["command"], "--python-platform"): install for install in report["installs"]}
    assert len(functions) == len(installs) == len(report["installs"]) == len(FUNCTIONS)

    for service, _, architecture, platform in FUNCTIONS:
        function = functions[architecture]
        assert function["Runtime"] == f"python{python}"
        # The stand-in installed nothing, so the bundle holds what is copied from the repository.
        bundle = outdir / ("asset." + function["Code"]["S3Key"].removesuffix(".zip"))
        assert files_under(bundle) == source_files(PACKAGES["common"], PACKAGES[service]) | {STAND_IN}

        command = installs[platform]["command"]
        assert command[:3] == ["uv", "pip", "install"]
        assert option(command, "--python-version") == python
        assert option(command, "--only-binary") == ":all:"  # wheels only
        wanted = {name: version for name, (version, _) in locked(service).items()}
        asked = {name: version for name, (version, _) in parse_requirements(installs[platform]["requirements"]).items()}
        assert asked == wanted
        assert {normalized(name) for name in GROUPS[service]} <= set(asked)


def test_the_image_context_holds_the_service_only(tmp_path: Path) -> None:
    dockerfile = tmp_path / "gateway.Dockerfile"
    dockerfile.write_text("FROM scratch\nCOPY . /srv\n", encoding="utf-8")
    # Three copies of what the context is made from, and of one more package: one as the
    # repository has it, one changed outside the gateway's context, one changed inside it.
    repos = [tmp_path / name for name in ("as_it_is", "changed_elsewhere", "changed_inside")]
    for repo in repos:
        for file in ("pyproject.toml", "uv.lock"):
            repo.mkdir(exist_ok=True)
            shutil.copyfile(REPO / file, repo / file)
        for package in PACKAGES.values():
            shutil.copytree(REPO / package, repo / package, ignore=shutil.ignore_patterns("__pycache__"))
    (repos[1] / PACKAGES["scaler"] / "__init__.py").write_text("CHANGED = True\n", encoding="utf-8")
    (repos[1] / "README.md").write_text("changed\n", encoding="utf-8")
    (repos[2] / PACKAGES["gateway"] / "__init__.py").write_text("CHANGED = True\n", encoding="utf-8")

    outdir, report = synthesize(tmp_path, image_repos=repos, dockerfile=dockerfile)

    assert report["docker"] is None
    as_it_is, changed_elsewhere, changed_inside = report["image_hashes"]
    images = json.loads((outdir / "Services.assets.json").read_text(encoding="utf-8"))["dockerImages"]
    context = outdir / images[as_it_is]["source"]["directory"]
    assert sorted(entry.name for entry in context.iterdir()) == [
        "Dockerfile",
        "requirements.txt",
        "rhi_common",
        "rhi_gateway",
    ]
    assert files_under(context) - {"Dockerfile", "requirements.txt"} == source_files(
        PACKAGES["common"], PACKAGES["gateway"]
    )
    assert (context / "Dockerfile").read_text(encoding="utf-8") == dockerfile.read_text(encoding="utf-8")
    requirements = parse_requirements((context / "requirements.txt").read_text(encoding="utf-8"))
    assert requirements == locked("gateway")
    assert all(hashes for _, hashes in requirements.values())

    assert changed_elsewhere == as_it_is
    assert changed_inside != as_it_is  # the hash does cover the context


def test_skipped_bundling_installs_nothing(tmp_path: Path) -> None:
    outdir, report = synthesize(
        tmp_path,
        context={"aws:cdk:bundling-stacks": []},
        functions=[(service, cdk_name) for service, cdk_name, _, _ in FUNCTIONS],
    )

    assert len(lambda_functions(outdir)) == len(FUNCTIONS)
    assert report["installs"] == []


def test_every_group_is_pinned(tmp_path: Path) -> None:
    assert unpinned(PYPROJECT) == []

    # Each group holds the libraries its service imports, at the version the lock holds.
    declared = groups(PYPROJECT)
    versions = {name: package["version"] for name, package in lock_packages().items()}
    for group, libraries in GROUPS.items():
        pins = [PIN.fullmatch(requirement) for requirement in declared[group]]
        assert {normalized(pin["name"]) for pin in pins if pin} == libraries, group
        assert all(versions[normalized(pin["name"])] == pin["version"] for pin in pins if pin), group
    # The dev group gives the tests and ty every one of them, and moto with its DynamoDB.
    assert {entry["include-group"] for entry in declared["dev"] if isinstance(entry, dict)} == set(GROUPS)
    assert any(
        requirement.startswith("moto[dynamodb]==") for requirement in declared["dev"] if isinstance(requirement, str)
    )

    # A requirement that is not one exact version is reported, in a service group and in the dev group.
    text = PYPROJECT.read_text(encoding="utf-8")
    copy = tmp_path / "pyproject.toml"
    for group, library in (("scaler", "cramjam"), ("cli", "keyring"), ("dev", "pytest"), ("dev", "moto[dynamodb]")):
        pinned = re.search(rf'"{re.escape(library)}==([^"]+)"', text)
        assert pinned, library
        for loosened in (f"{library}>={pinned[1]}", f"{library}~={pinned[1]}", f"{library}=={pinned[1]}.*", library):
            copy.write_text(text.replace(pinned[0], f'"{loosened}"'), encoding="utf-8")
            assert unpinned(copy) == [f"{group}: {loosened}"]

    for library in sorted(set().union(*GROUPS.values())):
        importlib.import_module(IMPORT_NAMES.get(library, library))


if __name__ == "__main__":
    in_the_second_interpreter(json.loads(sys.argv[1]))
