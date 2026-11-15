"""Independent issue 32 synthesis probe."""
import importlib.util
import json
from pathlib import Path

import pytest


def wiring(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    spec = importlib.util.spec_from_file_location("examiner32_app", Path("app.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("owner", [None, "stage", "nag"])
def test_same_finding_depends_on_the_creating_module(owner, monkeypatch, tmp_path):
    module = wiring(monkeypatch)
    import aws_cdk as cdk

    from rhi import config
    from rhi.stage import EndpointStage

    folder = tmp_path / "listed"
    folder.mkdir()
    if owner:
        (folder / f"{owner}.toml").write_text(
            '[nag]\n"AwsSolutions-SNS3" = "This independent topic holds no messages."\n'
        )
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    stage = EndpointStage(app, "Endpoint", config=config.load({"RHI_CONFIG": "example"}))
    cdk.aws_sns.Topic(stage.stack, "Topic")
    module.add_nag(app, acknowledged=folder)
    if owner == "stage":
        app.synth()
        assert list((tmp_path / "assembly").rglob("*.template.json"))
    else:
        with pytest.raises(RuntimeError):
            app.synth()
        report = json.loads((tmp_path / "assembly" / "validation-report.json").read_text())
        assert any(
            v["ruleName"] == "AwsSolutions-SNS3"
            for plugin in report["pluginReports"]
            for v in plugin.get("violations", [])
        )
