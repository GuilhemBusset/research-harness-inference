import os
import shutil
import subprocess
from pathlib import Path


def test_key_location_and_byte_identical_dry_run(tmp_path):
    tree = tmp_path / "checkout"
    shutil.copytree(Path(__file__).resolve().parents[2], tree, ignore=shutil.ignore_patterns(
        ".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache"))
    marker = "independent-example-private-key-text"
    outside = tmp_path / "outside.pem"
    outside.write_text(marker)
    inside = tree / "example.pem"
    inside.write_text(marker)
    link = tree / "link.pem"
    link.symlink_to(outside)
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    for name in ("gh", "aws", "uv", "cdk", "npm"):
        executable = tools / name
        executable.write_text('#!/bin/bash\nprintf called >> "$PROBE_CALLS"\nexit 79\n')
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"], PROBE_CALLS=str(calls))

    def snapshot():
        return {str(p.relative_to(tree)): (None if p.is_dir() else p.read_bytes()) for p in tree.rglob("*")}

    for file in (inside, "example.pem", link, outside):
        before = snapshot()
        done = subprocess.run(["bash", "setup/owner-setup.sh", "--dry-run", "--step", "apps",
                               "--reporter-key", str(file)], cwd=tree, env=env, text=True,
                              input="", capture_output=True, check=False)
        assert snapshot() == before
        assert marker not in done.stdout + done.stderr
        assert not calls.exists()
        if file == outside:
            assert done.returncode == 0, done.stderr
            assert "rhi/reporter/github-app" in done.stdout
        else:
            assert done.returncode != 0
            assert "inside the working tree" in done.stderr
