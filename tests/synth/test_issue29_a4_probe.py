"""Exercise app.py's real path bootstrap in a fresh interpreter."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_child_outside_checkout_can_import_every_root(tmp_path):
    root = Path.cwd()
    assert not tmp_path.is_relative_to(root)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["JSII_RUNTIME_PACKAGE_CACHE"] = "disabled"
    child = (
        "import runpy,sys,json,importlib; runpy.run_path(sys.argv[1]); "
        "common=importlib.import_module('rhi_common'); "
        "print(json.dumps([sys.path,common.__file__]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", child, str(root / "app.py")], cwd=tmp_path, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    paths, common = json.loads(result.stdout.splitlines()[-1])
    roots = tomllib.loads(Path("pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert roots
    assert all(str(root / folder) in paths for folder in roots)
    assert Path(common).is_relative_to(root / "services/common")
