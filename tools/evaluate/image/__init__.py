"""The trial image of the evaluation: its recipe, its inventory, and the one way to get it built
and verified (docs/design/agent-session-evaluation.md, "Course quality and the grader").

A trial runs a student harness in a container of one image. This folder holds everything that
image is made from, as committed files:

- `Dockerfile`: two bases, each pinned by digest. The official Node image at `harnesses.NODE_VERSION`
  installs the four npm harnesses; the Python image the services use holds the result, with the
  SDKs of the fifth harness (the SDK tool loop) and the solver, HiGHS as `highspy`.
- `package.json` and `package-lock.json`: the four harnesses at the pins of `spikes/harnesses.py`,
  and every package they bring at one version with its integrity hash. `npm ci` installs exactly
  that. `allowScripts` names the install scripts that may run.
- `requirements.txt`: the two SDKs and the solver with what they need, each at its version in the
  root `uv.lock` with its hashes. It is `export_command`'s output, kept as a file so that a build
  reads this folder alone.
- `inventory.json`: what the recipe holds. The base images, Node, each harness, each SDK and the
  solver at its version, the digest of each lock file, and the digest of the recipe (its four
  files).

**The inventory is checked, not trusted.** `faults` compares an inventory with what the recipe's
files say (`describe`) and with what the repository pins (`pinned`): a version that moved in
`spikes/harnesses.py` or in the group `spikes` without the locks, a lock that changed without the
inventory, a base with no digest. `check` refuses an inventory with any fault, so a bump is one
change of the pins, the locks and the inventory together. npm rewrites package-lock.json, and
`refresh` rewrites requirements.txt and inventory.json; setup/README.md has the commands.

**Two identities.** The recipe's digest says what an image was asked to be: `prepare` tags and
labels the image with it, and looks an image up by it. It does not say which image that is: two
builds of one recipe are two images, with two IDs. So the image's identity is the ID the
container runtime reports for it. `prepare` reads that ID once the image is built or found, runs
its verification in containers of that ID, and writes it into the *local* inventory: the committed
one plus `image`, in a file of this machine that git ignores (`LOCAL_INVENTORY`).

**Preparing.** `prepare` is the entry point, for the operator (`uv run --locked python
tools/evaluate/image`) and for each container test alike. It needs a clean checkout and a
container runtime, nothing generated beforehand:

1. it checks the committed inventory;
2. it reuses the local image that carries the recipe's digest under the reference it would build,
   or else runs `docker build` on this folder;
3. it verifies that image with no network: each harness prints its pinned version (the check of
   `harnesses.versions`), Node, the SDKs and the solver are the inventory's, and the solver solves
   a small model to its known optimum;
4. it writes the local inventory and returns it.

A step that fails raises, and no local inventory is written for an image that was not verified.
The runtime is reached through one function, `run_docker`, which the tests replace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import harnesses

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

DOCKERFILE = "Dockerfile"
PACKAGE = "package.json"
PACKAGE_LOCK = "package-lock.json"
REQUIREMENTS = "requirements.txt"
# What a build reads, and what the recipe's digest covers.
RECIPE = (DOCKERFILE, PACKAGE, PACKAGE_LOCK, REQUIREMENTS)
# The dependency locks: the harnesses' packages, and the Python libraries.
LOCKS = (PACKAGE_LOCK, REQUIREMENTS)
INVENTORY = "inventory.json"
# The local inventory `prepare` writes on this machine; .gitignore names it.
LOCAL_INVENTORY = HERE / "inventory.local.json"

# The group of pyproject.toml that pins the SDKs and the solver, and the solver's distribution.
GROUP = "spikes"
SOLVER = "highspy"

# The tables of an inventory that hold versions, which an image is verified against.
VERSIONS = ("node", "harnesses", "sdks", "solver")

# The image `prepare` builds: its name, and the label that carries its recipe's digest.
NAME = "rhi-evaluate-trial"
RECIPE_LABEL = "rhi.evaluate.trial.recipe"

BUILD_SECONDS = 1800
RUN_SECONDS = 120

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FROM = re.compile(r"^FROM[ \t]+(\S+)", re.MULTILINE | re.IGNORECASE)
_PINNED_BASE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_NODE_BASE = re.compile(r"node:([^-@]+)[-@]")
# One requirement of a requirements file, as uv exports it: `name==version`, then its hashes.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", re.MULTILINE)

# The version of an installed distribution, as a program for the image's Python.
_DISTRIBUTION = "import sys; from importlib import metadata; print(metadata.version(sys.argv[1]))"

# A small production plan with whole units, for the solver's check. Its relaxation has a better
# value (21, at 3 and 1.5) than any whole plan, so only a mixed-integer solve finds the optimum.
SMOKE_MODEL = """\
import json
import highspy

