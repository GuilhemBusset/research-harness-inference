"""Independent issue 32 synthesis probe."""
import importlib.util
import json
from pathlib import Path


def wiring(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    spec = importlib.util.spec_from_file_location("examiner32_app", Path("app.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_temporary_acknowledgements_drive_synthesis(monkeypatch, tmp_path):
    module = wiring(monkeypatch)
    import aws_cdk as cdk

    from rhi import config
    from rhi.stage import EndpointStage

    folder = tmp_path / "listed"
    folder.mkdir()
    reason = "This independent topic contains no messages."
    (folder / "stage.toml").write_text(f'[nag]\n"AwsSolutions-SNS3" = "{reason}"\n')
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stage = EndpointStage(app, "Endpoint", config=config.load({"RHI_CONFIG": "example"}))
    cdk.aws_sns.Topic(stage.stack, "Topic")
    module.add_nag(app, acknowledged=folder)
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
