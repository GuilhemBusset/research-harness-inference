"""Independent executable evidence for service packaging (issue 36)."""

from __future__ import annotations

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


@pytest.mark.parametrize("architecture,platform", [("ARM_64", "aarch64-manylinux_2_28"), ("X86_64", "x86_64-manylinux_2_28")])
def test_lambda_assets_are_local_locked_and_architecture_specific(tmp_path, monkeypatch, packaging_tools, architecture, platform):
    import aws_cdk as cdk
    from aws_cdk import aws_lambda

    packaging = packaging_tools
    repo = miniature_repo(tmp_path, packaging)
    calls = []

    def record(command):
        option = lambda flag: command[command.index(flag) + 1]
        assert command[:3] == ["uv", "pip", "install"]
        assert option("--python-platform") == platform
        assert option("--python-version") == "3.13"
        assert option("--only-binary") == ":all:"
        assert "--no-deps" in command and "--require-hashes" in command
        assert exported_requirements(Path(option("--requirements")).read_text()) == lock_closure("reporter")
        calls.append(Path(option("--target")))

    monkeypatch.setattr(packaging, "install", record)
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stack = cdk.Stack(app, "Probe")
    arch = getattr(aws_lambda.Architecture, architecture)
    aws_lambda.Function(stack, "Reporter", runtime=packaging.RUNTIME, architecture=arch,
                        handler="rhi_reporter.handler", code=packaging.function_code("reporter", arch, repo=repo))
    assembly = app.synth()
    template = assembly.get_stack_by_name(stack.stack_name).template
    function = next(r["Properties"] for r in template["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    assert function["Runtime"] == "python3.13"
    assert function["Architectures"] == ["arm64" if architecture == "ARM_64" else "x86_64"]
    assert len(calls) == 1
    asset = Path(assembly.directory) / ("asset." + function["Code"]["S3Key"].removesuffix(".zip"))
    assert {p.relative_to(asset).as_posix() for p in asset.rglob("*") if p.is_file()} == {
        "rhi_common/__init__.py", "rhi_reporter/__init__.py",
    }


