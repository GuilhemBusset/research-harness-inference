"""Run the real type gate and check that both CDK entry points are covered."""

import os
import subprocess
import tomllib
from pathlib import Path


def test_type_gate_includes_app_and_stage():
    settings = tomllib.loads(Path("ty.toml").read_text())
    includes = settings["src"]["include"]
    for path in (Path("app.py"), Path("src/rhi/stage.py")):
        assert any(path == Path(entry) or Path(entry) in path.parents for entry in includes)
    assert settings["terminal"]["error-on-warning"] is True
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        ["scripts/check.sh", "types"], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
