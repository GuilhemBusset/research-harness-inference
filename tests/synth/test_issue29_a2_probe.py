"""Independent CLI-style synthesis with repository write detection."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def test_script_synthesis_is_unbound_and_writes_to_selected_directory(tmp_path):
    root = Path.cwd()
    excluded = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".ty_cache"}

    def snapshot():
        result = {}
        for directory, folders, files in os.walk(root):
            folders[:] = [folder for folder in folders if folder not in excluded]
            for name in files:
                path = Path(directory) / name
                stat = path.stat()
                result[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
        return result

    environment = dict(os.environ)
    environment.update(RHI_CONFIG="example", CDK_OUTDIR=str(tmp_path), JSII_RUNTIME_PACKAGE_CACHE="disabled")
    environment.pop("PYTHONPATH", None)
    before = snapshot()
    result = subprocess.run(
        [sys.executable, str(root / "app.py")], cwd=tmp_path, env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot() == before
    templates = list(tmp_path.rglob("*.template.json"))
    assert templates
    for path in templates:
        text = json.dumps(json.loads(path.read_text()))
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", text)
        assert not re.search(r"\b(?:[a-z]{2}|us-gov|us-iso[a-z]?|eusc-de)-(?:[a-z]+-)+\d\b", text)
