"""Exercise the real walkthrough and uv, using only example configuration."""

import os
import subprocess
from pathlib import Path

from rhi import config

ROOT = Path(__file__).resolve().parents[1]


def test_private_and_prompted_values_use_the_scanner_default(tmp_path):
    external = tmp_path / "config.toml"
    external.write_bytes((ROOT / "config.example.toml").read_bytes())
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_"))}
    env.update(HYGIENE_LOCAL_VALUES="", XDG_CONFIG_HOME=str(tmp_path / "xdg"), TMPDIR=str(tmp_path))
    done = subprocess.run(
        ["bash", "setup/owner-setup.sh", "--step", "values", "--config", str(external)],
        cwd=ROOT, env=env, input="Additional placeholder\n\ny\n", capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    loaded = config.load({"RHI_CONFIG": "example"})
    expected = {str(getattr(loaded, key)) for key in config.PRIVATE_KEYS} | {"Additional placeholder"}
    values = tmp_path / "xdg/research-harness-inference/hygiene-values"
    assert set(values.read_text().splitlines()) == expected
    ordered = subprocess.run(
        ["bash", "setup/owner-setup.sh", "--dry-run"], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False,
    )
    assert ordered.returncode == 0, ordered.stderr
    assert ordered.stdout.index("Step 4 of") < ordered.stdout.index("Step 4b of") < ordered.stdout.index("Step 5 of")
