"""Independent issue 32 synthesis probe."""
import importlib.util
import json
import sys
from pathlib import Path


def wiring(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    spec = importlib.util.spec_from_file_location("examiner32_app", Path("app.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_stage(name, monkeypatch, tmp_path):
    """A source tree of one module, `stage.py`, whose stage holds nothing: the app's own stage now
    brings the network, and a probe needs a resource that only the stage module created."""
    source = tmp_path / "src" / "rhi"
    source.mkdir(parents=True)
    path = source / "stage.py"
    path.write_text("import aws_cdk as cdk\n\n\nclass EndpointStage(cdk.Stage):\n    pass\n")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return source, module.EndpointStage


def test_temporary_acknowledgements_drive_synthesis(monkeypatch, tmp_path):
    module = wiring(monkeypatch)
    import aws_cdk as cdk

    source, EndpointStage = probe_stage("examiner32_d4_stage", monkeypatch, tmp_path)

    folder = tmp_path / "listed"
    folder.mkdir()
    reason = "This independent topic contains no messages."
    (folder / "stage.toml").write_text(f'[nag]\n"AwsSolutions-SNS3" = "{reason}"\n')
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stack = cdk.Stack(EndpointStage(app, "Endpoint"), "Stack")
    cdk.aws_sns.Topic(stack, "Topic")
    module.add_nag(app, acknowledged=folder, source=source)
    app.synth()
    topics = [
        resource
        for template in (tmp_path / "assembly").rglob("*.template.json")
        for resource in json.loads(template.read_text()).get("Resources", {}).values()
        if resource["Type"] == "AWS::SNS::Topic"
    ]
    assert len(topics) == 1
    assert topics[0]["Metadata"]["cdk_nag"]["rules_to_suppress"] == [
        {"id": "AwsSolutions-SNS3", "reason": reason}
    ]
