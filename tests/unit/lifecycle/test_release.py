"""The release comparison (`rhi_lifecycle.release`; system design, section 4.3), with a stub for
the pool's parameter and a temporary file for the instance's disk. No test here reaches AWS."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from rhi_lifecycle import release

if TYPE_CHECKING:
    from pathlib import Path

NAME = "/rhi/pools/coursework/release"
REPOSITORY = "123456789012.dkr.ecr.us-east-1.amazonaws.com/rhi/engine"

# The parameter's value, with the keys the issue fixes. No start has been measured yet.
VALUE: dict[str, Any] = {
    "engine_image": f"{REPOSITORY}@sha256:{'a' * 64}",
    "model_revision": "0123456789abcdef0123456789abcdef01234567",
    "engine_flags": ["--max-model-len", "65536", "--enable-auto-tool-choice"],
    "files": {"bucket": "artifacts", "key": "releases/coursework.tar", "sha256": "b" * 64},
    "settings": {
        "pool": "coursework",
        "group": "rhi-coursework",
        "launch_hook": "launch",
        "termination_hook": "termination",
        "log_group": "/rhi/coursework",
        "workspace_url": "https://aps-workspaces.example.com/api/v1/remote_write",
        "heartbeat_table": "rhi-heartbeats",
        "secret_id": "rhi/coursework/engine",
    },
}


class Parameter:
    """An SSM client, as far as the module uses one: one parameter, or a read that fails."""

    def __init__(self, value: object) -> None:
        self.value = value

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        if request != {"Name": NAME} or self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "no"}}, "GetParameter")
        return {"Parameter": {"Name": NAME, "Value": self.value}}


def changed(*path: str, to: object) -> dict[str, Any]:
    """The value, with the key at `path` set to `to`."""
    value = copy.deepcopy(VALUE)
    table = value
    for key in path[:-1]:
        table = table[key]
    table[path[-1]] = to
    return value


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    """The record of an instance that was prepared with the release of `VALUE`."""
    record = tmp_path / "release.json"
    record.write_text(
        json.dumps({key: VALUE[key] for key in ("engine_image", "model_revision", "engine_flags", "files")})
    )
    return record


def check(value: object, disk: Path) -> release.Verdict:
    return release.check(Parameter(value if value is None or isinstance(value, str) else json.dumps(value)), NAME, disk)


def test_an_instance_with_the_current_release_continues(disk: Path) -> None:
    verdict = check(VALUE, disk)

    assert verdict.equal
    assert verdict.current is not None
    assert verdict.held == verdict.current.release
    assert verdict.held == release.Release(
        VALUE["engine_image"], VALUE["model_revision"], tuple(VALUE["engine_flags"]), "b" * 64
    )
    assert verdict.current.measured_start_seconds is None
    assert verdict.current.settings == release.Settings(**VALUE["settings"])


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(changed("engine_image", to=f"{REPOSITORY}@sha256:{'c' * 64}"), id="image-digest"),
        pytest.param(changed("model_revision", to="f" * 40), id="model-revision"),
        pytest.param(changed("engine_flags", to=["--max-model-len", "32768", "--enable-auto-tool-choice"]), id="flag"),
        pytest.param(changed("engine_flags", to=[*VALUE["engine_flags"], "--enable-prefix-caching"]), id="added-flag"),
        pytest.param(changed("files", "sha256", to="d" * 64), id="files-digest"),
    ],
)
def test_an_instance_whose_release_differs_is_told_to_abandon(current: dict[str, Any], disk: Path) -> None:
    verdict = check(current, disk)

    assert not verdict.equal
    # Both were read: the verdict comes from the comparison.
    assert verdict.current is not None
    assert verdict.held is not None


def test_a_parameter_that_differs_only_in_the_measured_start_is_equal(disk: Path) -> None:
    verdict = check(changed("measured_start_seconds", to=412), disk)

    assert verdict.equal
    assert verdict.current is not None
    assert verdict.current.measured_start_seconds == 412


@pytest.mark.parametrize("setting", sorted(VALUE["settings"]))
def test_a_parameter_that_differs_only_in_one_setting_is_equal(setting: str, disk: Path) -> None:
    verdict = check(changed("settings", setting, to="another"), disk)

    assert verdict.equal
    assert verdict.current is not None
    assert getattr(verdict.current.settings, setting) == "another"


def test_a_parameter_that_cannot_be_read_counts_as_not_equal(disk: Path) -> None:
    verdict = check(None, disk)

    assert not verdict.equal
    assert verdict.current is None
    assert verdict.held is not None


def without(*path: str) -> dict[str, Any]:
    value = changed(*path, to=None)
    table = value
    for key in path[:-1]:
        table = table[key]
    del table[path[-1]]
    return value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("not JSON", id="not-json"),
        pytest.param([VALUE], id="not-an-object"),
        pytest.param(without("engine_image"), id="no-image"),
        pytest.param(changed("engine_image", to=f"{REPOSITORY}:latest"), id="image-by-tag"),
        pytest.param(without("model_revision"), id="no-revision"),
        pytest.param(changed("engine_flags", to="--max-model-len 65536"), id="flags-not-a-list"),
        pytest.param(without("files", "sha256"), id="no-files-digest"),
        pytest.param(without("files", "bucket"), id="no-bucket"),
        pytest.param(changed("measured_start_seconds", to="soon"), id="start-not-a-number"),
        pytest.param(without("settings", "launch_hook"), id="no-launch-hook"),
    ],
)
def test_a_parameter_that_does_not_hold_a_release_counts_as_not_equal(value: object, disk: Path) -> None:
    verdict = check(value, disk)

    assert not verdict.equal
    assert verdict.current is None


@pytest.mark.parametrize("record", [None, "not JSON", json.dumps({"engine_image": VALUE["engine_image"]})])
def test_a_disk_that_holds_no_release_counts_as_not_equal(record: str | None, disk: Path) -> None:
    if record is None:
        disk.unlink()
    else:
        disk.write_text(record)

    verdict = check(VALUE, disk)

    assert not verdict.equal
    assert verdict.current is not None
    assert verdict.held is None
