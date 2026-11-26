"""The evaluation's trial image is prepared from the committed recipe, holds every pinned tool,
and its solver solves offline (docs/design/agent-session-evaluation.md, "Course quality and the
grader"; tools/evaluate/image/).

The image is prepared through the shared entry point, `evaluate.image.prepare`, under a name made
for this run: no image exists under it, so the first call builds from the committed locks, and the
second finds what the first built. A container of that image is then started with no network, no
mount and no variable of this machine, and asked what it holds: each harness, Node, the SDKs and
the solver print their pinned versions, and the solver solves a small mixed-integer model.

It needs a container runtime, so CI's job `container-tests` runs it and the local gate does not
(tests/container/README.md). Nothing here skips: without a runtime the fixture fails.
"""

from __future__ import annotations

import errno
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

import harnesses

# The evaluation tool is a package under `tools`, which only its own tests put on the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from evaluate import image

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

START_SECONDS = 120

# What a connection to an address outside the container fails with (a documentation address, RFC 5737).
NO_ROUTE = """\
import socket
try:
    socket.create_connection(("192.0.2.1", 443), timeout=5).close()
    print("connected")
except OSError as error:
    print(error.errno)
"""
# A variable that would carry a credential. The base image's own `GPG_KEY` is a public fingerprint.
CREDENTIAL = re.compile(r"API_KEY|TOKEN|SECRET|PASSWORD")


def docker(*arguments: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run one docker command to its end, whatever its result."""
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def name() -> Iterator[str]:
    """An image name of this run alone; whatever is built under it is removed here."""
    made = f"rhi-evaluate-trial-test-{uuid4().hex}"
    try:
        yield made
    finally:
        docker("image", "rm", "--force", image.reference(made))


@pytest.fixture(scope="module")
def prepared(name: str, tmp_path_factory: pytest.TempPathFactory) -> image.Prepared:
    """The image, prepared here from the committed recipe: nothing was built under its name."""
    assert docker("image", "ls", "--quiet", name).stdout.strip() == ""
    return image.prepare(name, record=tmp_path_factory.mktemp("first") / "inventory.json")


@pytest.fixture(scope="module")
def container(prepared: image.Prepared) -> Iterator[str]:
    """A started container of the image: no network, no mount, nothing of this machine's environment."""
    # Named before it exists, so that it is removed even when `docker run` does not return.
    started = f"rhi-evaluate-trial-test-{uuid4().hex}"
    try:
        ran = docker(
            *("run", "--detach", f"--name={started}", "--network=none", prepared.image_id, "sleep", "900"),
            timeout=START_SECONDS,
        )
        assert ran.returncode == 0, f"the trial image did not start:\n{ran.stdout}\n{ran.stderr}"
        yield started
    finally:
        docker("rm", "--force", "--volumes", started)


def test_the_image_is_built_from_the_committed_locks_then_reused(
    name: str, prepared: image.Prepared, tmp_path: Path
) -> None:
    assert prepared.built
    reported = docker("image", "inspect", "--format", "{{.Id}}", prepared.reference).stdout.strip()
    assert prepared.inventory["image"] == {"reference": image.reference(name), "id": reported}

    again = image.prepare(name, record=tmp_path / "inventory.json")

    assert not again.built
    assert again.image_id == prepared.image_id
    local = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    assert local["image"]["id"] == reported
    assert image.faults(local) == []


def test_a_started_container_holds_the_pinned_versions_and_solves_offline(container: str) -> None:
    def run(command: Sequence[str]) -> str:
        ran = docker("exec", container, *command, timeout=START_SECONDS)
        assert ran.returncode == 0, f"{command[0]} failed in the trial image:\n{ran.stdout}\n{ran.stderr}"
        return ran.stdout

    pins = image.group_pins()
    assert image.installed(run) == {
        "node": harnesses.NODE_VERSION,
        "harnesses": {harness.name: harness.version for harness in harnesses.HARNESSES},
        "sdks": {sdk: pins[distribution] for sdk, distribution in harnesses.SDKS.items()},
        "solver": {image.SOLVER: pins[image.SOLVER]},
    }

    # No cloud access: no route out of the container, and no credential in its environment.
    assert run(["python", "-c", NO_ROUTE]).split() == [str(errno.ENETUNREACH)]
    variables = [line.partition("=")[0] for line in run(["env"]).splitlines()]
    assert not [name for name in variables if name.startswith("AWS_") or CREDENTIAL.search(name)]

    solved = image.solve(run)
    assert solved["status"] == "Optimal"
    assert solved["objective"] == pytest.approx(20)
    assert solved["plan"] == pytest.approx([4, 0])
