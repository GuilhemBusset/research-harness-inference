"""Independent scratch-tree file validation through the shell entry point."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_external_configuration(tmp_path):
    tree = tmp_path / "checkout"
    shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache"))
    external = tmp_path / "configuration.toml"
    example = (tree / "config.example.toml").read_text()
    external.write_text(example)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/bash\n[ \"$1 $2 $3 $4 $5 $6 $7\" = 'run --locked --no-dev python -m rhi.owner_config check' ] || exit 93\nshift 4\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    uv.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=str(tmp_path))

    def check(file):
        return subprocess.run(
            ["bash", "setup/owner-setup.sh", "--dry-run", "--step", "configuration", "--config", str(file)],
            cwd=tree, env=env, capture_output=True, text=True, check=False,
        )

    accepted = check(external)
    assert accepted.returncode == 0, accepted.stderr
    inside = check(tree / "config.example.toml")
    assert inside.returncode != 0 and "inside the working tree" in inside.stderr
    external.write_text(example.replace("session_windows = []", 'session_windows = ["refused-window"]'))
    refused = check(external)
    assert refused.returncode != 0 and "'session_windows'" in refused.stderr
    assert "refused-window" not in refused.stdout + refused.stderr
