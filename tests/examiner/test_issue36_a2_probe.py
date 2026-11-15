"""Independent executable evidence for service packaging (issue 36)."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def lock_closure(service):
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["name"]
    pending = [d["name"] for d in packages[project]["dev-dependencies"][service]]
    found = {}
    while pending:
        name = pending.pop()
        if name in found:
            continue
        package = packages[name]
        found[name] = (
            package["version"],
            {f["hash"] for f in [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]},
        )
        pending.extend(d["name"] for d in package.get("dependencies", []))
    return found


def exported_requirements(text):
    entries = {}
    for line in text.replace("\\\n", " ").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement, *hashes = line.split("--hash=")
        match = re.fullmatch(r"([\w.-]+)==([^\s;]+)(?:\s*;.*)?", requirement.strip())
        assert match, line
        entries[normalize(match[1])] = (match[2], {h.strip() for h in hashes})
    return entries


@pytest.fixture
def packaging_tools(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("node", "uv"):
        located = shutil.which(name)
        assert located
        (tools / name).symlink_to(located)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    assert shutil.which("docker") is None
    from rhi import packaging
    return packaging


def miniature_repo(tmp_path, packaging):
    repo = tmp_path / "repository"
    repo.mkdir()
    for filename in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / filename, repo / filename)
    for relative in (packaging.COMMON, *packaging.SERVICES.values()):
        package = repo / relative
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("VALUE = " + repr(package.name) + "\n")
        (package / "__pycache__").mkdir()
        (package / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    (repo / "unrelated.txt").write_text("not a service file\n")
    return repo


def normalize(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def test_image_hash_ignores_other_groups_and_tracks_its_inputs(tmp_path, packaging_tools):
    import aws_cdk as cdk
    from aws_cdk.aws_ecr_assets import DockerImageAsset

    packaging = packaging_tools
    repo = miniature_repo(tmp_path, packaging)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\nCOPY . /app\n")
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stack = cdk.Stack(app, "Images")

    def asset(number):
        directory = Path(packaging.image_context("gateway", dockerfile, repo=repo))
        assert {p.name for p in directory.iterdir()} == {"rhi_common", "rhi_gateway", "Dockerfile", "requirements.txt"}
        assert exported_requirements((directory / "requirements.txt").read_text()) == lock_closure("gateway")
        assert not list(directory.rglob("*.pyc"))
        return DockerImageAsset(stack, "Image" + str(number), directory=str(directory)).asset_hash

    initial = asset(0)
    (repo / "unrelated.txt").write_text("different\n")
    (repo / packaging.SERVICES["scaler"] / "__init__.py").write_text("DIFFERENT = True\n")
    lock = (repo / "uv.lock").read_text()
    keyring = next(p for p in tomllib.loads(lock)["package"] if p["name"] == "keyring")
    digest = keyring["wheels"][0]["hash"]
    (repo / "uv.lock").write_text(lock.replace(digest, "sha256:" + "0" * 64))
    assert asset(1) == initial
    (repo / packaging.COMMON / "__init__.py").write_text("DIFFERENT = True\n")
    changed = asset(2)
    assert changed != initial
    dockerfile.write_text("FROM scratch\nCOPY . /changed\n")
    assert asset(3) != changed
    assembly = app.synth()
    manifest = json.loads((Path(assembly.directory) / "Images.assets.json").read_text())
    assert len(manifest["dockerImages"]) == 3


