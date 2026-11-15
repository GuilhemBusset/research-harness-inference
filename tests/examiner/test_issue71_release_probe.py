"""Independent checks of the pool release and fail-closed comparison."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError

from rhi_lifecycle.release import check


def document() -> dict[str, Any]:
    return {
        "engine_image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/engine@sha256:" + "1" * 64,
        "model_revision": "2" * 40,
        "engine_flags": ["--port", "8000"],
        "files": {"bucket": "example-artifacts", "key": "release.tar", "sha256": "3" * 64},
        "settings": {
            "pool": "example",
            "group": "example-group",
            "launch_hook": "example-launch",
            "termination_hook": "example-termination",
            "log_group": "/example/engine",
            "workspace_url": "https://example.com/remote_write",
            "heartbeat_table": "example-heartbeat",
            "secret_id": "example/secret",
        },
    }


class Parameter:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.requests: list[dict[str, str]] = []

    def get_parameter(self, **request: str) -> dict[str, Any]:
        self.requests.append(request)
        if self.value is None:
            raise EndpointConnectionError(endpoint_url="https://example.com")
        return {"Parameter": {"Value": self.value}}


@pytest.mark.parametrize("changed", [None, "engine_image", "model_revision", "engine_flags", "files"])
def test_each_release_component_controls_the_verdict(tmp_path: Path, changed: str | None) -> None:
    held = document()
    current = copy.deepcopy(held)
    if changed == "files":
        current["files"]["sha256"] = "4" * 64
    elif changed == "engine_image":
        current[changed] = current[changed].replace("1" * 64, "5" * 64)
    elif changed == "model_revision":
        current[changed] = "6" * 40
    elif changed == "engine_flags":
        current[changed] = ["--port", "9000"]
    record = tmp_path / "held.json"
    record.write_text(json.dumps(held))
    parameter = Parameter(json.dumps(current))
    verdict = check(parameter, "/example/release", record)
    assert verdict.equal is (changed is None)
    assert verdict.current is not None and verdict.held is not None
    assert parameter.requests == [{"Name": "/example/release"}]


def test_settings_and_measurement_are_read_but_do_not_change_release(tmp_path: Path) -> None:
    held = document()
    record = tmp_path / "held.json"
    record.write_text(json.dumps(held))
    for setting in held["settings"]:
        current = copy.deepcopy(held)
        current["settings"][setting] = "new-value"
        current["measured_start_seconds"] = 37
        verdict = check(Parameter(json.dumps(current)), "/example/release", record)
        assert verdict.equal
        assert verdict.current is not None
        assert verdict.current.measured_start_seconds == 37
        assert getattr(verdict.current.settings, setting) == "new-value"


@pytest.mark.parametrize("value", [None, "{}", "[]", "invalid-json"])
def test_unreadable_or_invalid_parameter_never_matches(tmp_path: Path, value: str | None) -> None:
    record = tmp_path / "held.json"
    record.write_text(json.dumps(document()))
    verdict = check(Parameter(value), "/example/release", record)
    assert not verdict.equal
    assert verdict.current is None
    assert verdict.held is not None


def test_absent_disk_record_never_matches(tmp_path: Path) -> None:
    verdict = check(Parameter(json.dumps(document())), "/example/release", tmp_path / "missing.json")
    assert not verdict.equal
    assert verdict.current is not None
    assert verdict.held is None
