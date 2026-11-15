"""The actual script and uv in a separate checkout, with no service operations."""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scratch_dry_run(tmp_path):
    tree = tmp_path / "checkout"
    shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache"))
    external = tmp_path / "configuration.toml"
    external.write_bytes((tree / "config.example.toml").read_bytes())
    env = {k: v for k, v in os.environ.items() if k not in ("UV_PROJECT_ENVIRONMENT", "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV")}
    env.update(TMPDIR=str(tmp_path), UV_OFFLINE="1")

    def snapshot():
        return {str(p.relative_to(tree)): None if p.is_dir() else p.read_bytes() for p in tree.rglob("*")}

    before = snapshot()
    for extra, expected in (([], "24"), (["--upgrade-pool"], "120")):
        done = subprocess.run(
            ["bash", "setup/owner-setup.sh", "--dry-run", "--max-instances", "2", "--config", str(external), *extra],
            cwd=tree, env=env, capture_output=True, text=True, check=False,
        )
        assert done.returncode == 0, done.stderr
        assert re.findall(r"^== Step (\d) of 9:", done.stdout, re.MULTILINE) == list("123456789")
        assert re.findall(r"--desired-value (\d+)", done.stdout) == [expected]
        assert snapshot() == before
    assert not list(tmp_path.glob("rhi-owner-setup.*"))
