"""Independent check of the CDK entry point and feature flag catalog."""

import json


def test_entry_point_and_library_feature_flags(cloud_assembly):
    from pathlib import Path

    settings = json.loads(Path("cdk.json").read_text())
    assert set(settings) == {"app", "context"}
    assert settings["app"] == "uv run --locked python app.py"
    manifest = json.loads((cloud_assembly / "manifest.json").read_text())
    reports = [
        entry["properties"]["flags"]
        for entry in manifest["artifacts"].values()
        if entry["type"] == "cdk:feature-flag-report"
    ]
    assert len(reports) == 1
    assert set(settings["context"]) <= set(reports[0])
    assert all(type(value) is bool for value in settings["context"].values())
