"""Independent examination of issue 24's real manifests and regression checks."""

import importlib.util
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def read_json(name):
    return json.loads((ROOT / name).read_text())


def load_checks():
    spec = importlib.util.spec_from_file_location("reviewed_toolchain", ROOT / "tests/unit/test_toolchain.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_d1_project_and_lock_pins():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {package["name"]: package for package in lock["package"]}
    floors = {"aws-cdk-lib": (2, 276, 0), "constructs": (0, 0, 0), "cdk-nag": (3, 0, 2)}
    for name, floor in floors.items():
        requirements = [value for value in project["project"]["dependencies"] if value.startswith(name + "==")]
        assert len(requirements) == 1
        version = requirements[0].split("==")[1]
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        assert tuple(map(int, version.split("."))) >= floor
        assert locked[name]["version"] == version
    assert {"name": "constructs"} in locked["aws-cdk-lib"]["dependencies"]


def test_d2_only_cli_and_consistent_npm_lock():
    package = read_json("package.json")
    lock = read_json("package-lock.json")["packages"]
    declarations = [
        (name, version)
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
        for name, version in package.get(section, {}).items()
    ]
    assert len(declarations) == 1
    name, version = declarations[0]
    assert name == "aws-cdk"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    assert tuple(map(int, version.split("."))) >= (2, 1148, 1)
    assert lock[""]["dependencies"] == {name: version}
    assert lock["node_modules/aws-cdk"]["version"] == version
    assert not package.get("engineStrict", False)


def test_d4_checks_read_changed_node_file(tmp_path, monkeypatch):
    checks = load_checks()
    changed = tmp_path / ".node-version"
    changed.write_text("24\n")
    monkeypatch.setattr(checks, "NODE_VERSION", changed)
    checks.test_node_version_names_a_major_the_engines_allow()
    changed.write_text("26\n")
    with pytest.raises(AssertionError):
        checks.test_node_version_names_a_major_the_engines_allow()


def test_a1_locked_sync_without_aws_material():
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_"))
        and key not in {"VIRTUAL_ENV", "UV_ENV_FILE"}
    }
    environment.update(
        AWS_CONFIG_FILE="/dev/null",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null",
        AWS_EC2_METADATA_DISABLED="true",
        UV_NO_ENV_FILE="1",
    )
    result = subprocess.run(["uv", "sync", "--locked"], cwd=ROOT, env=environment, capture_output=True, check=False)
    assert result.returncode == 0, "credential-free locked sync must succeed"


def test_a2_node_major_and_engines():
    major = (ROOT / ".node-version").read_text().strip()
    alternatives = {part.strip() for part in read_json("package.json")["engines"]["node"].split("||")}
    assert major == "24"
    assert alternatives == {"22", "24"}
    assert major in alternatives
    npmrc = ROOT / ".npmrc"
    assert not npmrc.exists() or "engine-strict=true" not in npmrc.read_text().replace(" ", "")


def test_a3_real_pin_checks_reject_copies_with_ranges(tmp_path):
    checks = load_checks()
    original = (ROOT / "pyproject.toml").read_text()
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(original)
    assert not checks.library_pin_problems(pyproject)
    for name in ("aws-cdk-lib", "constructs", "cdk-nag"):
        changed = original.replace(name + "==", name + ">=")
        assert changed != original
        pyproject.write_text(changed)
        assert checks.library_pin_problems(pyproject)
    package = read_json("package.json")
    copied = tmp_path / "package.json"
    copied.write_text(json.dumps(package))
    assert not checks.cli_pin_problems(copied)
    package["dependencies"]["aws-cdk"] = "^" + package["dependencies"]["aws-cdk"]
    copied.write_text(json.dumps(package))
    assert checks.cli_pin_problems(copied)


def test_a4_committed_test_fails_on_second_dependency(tmp_path, monkeypatch):
    checks = load_checks()
    package = read_json("package.json")
    copied = tmp_path / "package.json"
    monkeypatch.setattr(checks, "PACKAGE", copied)
    copied.write_text(json.dumps(package))
    checks.test_the_cli_is_the_only_node_dependency()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        changed = json.loads(json.dumps(package))
        changed.setdefault(section, {})["another-package"] = "1.0.0"
        copied.write_text(json.dumps(changed))
        with pytest.raises(AssertionError):
            checks.test_the_cli_is_the_only_node_dependency()
