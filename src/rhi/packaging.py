"""How a service's code and its libraries reach AWS (system design, sections 8.1 and 8.2).

A service imports `rhi_common`, its own package and the libraries of its dependency group in
`pyproject.toml`, the group named after it. Two things carry them, and both take the libraries from
`uv.lock`, so the versions the tests ran are the versions that run:

- `function_code` gives a Lambda function a bundle: the two packages and the group's libraries,
  installed by uv as wheels for the Lambda runtime's Python and for Linux on the function's
  architecture. CDK's local bundling builds it, so no container is ever started, and CDK builds
  nothing for a stack that the context `aws:cdk:bundling-stacks` excludes.
- `image_context` gives a container image its build context: the two packages, the service's
  Dockerfile and the group's requirements file with hashes, which the Dockerfile installs.

`bundle` is the layout of a function's bundle, in any directory: the release's files carry the
lifecycle agent in it (`rhi.pools.release`).

None holds anything else of the repository. A function that needs a list kept elsewhere in it
gets it at synthesis, as an environment variable its construct sets.
"""

from __future__ import annotations

import atexit
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import aws_cdk as cdk
import jsii
from aws_cdk import aws_lambda as lambda_

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO = Path(__file__).resolve().parents[2]

# The Lambda runtime of every function, and its Python: the version `.python-version` names.
RUNTIME = lambda_.Runtime.PYTHON_3_13
PYTHON = "3.13"

# A function's architecture, as Lambda names it, and the platform uv installs wheels for. Lambda
# runs Amazon Linux 2023, whose glibc is newer than the one manylinux 2.28 wheels ask for.
PLATFORMS = {
    "arm64": "aarch64-manylinux_2_28",
    "x86_64": "x86_64-manylinux_2_28",
}

# Each service that is deployed as a function or as an image, under the name of its dependency
# group, and where its package lives.
COMMON = "services/common/rhi_common"
SERVICES = {
    "gateway": "services/gateway/rhi_gateway",
    "mock-engine": "services/engine/mock/rhi_mock_engine",
    "scaler": "services/scaler/rhi_scaler",
    "reporter": "services/reporter/rhi_reporter",
    "issuer": "services/issuer/rhi_issuer",
    "lifecycle": "services/engine/lifecycle/rhi_lifecycle",
}
# Python's bytecode cache, which a run of the tests leaves in a package: never part of what is deployed.
_CACHE = "__pycache__"


