"""Exercise both packaging paths with a wheel-install recorder and no docker."""

from __future__ import annotations

import shutil
from pathlib import Path


def test_both_paths_work_without_docker(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("node", "uv"):
        located = shutil.which(name)
        assert located
        (tools / name).symlink_to(located)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    assert shutil.which("docker") is None
    import aws_cdk as cdk
    from aws_cdk import aws_ecr_assets, aws_lambda

    from rhi import packaging

    calls = []

    def record(command):
        calls.append((list(command), Path(command[command.index("--requirements") + 1]).read_text()))

    monkeypatch.setattr(packaging, "install", record)
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stack = cdk.Stack(app, "BothPaths")
    aws_lambda.Function(
        stack, "Scaler", runtime=packaging.RUNTIME, handler="rhi_scaler.handler",
        code=packaging.function_code("scaler", aws_lambda.Architecture.X86_64),
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    context = Path(packaging.image_context("mock-engine", dockerfile))
    aws_ecr_assets.DockerImageAsset(stack, "Mock", directory=str(context))
    template = app.synth().get_stack_by_name(stack.stack_name).template
    assert any(r["Type"] == "AWS::Lambda::Function" for r in template["Resources"].values())
    assert len(calls) == 1
    command, requirements = calls[0]
    assert command[:3] == ["uv", "pip", "install"]
    assert "--only-binary" in command
    assert "boto3==" in requirements and "cramjam==" in requirements
    assert {p.name for p in context.iterdir()} == {"rhi_common", "rhi_mock_engine", "Dockerfile", "requirements.txt"}
