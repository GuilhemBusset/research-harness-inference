"""Independent issue 32 synthesis probe."""
import importlib.util
import json
from pathlib import Path

import pytest


def wiring(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    spec = importlib.util.spec_from_file_location("examiner32_app", Path("app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_rejects_a_real_finding(monkeypatch, tmp_path):
    module = wiring(monkeypatch)
    import aws_cdk as cdk

    from rhi import config

    app = module.build(config.load({"RHI_CONFIG": "example"}), outdir=str(tmp_path))
    cdk.aws_sns.Topic(cdk.Stack(app, "Independent"), "Topic")
    with pytest.raises(RuntimeError):
        app.synth()
    report = json.loads((tmp_path / "validation-report.json").read_text())
    assert any(
        violation["ruleName"] == "AwsSolutions-SNS3"
        for plugin in report["pluginReports"]
        for violation in plugin.get("violations", [])
    )
