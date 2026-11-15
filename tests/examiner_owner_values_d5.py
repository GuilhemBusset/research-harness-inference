"""The values step operates with the list wholly in a temporary directory."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_real_step_with_an_explicit_temporary_list(tmp_path):
    external = tmp_path / "configuration.toml"
    external.write_bytes((ROOT / "config.example.toml").read_bytes())
    values = tmp_path / "local-values"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_"))}
    env.update(HYGIENE_LOCAL_VALUES=str(values), TMPDIR=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    done = subprocess.run(
        ["bash", "setup/owner-setup.sh", "--step", "values", "--config", str(external)],
        cwd=ROOT, env=env, input="\ny\n", capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert values.is_file() and "example.com" in values.read_text().splitlines()
    assert not list(tmp_path.glob("rhi-owner-setup.*"))
