"""Independent executable evidence for service packaging (issue 36)."""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


SERVICE_LIBRARIES = {
    "gateway": {"starlette", "httpx", "uvicorn", "prometheus-client", "boto3"},
    "mock-engine": {"starlette", "uvicorn", "prometheus-client"},
    "scaler": {"boto3", "protobuf", "cramjam"},
    "reporter": {"boto3", "pyjwt", "cryptography"},
    "issuer": {"boto3"},
    "lifecycle": {"boto3", "prometheus-client"},
    "admin": {"boto3"},
    "cli": {"keyring"},
}


def normalize(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def test_dependencies_and_dev_environment():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    groups = project["dependency-groups"]
    assert {d["include-group"] for d in groups["dev"] if isinstance(d, dict)} == set(SERVICE_LIBRARIES)
    packages = {p["name"]: p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    for group, names in SERVICE_LIBRARIES.items():
        actual = set()
        for declaration in groups[group]:
            match = re.fullmatch(r"([\w.-]+)==([\d][\w.!+]*)", declaration)
            assert match, declaration
            name = normalize(match[1])
            assert packages[name]["version"] == match[2]
            actual.add(name)
            module = {"pyjwt": "jwt", "protobuf": "google.protobuf", "prometheus-client": "prometheus_client"}.get(name, name)
            importlib.import_module(module)
        assert actual == names
    assert any(isinstance(d, str) and d.startswith("moto[dynamodb]==") for d in groups["dev"])
    importlib.import_module("moto")
    for declaration in groups["dev"]:
        if isinstance(declaration, str):
            assert re.fullmatch(r"[\w.-]+(?:\[[\w,.-]+\])?==[\d][\w.!+]*", declaration)


