"""The CDK toolchain is pinned (system design, section 8.1; evidence E20).

The CDK libraries are exact pins in the project's own dependencies in `pyproject.toml`. The CDK CLI
is an exact pin, and the only dependency, in `package.json`. `.node-version` names a Node that the
engines field of `package.json` allows. A range in any of them would let two machines synthesize
with two toolchains.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"
NODE_VERSION = ROOT / ".node-version"

# The floors are the versions evidence E20 records. It records none for constructs, which is pinned
# at the version the lock resolves for aws-cdk-lib.
LIBRARY_FLOORS = {"aws-cdk-lib": (2, 276, 0), "constructs": (0, 0, 0), "cdk-nag": (3, 0, 2)}
CLI = "aws-cdk"
CLI_FLOOR = (2, 1148, 1)
SUPPORTED_NODE_MAJORS = {22, 24}
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")

EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")


def exact(version: str) -> tuple[int, ...] | None:
    """The version as numbers when it is one exact release, None for a range or anything else."""
    if not EXACT_VERSION.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def library_pin_problems(pyproject: Path) -> list[str]:
    """What keeps the CDK libraries from being exact pins in the project's dependencies; empty when they are."""
    requirements = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    specifiers = {}
    for requirement in requirements:
        name, specifier = re.fullmatch(r"([A-Za-z0-9._-]+)\s*(.*)", requirement).groups()
        specifiers[name.lower().replace("_", "-")] = specifier
    problems = []
    for library, floor in LIBRARY_FLOORS.items():
        specifier = specifiers.get(library)
        if specifier is None:
            problems.append(f"{library} is not in the project's dependencies")
            continue
        version = exact(specifier.removeprefix("=="))
        if not specifier.startswith("==") or version is None:
            problems.append(f"{library} is not pinned to an exact version")
        elif version < floor:
            problems.append(f"{library} is below the version evidence E20 records")
    return problems


def node_dependencies(package: dict[str, Any]) -> dict[str, str]:
    """Every dependency `package.json` declares, in any section, with its version."""
    found = {name: version for section in DEPENDENCY_SECTIONS for name, version in package.get(section, {}).items()}
    bundled = package.get("bundleDependencies", package.get("bundledDependencies", []))
    return found | dict.fromkeys(bundled if isinstance(bundled, list) else [], "bundled")


def cli_pin_problems(package: Path) -> list[str]:
    """What keeps the CDK CLI from being an exact pin in `package.json`; empty when it is."""
    declared = node_dependencies(json.loads(package.read_text())).get(CLI)
    if declared is None:
        return [f"{CLI} is not a dependency"]
    version = exact(declared)
    if version is None:
        return [f"{CLI} is not pinned to an exact version"]
    return [f"{CLI} is below the version evidence E20 records"] if version < CLI_FLOOR else []


def engine_majors(package: dict[str, Any]) -> set[int]:
    """The Node majors the engines field allows, when it is a list of majors such as `22 || 24`."""
    majors = set()
    for alternative in package["engines"]["node"].split("||"):
        match = re.fullmatch(r"\^?(\d+)(\.x)?", alternative.strip())
        assert match, f"the engines field is not a list of Node majors: {alternative!r}"
        majors.add(int(match.group(1)))
    return majors


def test_the_cdk_libraries_are_exact_pins() -> None:
    assert library_pin_problems(PYPROJECT) == []


def test_the_cli_is_an_exact_pin_and_the_lock_agrees() -> None:
    assert cli_pin_problems(PACKAGE) == []
    version = node_dependencies(json.loads(PACKAGE.read_text()))[CLI]
    locked = json.loads(PACKAGE_LOCK.read_text())["packages"]
    assert node_dependencies(locked[""]) == {CLI: version}
    assert locked[f"node_modules/{CLI}"]["version"] == version


def test_a_range_fails(tmp_path: Path) -> None:
    text = PYPROJECT.read_text()
    copy = tmp_path / "pyproject.toml"
    for library in LIBRARY_FLOORS:
        for operator in (">=", "~="):
            loosened = text.replace(f'"{library}==', f'"{library}{operator}')
            assert loosened != text
            copy.write_text(loosened)
            problems = library_pin_problems(copy)
            assert len(problems) == 1 and library in problems[0], problems

    package = json.loads(PACKAGE.read_text())
    section = next(name for name in DEPENDENCY_SECTIONS if CLI in package.get(name, {}))
    version = package[section][CLI]
    copy = tmp_path / "package.json"
    for ranged in (f"^{version}", f"~{version}", f">={version}", "2.x", "latest"):
        package[section][CLI] = ranged
        copy.write_text(json.dumps(package))
        assert cli_pin_problems(copy), ranged


def test_the_cli_is_the_only_node_dependency() -> None:
    package = json.loads(PACKAGE.read_text())
    assert set(node_dependencies(package)) == {CLI}
    # The same reading sees a second dependency, whichever section it arrives in.
    for section in DEPENDENCY_SECTIONS:
        grown = package | {section: package.get(section, {}) | {"aws-cdk-lib": "2.276.0"}}
        assert set(node_dependencies(grown)) != {CLI}, section
    assert set(node_dependencies(package | {"bundleDependencies": ["constructs"]})) != {CLI}


def test_the_engines_are_node_22_or_24_and_not_strict() -> None:
    package = json.loads(PACKAGE.read_text())
    assert engine_majors(package) == SUPPORTED_NODE_MAJORS
    # A strict engine check would make `npm ci` fail, instead of warn, on a machine with a newer Node.
    assert "engineStrict" not in package
    npmrc = ROOT / ".npmrc"
    assert not npmrc.exists() or "engine-strict" not in npmrc.read_text()


def test_node_version_names_a_major_the_engines_allow() -> None:
    major = int(NODE_VERSION.read_text().strip().removeprefix("v").split(".")[0])
    assert major == 24
    assert major in engine_majors(json.loads(PACKAGE.read_text()))
