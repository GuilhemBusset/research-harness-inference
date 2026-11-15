"""client/pyproject.toml: the command rhi, with its libraries at the versions uv.lock holds."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def read(name: str) -> dict:
    return tomllib.loads((REPO / name).read_text(encoding="utf-8"))


def test_the_command_is_rhi() -> None:
    assert read("client/pyproject.toml")["project"]["scripts"] == {"rhi": "rhi_cli.main:main"}


def test_each_library_is_pinned_at_the_locked_version() -> None:
    dependencies = read("client/pyproject.toml")["project"]["dependencies"]
    locked = {package["name"]: package["version"] for package in read("uv.lock")["package"]}
    assert dependencies == read("pyproject.toml")["dependency-groups"]["cli"]
    for requirement in dependencies:
        name, version = requirement.split("==")
        assert locked[name] == version
