"""The trial image of the evaluation (tools/evaluate/image/): its recipe and its transitive locks
hold the repository's pins, an inventory that is not the recipe's or not the pins' is refused, and
`prepare` builds, reuses and verifies as it says.

Nothing is built here and nothing is fetched: the recipe is read as files, uv reads its lock, the
container runtime is a stand-in, and the solver is the one of the project's environment.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

import harnesses
from evaluate import image

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

INVENTORY = json.loads((image.HERE / image.INVENTORY).read_text(encoding="utf-8"))
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def read_json(name: str) -> dict[str, Any]:
    return json.loads((image.HERE / name).read_text(encoding="utf-8"))


# The recipe and its locks.


def test_the_manifest_names_exactly_the_pinned_harnesses() -> None:
    pins = {harness.package: harness.version for harness in harnesses.HARNESSES}
    assert read_json(image.PACKAGE)["dependencies"] == pins
    # The scripts npm may run at install are named at the same versions, or refused.
    allowed = [entry for entry, allow in read_json(image.PACKAGE)["allowScripts"].items() if allow]
    assert allowed
    assert set(allowed) <= {harness.spec for harness in harnesses.HARNESSES}


def test_the_npm_lock_holds_each_harness_at_its_pin_and_every_package_by_its_hash() -> None:
    packages = read_json(image.PACKAGE_LOCK)["packages"]
    assert packages[""]["dependencies"] == read_json(image.PACKAGE)["dependencies"]
    for harness in harnesses.HARNESSES:
        assert packages[f"node_modules/{harness.package}"]["version"] == harness.version
    transitive = {path: package for path, package in packages.items() if path}
    assert len(transitive) > len(harnesses.HARNESSES)
    for path, package in transitive.items():
        assert harnesses.is_exact(package["version"]), path
        assert package["resolved"].startswith("https://registry.npmjs.org/"), path
        assert package["integrity"].startswith("sha512-"), path


def test_every_base_image_is_pinned_by_digest_and_node_is_the_pinned_one() -> None:
    dockerfile = (image.HERE / image.DOCKERFILE).read_text(encoding="utf-8")
    bases = [line.split()[1] for line in dockerfile.splitlines() if line.upper().startswith("FROM ")]
    assert len(bases) == 2
    for base in bases:
        assert DIGEST.fullmatch(base.partition("@")[2]), base
    assert [base for base in bases if base.startswith(f"node:{harnesses.NODE_VERSION}-")]
    # The locked installs, and no other: nothing resolves a version while the image builds.
    steps = [line for line in dockerfile.splitlines() if line.startswith("RUN ")]
    assert [step for step in steps if "npm ci" in step]
    assert [step for step in steps if "pip install" in step and "--require-hashes" in step and "--no-deps" in step]
    assert not [step for step in steps if "npm install" in step or "apt-get" in step]


def test_the_python_lock_is_the_root_locks_sdks_and_solver() -> None:
    committed = (image.HERE / image.REQUIREMENTS).read_text(encoding="utf-8")
    exported = subprocess.run(image.export_command(), check=True, capture_output=True, text=True).stdout
    assert committed == exported
    group, locked = image.group_pins(), image.requirements(committed)
    for distribution in (*harnesses.SDKS.values(), image.SOLVER):
        assert locked[distribution] == group[distribution]
    # Each library by its hashes, and none of what the group names for the spikes' own scripts.
    listed = re.split(r"\n(?=\S)", committed.strip())
    assert len(listed) == len(locked)
    for requirement in listed:
        assert "--hash=sha256:" in requirement, requirement
    assert "boto3" not in locked


# The inventory.


def test_the_committed_inventory_is_the_recipes_and_the_repositorys_pins() -> None:
    assert image.faults(INVENTORY) == []
    assert INVENTORY["harnesses"] == {harness.name: harness.version for harness in harnesses.HARNESSES}
    assert INVENTORY["node"] == harnesses.NODE_VERSION
    assert set(INVENTORY["sdks"]) == set(harnesses.SDKS)
    assert set(INVENTORY["locks"]) == set(image.LOCKS)
    assert DIGEST.fullmatch(INVENTORY["recipe"])


def changed(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(inventory: dict[str, Any]) -> None:
        *tables, last = path.split("/")
        table: Any = inventory
        for name in tables:
            table = table[name]
        key = int(last) if isinstance(table, list) else last
        if value is None:
            del table[key]
        else:
            table[key] = value

    return change


@pytest.mark.parametrize(
    ("change", "fault"),
    [
        (changed("harnesses/codex", "0.168.0"), "harnesses.codex"),
        (changed("harnesses/pi", None), "harnesses.pi"),
        (changed("harnesses/aider", "1.0.0"), "harnesses.aider"),
        (changed("sdks/openai_sdk", "3.56.0"), "sdks.openai_sdk"),
        (changed("solver/highspy", "1.15.0"), "solver.highspy"),
        (changed("node", "22.19.0"), "node"),
        (changed("locks/package-lock.json", "sha256:" + "0" * 64), "locks.package-lock.json"),
        (changed("locks/requirements.txt", None), "locks.requirements.txt"),
        (changed("bases/0", "node:24.22.3-trixie-slim@sha256:" + "0" * 64), "bases.0"),
        (changed("recipe", "sha256:" + "0" * 64), "recipe"),
        (changed("image", {"reference": "rhi-evaluate-trial:0", "id": "latest"}), "image.id"),
    ],
)
def test_an_incompatible_inventory_is_detected(change: Callable[[dict[str, Any]], None], fault: str) -> None:
    inventory = copy.deepcopy(INVENTORY)
    change(inventory)
    assert image.faults(inventory) == [fault]
    with pytest.raises(image.Incompatible):
        image.check(inventory)


def recipe_copy(folder: Path) -> Path:
    for name in (*image.RECIPE, image.INVENTORY):
        shutil.copyfile(image.HERE / name, folder / name)
    return folder


def test_a_recipe_that_left_the_pins_is_incompatible_whatever_its_inventory_says(tmp_path: Path) -> None:
    folder = recipe_copy(tmp_path)
    lock = json.loads((folder / image.PACKAGE_LOCK).read_text(encoding="utf-8"))
    lock["packages"][f"node_modules/{harnesses.CODEX.package}"]["version"] = "0.168.0"
    (folder / image.PACKAGE_LOCK).write_text(json.dumps(lock), encoding="utf-8")
    # The stale inventory names the old lock; one rewritten from the recipe names the wrong version.
    assert "locks.package-lock.json" in image.faults(INVENTORY, folder)
    assert image.faults(image.describe(folder), folder) == ["harnesses.codex"]


def test_a_base_without_a_digest_is_incompatible(tmp_path: Path) -> None:
    folder = recipe_copy(tmp_path)
    dockerfile = (folder / image.DOCKERFILE).read_text(encoding="utf-8")
    (folder / image.DOCKERFILE).write_text(re.sub(r"(FROM python:\S+?)@\S+", r"\1", dockerfile), encoding="utf-8")
    assert image.faults(image.describe(folder), folder) == ["bases.1"]


# Preparing, against a stand-in for the container runtime.


class Runtime:
    """A container runtime that holds images by reference, and runs the commands of an image
    whose tools print `versions`."""

    def __init__(self, **versions: str) -> None:
        self.versions = {
            **{harness.command: harness.version for harness in harnesses.HARNESSES},
            "node": harnesses.NODE_VERSION,
            **image.group_pins(),
            **versions,
        }
        self.images: dict[str, tuple[str, str]] = {}  # reference -> (ID, recipe label)
        self.builds: list[list[str]] = []
        self.ran_in: set[str] = set()

    def hold(self, reference: str, label: str) -> str:
        image_id = "sha256:" + f"{len(self.images) + len(self.builds) + 1:064x}"
        self.images[reference] = (image_id, label)
        return image_id

    def __call__(self, arguments: Sequence[str]) -> str:
        arguments = list(arguments)
        if arguments[:2] == ["image", "ls"]:
            filters = dict(arguments[at + 1].split("=", 1) for at, word in enumerate(arguments) if word == "--filter")
            image_id, label = self.images.get(filters["reference"], ("", ""))
            return f"{image_id}\n" if filters["label"] == f"{image.RECIPE_LABEL}={label}" else ""
        if arguments[0] == "build":
            self.builds.append(arguments)
            options = dict(zip(arguments[1:-1:2], arguments[2:-1:2], strict=True))
            self.hold(options["--tag"], options["--label"].split("=", 1)[1])
            return ""
        assert arguments[:3] == ["run", "--rm", "--network=none"], arguments
        self.ran_in.add(arguments[3])
        command = arguments[4:]
        if command[1] == "--version":
            return f"{command[0]} {self.versions[command[0]]}\n"
        if command[2] == image.SMOKE_MODEL:
            return json.dumps({"status": "Optimal", "objective": image.SMOKE_OPTIMUM, "plan": [4.0, 0.0]})
        return self.versions[command[3]] + "\n"


def test_prepare_builds_the_committed_recipe_when_no_image_matches(tmp_path: Path) -> None:
    runtime, record = Runtime(), tmp_path / "local" / "inventory.json"

    prepared = image.prepare("trial", record=record, docker=runtime)

    assert prepared.built
    assert len(runtime.builds) == 1
    assert runtime.builds[0][-1] == str(image.HERE)
    assert prepared.reference == image.reference("trial")
    assert runtime.images[prepared.reference] == (prepared.image_id, INVENTORY["recipe"])
    # Verified by its ID, and that ID is the identity the local inventory records.
    assert runtime.ran_in == {prepared.image_id}
    local = json.loads(record.read_text(encoding="utf-8"))
    assert local == prepared.inventory == {**INVENTORY, "image": {"reference": prepared.reference, "id": prepared.image_id}}
    assert image.faults(local) == []


def test_prepare_reuses_the_image_of_the_same_recipe_and_records_its_id(tmp_path: Path) -> None:
    runtime = Runtime()
    held = runtime.hold(image.reference("trial"), INVENTORY["recipe"])

    prepared = image.prepare("trial", record=tmp_path / "inventory.json", docker=runtime)

    assert not prepared.built
    assert runtime.builds == []
    assert prepared.image_id == held
    assert runtime.ran_in == {held}
    assert json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))["image"]["id"] == held


def test_prepare_rebuilds_over_an_image_of_another_recipe(tmp_path: Path) -> None:
    runtime = Runtime()
    other = runtime.hold(image.reference("trial"), "sha256:" + "0" * 64)

    prepared = image.prepare("trial", record=tmp_path / "inventory.json", docker=runtime)

    assert prepared.built
    assert prepared.image_id != other


@pytest.mark.parametrize("tool", ["codex", "node", "openai", "highspy"])
def test_prepare_refuses_an_image_that_holds_another_version(tmp_path: Path, tool: str) -> None:
    record = tmp_path / "inventory.json"
    with pytest.raises(RuntimeError):
        image.prepare("trial", record=record, docker=Runtime(**{tool: "0.0.1"}))
    assert not record.exists()


def test_prepare_builds_nothing_from_an_incompatible_inventory(tmp_path: Path) -> None:
    folder = recipe_copy(tmp_path)
    inventory = copy.deepcopy(INVENTORY)
    inventory["harnesses"]["claude_code"] = "2.1.322"
    (folder / image.INVENTORY).write_text(json.dumps(inventory), encoding="utf-8")

    def no_runtime(arguments: Sequence[str]) -> str:
        raise AssertionError(f"docker {arguments[0]} ran")

    with pytest.raises(image.Incompatible):
        image.prepare("trial", record=tmp_path / "local.json", docker=no_runtime, folder=folder)
    assert not (tmp_path / "local.json").exists()


def test_the_locked_solver_solves_the_small_model_to_its_optimum() -> None:
    # The program the image is verified with, on the solver of the same lock.
    def run(command: Sequence[str]) -> str:
        assert command[0] == "python"
        return subprocess.run([sys.executable, *command[1:]], check=True, capture_output=True, text=True).stdout

    solved = image.solve(run)
    assert solved["status"] == "Optimal"
    assert solved["objective"] == pytest.approx(20)
    assert solved["plan"] == pytest.approx([4, 0])
