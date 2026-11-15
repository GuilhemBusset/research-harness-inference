"""Independent issue 32 synthesis gate probe."""

import os
import subprocess


def test_the_real_synthesis_gate_succeeds():
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment["JSII_RUNTIME_PACKAGE_CACHE"] = "disabled"
    result = subprocess.run(
        ["scripts/check.sh", "synth"], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
