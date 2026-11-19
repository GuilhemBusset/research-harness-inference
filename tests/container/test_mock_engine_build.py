"""The mock engine's image builds, starts and serves (system design, sections 4.1 and 8.3).

The pipeline's asset step builds this image only after a merge, so a Dockerfile that does not
build, or a library missing from the image, would first show as a failed deployment. This test
builds the image of `services/engine/mock/Dockerfile` from the build context the release gives
the pipeline (`rhi.packaging.image_context`), for the runner's own architecture: the base image
is published for arm64 and x86_64.

The mock needs nothing outside itself but the pool's secret, so the image is started as it is
deployed, with a short start delay and a secret made here, and the running engine is asked.

It needs a container runtime, so CI's job `container-tests` runs it and the local gate does not
(tests/container/README.md). Nothing here skips: without a runtime the fixture fails.
"""

from __future__ import annotations

import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "services" / "engine" / "mock" / "Dockerfile"
# The port every engine listens on (system design, section 4.1).
PORT = 8000
# What the mock is started with: how long `/health` answers 503 after its start, in seconds.
START_DELAY = 3
# One of the inference routes, and a request it answers when the secret comes with it.
INFERENCE_ROUTE = "/v1/chat/completions"
REQUEST = {"model": "mock", "messages": [{"role": "user", "content": "Say something."}]}

BUILD_SECONDS = 900
LISTEN_SECONDS = 60
HEALTHY_SECONDS = START_DELAY + 60


@dataclass(frozen=True)
class Engine:
    """A running container of the image: where it answers, its secret, and when its start was asked for."""

    base: str
    # Not in what a failing test prints of its arguments.
    secret: str = field(repr=False)
    asked: float


def docker(*arguments: str, timeout: float = 60) -> str:
    """Run one docker command and return what it printed.

    A failure carries docker's own words and not the command's arguments: one of them is the secret.
    """
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise AssertionError(f"docker {arguments[0]} failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout.strip()


def remove(*arguments: str) -> None:
    """Remove a container or an image this file made, if it is there: docker's answer is not looked at."""
    subprocess.run(["docker", *arguments], capture_output=True, timeout=60, check=False)


def health(base: str) -> int | None:
    """The status `/health` answers with, or nothing while the engine does not listen yet."""
    try:
        return httpx.get(f"{base}/health", timeout=5, trust_env=False).status_code
    except httpx.HTTPError:
        return None


def address(container: str) -> str:
    """Wait until the container's engine listens and return where; a failure carries its log."""
    try:
        port = docker("port", container, f"{PORT}/tcp").splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + LISTEN_SECONDS
        while health(base) is None:
            assert docker("inspect", "--format={{.State.Running}}", container) == "true", "the container stopped"
            assert time.monotonic() < deadline, f"no answer in {LISTEN_SECONDS} seconds"
            time.sleep(0.2)
    except AssertionError as failure:
        log = subprocess.run(["docker", "logs", "--tail=80", container], capture_output=True, text=True, check=False)
        raise AssertionError(f"the mock engine did not start: {failure}\n{log.stdout}\n{log.stderr}") from None
    return base


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    """The tag of the mock engine's image, built here; the image is removed here."""
    with pytest.MonkeyPatch.context() as patch:
        # Imported here: collecting this file loads no CDK. The packaging module imports it, and
        # its jsii refuses a package cache below a directory another user owns, as in the
        # implement loop's sandbox; without the cache it needs none.
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        from rhi import packaging

    tag = f"rhi-mock-engine-build:{uuid4().hex}"
    try:
        # No `--platform`: the runner's own architecture.
        docker("build", "--tag", tag, packaging.image_context("mock-engine", DOCKERFILE), timeout=BUILD_SECONDS)
        yield tag
    finally:
        # Whether the build failed, a test failed or all passed. A build that failed left no tag.
        remove("image", "rm", "--force", tag)


@pytest.fixture(scope="module")
def engine(image: str) -> Iterator[Engine]:
    """A running container of the image, with a start delay and a secret; the container ends here."""
    # Named before it exists, so that it is removed even when `docker run` does not return.
    container = f"rhi-mock-engine-build-{uuid4().hex}"
    # Made at each run, and given to the engine as the deployment gives it: in RHI_POOL_SECRET.
    secret = secrets.token_urlsafe(32)
    settings = [f"--env=RHI_MOCK_START_SECONDS={START_DELAY}", f"--env=RHI_POOL_SECRET={secret}"]
    asked = time.monotonic()
    try:
        # Published on a free port of the loopback interface only.
        docker("run", "--detach", f"--name={container}", f"--publish=127.0.0.1::{PORT}", *settings, image)
        yield Engine(base=address(container), secret=secret, asked=asked)
    finally:
        # Stops and removes the container, whether it never answered, a test failed or all passed.
        remove("rm", "--force", "--volumes", container)


def test_health_answers_200_once_the_start_delay_has_passed(engine: Engine) -> None:
    deadline = time.monotonic() + HEALTHY_SECONDS
    while (status := health(engine.base)) != 200:
        # Until then the engine says that it is starting, and nothing else.
        assert status == 503
        assert time.monotonic() < deadline, f"/health did not answer 200 in {HEALTHY_SECONDS} seconds"
        time.sleep(0.2)
    # The engine started after its container was asked for, so its delay cannot have ended sooner.
    assert time.monotonic() - engine.asked >= START_DELAY


def test_an_inference_route_refuses_a_request_without_the_secret(engine: Engine) -> None:
    url = engine.base + INFERENCE_ROUTE
    without = httpx.post(url, json=REQUEST, timeout=30, trust_env=False)
    assert without.status_code == 401

    # The refusal is about the secret: the same request is answered when it carries it.
    bearer = {"Authorization": f"Bearer {engine.secret}"}
    assert httpx.post(url, json=REQUEST, headers=bearer, timeout=30, trust_env=False).status_code == 200
