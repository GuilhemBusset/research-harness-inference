"""Independent executable evidence for service packaging (issue 36)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("group", [*SERVICE_LIBRARIES, "dev"])
def test_named_pin_test_rejects_a_range(tmp_path, group):
    """Run the delivered test against a separately mutated project, bypassing uv resolution."""
    project = (ROOT / "pyproject.toml").read_text()
    declaration = next(d for d in tomllib.loads(project)["dependency-groups"][group] if isinstance(d, str))
    replacement = declaration.replace("==", ">=", 1)
    (tmp_path / "pyproject.toml").write_text(project.replace('"' + declaration + '"', '"' + replacement + '"'))
    shutil.copyfile(ROOT / "uv.lock", tmp_path / "uv.lock")
    test = tmp_path / "tests" / "synth" / "test_packaging.py"
    test.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tests/synth/test_packaging.py", test)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test) + "::test_every_group_is_pinned"],
        capture_output=True, text=True, cwd=ROOT, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert replacement in result.stdout