solver = highspy.Highs()
solver.silent()
solver.setOptionValue("threads", 1)
bikes = solver.addIntegral(lb=0, name="bikes")
trailers = solver.addIntegral(lb=0, name="trailers")
solver.addConstr(6 * bikes + 4 * trailers <= 24)
solver.addConstr(bikes + 2 * trailers <= 6)
solver.maximize(5 * bikes + 4 * trailers)
print(json.dumps({
    "status": solver.modelStatusToString(solver.getModelStatus()),
    "objective": solver.getObjectiveValue(),
    "plan": [solver.val(bikes), solver.val(trailers)],
}))
"""
SMOKE_OPTIMUM = 20.0
_TOLERANCE = 1e-6

type Runner = Callable[[Sequence[str]], str]


class Incompatible(RuntimeError):
    """An inventory that is not the recipe's or not the repository's pins, or an image that does
    not hold what its inventory says."""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def recipe_digest(folder: Path = HERE) -> str:
    """One digest of the recipe: the names and the bytes of the files a build reads."""
    whole = hashlib.sha256()
    for name in RECIPE:
        whole.update(f"{name}\0{digest((folder / name).read_bytes())}\n".encode())
    return "sha256:" + whole.hexdigest()


def requirements(text: str) -> dict[str, str]:
    """The version of each distribution a requirements file pins, by its name."""
    return dict(_REQUIREMENT.findall(text))


def describe(folder: Path = HERE) -> dict[str, Any]:
    """The inventory of the recipe in `folder`, read from its files and from nothing else. A
    version the recipe does not pin is None."""
    bases = _FROM.findall((folder / DOCKERFILE).read_text(encoding="utf-8"))
    node = [found.group(1) for base in bases if (found := _NODE_BASE.match(base))]
    packages = json.loads((folder / PACKAGE_LOCK).read_text(encoding="utf-8")).get("packages", {})
    libraries = requirements((folder / REQUIREMENTS).read_text(encoding="utf-8"))
    return {
        "bases": bases,
        "node": node[0] if len(node) == 1 else None,
        "harnesses": {
            harness.name: packages.get(f"node_modules/{harness.package}", {}).get("version")
            for harness in harnesses.HARNESSES
        },
        "sdks": {name: libraries.get(distribution) for name, distribution in harnesses.SDKS.items()},
        "solver": {SOLVER: libraries.get(SOLVER)},
        "locks": {name: digest((folder / name).read_bytes()) for name in LOCKS},
        "recipe": recipe_digest(folder),
    }


def group_pins(repo: Path = REPO) -> dict[str, str]:
    """The exact version of each library the group `spikes` of pyproject.toml names."""
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(entry.split("==", 1) for entry in project["dependency-groups"][GROUP] if isinstance(entry, str))


def pinned(repo: Path = REPO) -> dict[str, Any]:
    """What the repository pins of the image: Node and the npm harnesses in spikes/harnesses.py,
    the SDKs and the solver in the group `spikes`."""
    group = group_pins(repo)
    return {
        "node": harnesses.NODE_VERSION,
        "harnesses": {harness.name: harness.version for harness in harnesses.HARNESSES},
        "sdks": {name: group.get(distribution) for name, distribution in harnesses.SDKS.items()},
        "solver": {SOLVER: group.get(SOLVER)},
    }


def _flat(value: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Each leaf of nested tables and lists, under its dotted path."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _flat(inner, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        yield prefix + ".length", len(value)
        for index, inner in enumerate(value):
            yield from _flat(inner, f"{prefix}.{index}")
    else:
        yield prefix, value


def faults(inventory: Any, folder: Path = HERE, repo: Path = REPO) -> list[str]:
    """The entries of an inventory that make it incompatible, each by its path; none when it is
    the inventory of the recipe in `folder` and that recipe is the repository's pins.

    `image`, which only a local inventory holds, is not the recipe's to say: its ID has to be one.
    """
    if not isinstance(inventory, dict):
        return ["inventory"]
    described = describe(folder)
    recipe, held = dict(_flat(described)), dict(_flat({k: v for k, v in inventory.items() if k != "image"}))
    found = {path for path in recipe.keys() | held.keys() if path not in recipe or held.get(path) != recipe[path]}
    # The recipe itself: every version the repository pins, and every base by its digest.
    found |= {path for path, version in _flat(pinned(repo)) if version is None or recipe.get(path) != version}
    found |= {f"bases.{index}" for index, base in enumerate(described["bases"]) if not _PINNED_BASE.fullmatch(base)}
    if not described["bases"]:
        found.add("bases")
    if "image" in inventory:
        image = inventory["image"]
        if not (isinstance(image, dict) and isinstance(image.get("id"), str) and _DIGEST.fullmatch(image["id"])):
            found.add("image.id")
    return sorted(found)


def check(inventory: Any, folder: Path = HERE, repo: Path = REPO) -> None:
    """Incompatible when the inventory has a fault."""
    found = faults(inventory, folder, repo)
    if found:
        raise Incompatible("the inventory of the trial image is incompatible at: " + ", ".join(found))


def export_command(repo: Path = REPO) -> list[str]:
    """The uv command that prints requirements.txt: the SDKs and the solver of the group `spikes`
    with what they need, each at its version in `uv.lock` with its hashes. What else the group
    names is for the spikes' own scripts, and is left out with what only it needs. uv reads the
    lock and nothing else: no network."""
    wanted = {*harnesses.SDKS.values(), SOLVER}
    others = sorted(set(group_pins(repo)) - wanted)
    return [
        *("uv", "export", "--quiet", "--project", str(repo), "--locked", "--only-group", GROUP),
        *(f"--prune={name}" for name in others),
        *("--format", "requirements-txt", "--no-header", "--no-annotate"),
    ]


def refresh(folder: Path = HERE, repo: Path = REPO) -> None:
    """Rewrites requirements.txt from `uv.lock`, then inventory.json from the recipe. Incompatible
    when the recipe is still not the repository's pins (package-lock.json is npm's to rewrite)."""
    exported = subprocess.run(export_command(repo), check=True, stdout=subprocess.PIPE, text=True).stdout
    (folder / REQUIREMENTS).write_text(exported, encoding="utf-8")
    inventory = describe(folder)
    (folder / INVENTORY).write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    check(inventory, folder, repo)


def run_docker(arguments: Sequence[str]) -> str:
    """Runs one docker command and returns its standard output. RuntimeError, with what the
    command wrote, when it fails."""
    timeout = BUILD_SECONDS if arguments[0] == "build" else RUN_SECONDS
    done = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"docker {arguments[0]} failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout


def installed(run: Runner) -> dict[str, Any]:
    """The versions an image holds, in the inventory's own tables; `run` runs a command in it and
    returns what it printed. RuntimeError when a harness is not the version pinned."""

    def distribution(name: str) -> str:
        return run(["python", "-c", _DISTRIBUTION, name]).strip()

    commands = {harness.name: Path(harness.command) for harness in harnesses.HARNESSES}
    found = harnesses.versions(commands, run, distribution)
    return {
        "node": harnesses.printed_version(run(["node", "--version"])),
        "harnesses": {harness.name: found[harness.name] for harness in harnesses.HARNESSES},
        "sdks": {name: found[name] for name in harnesses.SDKS},
        "solver": {SOLVER: distribution(SOLVER)},
    }


def solve(run: Runner) -> dict[str, Any]:
    """Solves `SMOKE_MODEL` where `run` runs it: the status, the objective and the plan."""
    return json.loads(run(["python", "-c", SMOKE_MODEL]).strip().splitlines()[-1])


def verify(image: str, inventory: dict[str, Any], docker: Runner = run_docker) -> None:
    """Incompatible unless the image holds every version of the inventory and its solver solves
    the small model to its optimum. Each command runs in a container of its own, with no network."""

    def run(command: Sequence[str]) -> str:
        return docker(["run", "--rm", "--network=none", image, *command])

    found, expected = dict(_flat(installed(run))), dict(_flat({key: inventory[key] for key in VERSIONS}))
    wrong = sorted(path for path in expected if found.get(path) != expected[path])
    if wrong:
        raise Incompatible("the trial image does not hold the inventory's version of: " + ", ".join(wrong))
    solved = solve(run)
    if solved["status"] != "Optimal" or abs(solved["objective"] - SMOKE_OPTIMUM) > _TOLERANCE:
        raise Incompatible("the solver of the trial image did not solve the small model to its optimum")


@dataclass(frozen=True)
class Prepared:
    """A verified image: the reference it is tagged with, the ID the runtime reports for it,
    whether this call built it, and the local inventory written for it."""

    reference: str
    image_id: str
    built: bool
    inventory: dict[str, Any]


def reference(name: str = NAME, folder: Path = HERE) -> str:
    """The reference `prepare` builds and looks up: the name, tagged with the recipe's digest."""
    return f"{name}:{recipe_digest(folder).removeprefix('sha256:')[:16]}"