def requirements(service: str, *, repo: Path = REPO) -> str:
    """The libraries of the service's group, each at its version in `uv.lock` with its hashes, as a requirements file.

    It holds what the group needs and nothing about the rest of the lock, so a change to another
    group leaves it as it was. uv reads the lock and nothing else: no network.
    """
    _package(service, repo)
    exported = subprocess.run(
        ["uv", "export", "--quiet", "--project", str(repo), "--locked", "--only-group", service]
        + ["--format", "requirements-txt", "--no-header", "--no-annotate"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return exported.stdout


def install_command(requirements_file: Path, target: Path, platform: str) -> list[str]:
    """The uv command that installs a requirements file into a bundle, for the Lambda runtime on one platform.

    Wheels only: nothing is compiled, so the architecture of the machine that synthesizes does not
    matter. The file lists every library with its hashes, so uv resolves nothing and checks each
    wheel it downloads.
    """
    return [
        *("uv", "pip", "install"),
        *("--requirements", str(requirements_file)),
        *("--target", str(target)),
        *("--python-version", PYTHON),
        *("--python-platform", platform),
        *("--only-binary", ":all:"),
        "--no-deps",
        "--require-hashes",
    ]


def install(command: Sequence[str]) -> None:
    """Run an `install_command`. The one step that downloads; the tests replace it with a stand-in."""
    subprocess.run(command, check=True)


def function_code(service: str, architecture: lambda_.Architecture, *, repo: Path = REPO) -> lambda_.Code:
    """The code of a Lambda function: `rhi_common`, the service's package and the libraries of the service's group.

    Give the function the same architecture, and `RUNTIME`.
    """
    package = _package(service, repo)
    name = architecture.name
    if name not in PLATFORMS:
        message = f"no Lambda platform for the architecture {name!r}: expected one of {sorted(PLATFORMS)}"
        raise ValueError(message)
    platform = PLATFORMS[name]
    locked = requirements(service, repo=repo)

    # The asset's hash covers what the bundle is made from: the two packages, the locked libraries
    # and how they are installed. The bundle itself would not do: uv records in it the hash of each
    # script it writes, and a script starts with the path of the Python that uv ran under, so every
    # checkout the pipeline builds in would change every function.
    made_from = hashlib.sha256()
    made_from.update("\0".join([*install_command(Path("requirements.txt"), Path(), platform), locked]).encode())
    for folder in (repo / COMMON, package):
        for file in sorted(folder.rglob("*")):
            if file.is_file() and _CACHE not in file.parts:
                made_from.update(b"\0" + file.relative_to(folder.parent).as_posix().encode() + b"\0")
                made_from.update(file.read_bytes())

    return lambda_.Code.from_asset(
        str(package),
        asset_hash=made_from.hexdigest(),
        bundling=cdk.BundlingOptions(
            # CDK asks for an image and starts it only when the local bundling declines, which
            # `_LocalBundle` never does: it builds the bundle or raises.
            image=RUNTIME.bundling_image,
            local=_LocalBundle(service, platform, locked, repo),
        ),
    )


def image_context(service: str, dockerfile: Path, *, repo: Path = REPO) -> str:
    """A build context for the service's image, for the `directory` of a `DockerImageAsset`.

    At its root: `rhi_common/`, the service's package, `Dockerfile` (a copy of `dockerfile`) and
    `requirements.txt`, the service's group with hashes. The image's asset hash covers these four
    and nothing else, so a change elsewhere in the repository starts no new image. The directory is
    a temporary one, removed when the process ends: CDK copies it into the cloud assembly when the
    asset is created.
    """
    _package(service, repo)
    context = Path(tempfile.mkdtemp(prefix=f"rhi-{service}-image-"))
    atexit.register(shutil.rmtree, context, ignore_errors=True)
    copy_packages(service, repo, context)
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / "requirements.txt").write_text(requirements(service, repo=repo), encoding="utf-8")
    return str(context)


def bundle(service: str, platform: str, into: Path, *, repo: Path = REPO, locked: str | None = None) -> None:
    """Fill `into` with a function's bundle: `rhi_common`, the service's package and the libraries
    of the service's group, installed for the Lambda runtime's Python on `platform`.

    `locked` is the group's requirements file when the caller already holds it.
    """
    copy_packages(service, repo, into)
    with tempfile.TemporaryDirectory(prefix="rhi-bundle-") as scratch:
        requirements_file = Path(scratch) / "requirements.txt"
        requirements_file.write_text(requirements(service, repo=repo) if locked is None else locked, encoding="utf-8")
        install(install_command(requirements_file, into, platform))
    # What uv leaves beside the libraries: its lock on the directory, and the libraries'
    # command-line scripts, which start with the path of the Python of this machine.
    (into / ".lock").unlink(missing_ok=True)
    shutil.rmtree(into / "bin", ignore_errors=True)


def _package(service: str, repo: Path) -> Path:
    """The folder of the service's package, for a service this module knows."""
    if service not in SERVICES:
        message = f"no service named {service!r}: expected one of {sorted(SERVICES)}"
        raise ValueError(message)
    return repo / SERVICES[service]


def copy_packages(service: str, repo: Path, into: Path) -> None:
    """Copy `rhi_common` and the service's package to the root of a bundle or of a build context."""
    for package in (repo / COMMON, _package(service, repo)):
        shutil.copytree(package, into / package.name, ignore=shutil.ignore_patterns(_CACHE))


@jsii.implements(cdk.ILocalBundling)
class _LocalBundle:
    """Builds a function's bundle on the machine that synthesizes, in the directory CDK gives."""

    def __init__(self, service: str, platform: str, locked: str, repo: Path) -> None:
        self._service = service
        self._platform = platform
        self._locked = locked
        self._repo = repo

    def try_bundle(self, output_dir: str, *_options: object, **_named_options: object) -> bool:
        """Fill `output_dir`, and say that it is done. A failure raises: there is no container to fall back to.

        The bundling options are not used. jsii passes them as one value, where the interface
        declares keywords, so both forms are accepted.
        """
        bundle(self._service, self._platform, Path(output_dir), repo=self._repo, locked=self._locked)
        return True
