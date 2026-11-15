"""Run the public synthesis gate from a credential-free caller environment."""

import os
import subprocess
from pathlib import Path


def test_gate_synthesizes_without_aws_or_configuration_variables():
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_", "RHI_CONFIG")) and key != "VIRTUAL_ENV"
    }
    environment["JSII_RUNTIME_PACKAGE_CACHE"] = "disabled"
    assert not Path("cdk.context.json").exists()
    result = subprocess.run(
        ["scripts/check.sh", "synth"], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not Path("cdk.context.json").exists()