def _image_id(tagged: str, recipe: str, docker: Runner) -> str | None:
    """The ID of the local image under the reference that carries the recipe's digest, if any."""
    listed = docker(
        ["image", "ls", "--no-trunc", "--format", "{{.ID}}"]
        + ["--filter", f"reference={tagged}", "--filter", f"label={RECIPE_LABEL}={recipe}"]
    ).split()
    return listed[0] if listed else None


def prepare(
    name: str = NAME, *, record: Path = LOCAL_INVENTORY, docker: Runner = run_docker, folder: Path = HERE
) -> Prepared:
    """Builds the image of the committed recipe, or reuses the local one that matches it, verifies
    it and writes its local inventory to `record` (see the module's text)."""
    inventory = json.loads((folder / INVENTORY).read_text(encoding="utf-8"))
    check(inventory, folder)
    recipe, tagged = inventory["recipe"], reference(name, folder)
    image_id = _image_id(tagged, recipe, docker)
    built = image_id is None
    if image_id is None:
        docker(["build", "--tag", tagged, "--label", f"{RECIPE_LABEL}={recipe}", str(folder)])
        image_id = _image_id(tagged, recipe, docker)
        if image_id is None:
            raise RuntimeError("the container runtime does not list the image it built")
    if not _DIGEST.fullmatch(image_id):
        raise RuntimeError("the container runtime reported no image ID")
    # By its ID, not its tag: what is verified is the image the local inventory names.
    verify(image_id, inventory, docker)
    local = {**inventory, "image": {"reference": tagged, "id": image_id}}
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(local, indent=2) + "\n", encoding="utf-8")
    return Prepared(reference=tagged, image_id=image_id, built=built, inventory=local)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/evaluate/image", description="Prepare the evaluation's trial image.")
    parser.add_argument("--refresh", action="store_true", help="rewrite requirements.txt and inventory.json; no build")
    parser.add_argument("--name", default=NAME, help="the image's name (default: %(default)s)")
    parser.add_argument("--record", type=Path, default=LOCAL_INVENTORY, help="where the local inventory is written")
    options = parser.parse_args(arguments)
    if options.refresh:
        refresh()
        print(f"wrote {REQUIREMENTS} and {INVENTORY}")
        return 0
    prepared = prepare(options.name, record=options.record)
    print(f"{'built' if prepared.built else 'reused'} {prepared.reference}")
    print(f"image: {prepared.image_id}")
    print(harnesses.report(dict(_flat({key: prepared.inventory[key] for key in VERSIONS}))))
    print(f"local inventory: {options.record}")
    return 0
