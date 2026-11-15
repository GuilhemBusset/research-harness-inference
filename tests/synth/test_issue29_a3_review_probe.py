"""Verify that the required feature-flag test rejects a real configuration mutation."""

import json
import runpy
from pathlib import Path

import pytest


def test_feature_flag_test_rejects_non_flag_context(cloud_assembly, tmp_path, monkeypatch):
    namespace = runpy.run_path(str(Path("tests/synth/test_app.py").resolve()))
    check = namespace["test_cdk_json_holds_flags_only"]
    # First require the real check to pass against the unchanged repository file.
    check(cloud_assembly)
    settings = json.loads(Path("cdk.json").read_text())
    settings["context"]["reviewNonFeatureFlag"] = True
    changed = tmp_path / "cdk.json"
    changed.write_text(json.dumps(settings))
    # Change the input read by the actual test without editing the checkout.
    monkeypatch.setitem(check.__globals__, "CDK_JSON", changed)
    with pytest.raises(AssertionError):
        check(cloud_assembly)
