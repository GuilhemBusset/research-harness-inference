"""Independent executable evidence for service packaging (issue 36)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


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


def test_excluded_stack_never_installs(tmp_path, monkeypatch, packaging_tools):
    import aws_cdk as cdk
    from aws_cdk import aws_lambda

    packaging = packaging_tools
    repo = miniature_repo(tmp_path, packaging)

    def forbidden(command):
        message = "excluded stack installed wheels"
        raise AssertionError(message)

    monkeypatch.setattr(packaging, "install", forbidden)
    app = cdk.App(outdir=str(tmp_path / "assembly"), context={"aws:cdk:bundling-stacks": []})
    stack = cdk.Stack(app, "Excluded")
    aws_lambda.Function(stack, "Issuer", runtime=packaging.RUNTIME, handler="rhi_issuer.handler",
                        code=packaging.function_code("issuer", aws_lambda.Architecture.X86_64, repo=repo))
    template = app.synth().get_stack_by_name(stack.stack_name).template
    assert any(r["Type"] == "AWS::Lambda::Function" for r in template["Resources"].values())


