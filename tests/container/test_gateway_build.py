"""The gateway's image builds, and holds every library its entry point imports (system design, 8.3).

The pipeline's asset step builds this image only after a merge, so a Dockerfile that does not
build, or a library missing from the image, would first show as a failed deployment. This test
builds the image of `services/gateway/Dockerfile` from the build context the gateway's construct
gives the pipeline (`rhi.packaging.image_context`), for the runner's own architecture.

The gateway needs AWS and its settings to serve, so the image is not started as a server: it is
run once to import `rhi_gateway.main`, the module its command runs, which imports every library
the gateway uses, and it stops there.

It needs a container runtime, so CI's job `container-tests` runs it and the local gate does not
(tests/container/README.md). Nothing here skips: without a runtime the fixture fails.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "services" / "gateway" / "Dockerfile"
# The module the image's command runs: `python -m rhi_gateway.main`.
ENTRY_POINT = "rhi_gateway.main"

BUILD_SECONDS = 900
IMPORT_SECONDS = 120


def docker(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run one docker command to its end, whatever its result."""
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    """The tag of the gateway's image, built here; the image is removed here."""
    with pytest.MonkeyPatch.context() as patch:
        # Imported here: collecting this file loads no CDK. The packaging module imports it, and
        # its jsii refuses a package cache below a directory another user owns, as in the
        # implement loop's sandbox; without the cache it needs none.
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        from rhi import packaging

    tag = f"rhi-gateway-build:{uuid4().hex}"
    try:
        # No `--platform`: the runner's own architecture.
        built = docker("build", "--tag", tag, packaging.image_context("gateway", DOCKERFILE), timeout=BUILD_SECONDS)
        assert built.returncode == 0, f"docker build failed:\n{built.stdout}\n{built.stderr}"
        yield tag
    finally:
        # Whether the build failed, the test failed or it passed. A build that failed left no tag.
        docker("image", "rm", "--force", tag)


def test_the_image_imports_the_gateways_entry_point(image: str) -> None:
    # Named before it exists, so that it is removed even when `docker run` does not return.
    container = f"rhi-gateway-build-{uuid4().hex}"
    try:
        # The import in place of the image's command: no settings, no AWS, nothing served.
        ran = docker(
            "run", f"--name={container}", image, "python", "-c", f"import {ENTRY_POINT}", timeout=IMPORT_SECONDS
        )
    finally:
        docker("rm", "--force", "--volumes", container)
    assert ran.returncode == 0, f"importing {ENTRY_POINT} in the image failed:\n{ran.stdout}\n{ran.stderr}"
