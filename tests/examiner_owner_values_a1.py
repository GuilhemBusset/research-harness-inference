"""Check the complete local-only path against a scratch checkout and the real scanner."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from rhi import config

ROOT = Path(__file__).resolve().parents[1]


def test_local_list_is_private_repeatable_and_activates_the_scanner(tmp_path):
    tree = tmp_path / "checkout"
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode().split("\0")
    for name in filter(None, tracked):
        destination = tree / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    before = {name: (tree / name).read_bytes() for name in filter(None, tracked)}
    external = tmp_path / "configuration.toml"
    external.write_bytes((ROOT / "config.example.toml").read_bytes())
    values = tmp_path / "private" / "values"
    extra = "Additional placeholder"
    loaded = config.load({"RHI_CONFIG": "example"})
    expected = {str(getattr(loaded, key)) for key in config.PRIVATE_KEYS} | {extra}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_"))}
    env.update(HYGIENE_LOCAL_VALUES=str(values), TMPDIR=str(tmp_path))
    for _ in range(2):
        done = subprocess.run(
            ["bash", "setup/owner-setup.sh", "--step", "values", "--config", str(external)],
            cwd=tree, env=env, input=f"{extra}\n\ny\n", capture_output=True, text=True, check=False,
        )
        assert done.returncode == 0, done.stderr
        lines = values.read_text().splitlines()
        assert set(lines) == expected and len(lines) == len(expected)
        assert stat.S_IMODE(values.stat().st_mode) == 0o600
        assert f"holds {len(expected)} values" in done.stdout
        assert all(value not in done.stdout + done.stderr for value in expected)
        after = {str(path.relative_to(tree)): path.read_bytes()
                 for path in tree.rglob("*") if path.is_file()}
        assert after == before
    scratch = tmp_path / "scratch.txt"
    scratch.write_text(loaded.domain + "\n")
    scanned = subprocess.run(
        [sys.executable, str(ROOT / "tools/hygiene/scan.py"), "--paths", str(scratch)],
        cwd=tree, env=env, capture_output=True, text=True, check=False,
    )
    assert scanned.returncode == 1, scanned.stderr
    assert f"{scratch}:1: local-value:" in scanned.stdout
    assert loaded.domain not in scanned.stdout + scanned.stderr
