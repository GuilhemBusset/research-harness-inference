"""Build the real app from a separately loaded configuration."""

import json
import runpy
from pathlib import Path

from rhi.config import load


def test_loaded_configuration_and_unbound_environments(tmp_path, monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    module = runpy.run_path(str(Path("app.py").resolve()))
    source = tmp_path / "configuration.toml"
    source.write_text(Path("config.example.toml").read_text().replace("example-owner", "probe-owner"))
    config = load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(source)})
    app = module["build"](config, outdir=str(tmp_path / "assembly"))
    stages = app.node.children
    assert len(stages) == 1
    assert stages[0].config == config
    assert stages[0].config.github_owner == "probe-owner"
    app.synth()
    count = 0
    for path in (tmp_path / "assembly").rglob("manifest.json"):
        manifest = json.loads(path.read_text())
        assert not manifest.get("missing")
        for artifact in manifest["artifacts"].values():
            if artifact["type"] == "aws:cloudformation:stack":
                count += 1
                assert artifact["environment"] == "aws://unknown-account/unknown-region"
    assert count == 1
