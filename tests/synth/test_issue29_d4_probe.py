"""The real gate must replace a caller's unusable configuration source."""

import os
import subprocess


def test_synth_overrides_a_bad_caller_source(tmp_path):
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment.update(
        RHI_CONFIG="file",
        RHI_CONFIG_FILE=str(tmp_path / "absent.toml"),
        JSII_RUNTIME_PACKAGE_CACHE="disabled",
    )
    result = subprocess.run(
        ["scripts/check.sh", "synth"], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[check] synth: ok" in result.stdout
