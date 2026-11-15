"""Independent quota outputs from the actual walkthrough."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("extra", "expected"), [([], ["24"]), (["--upgrade-pool"], ["120"]), (["--xl-pool"], ["24", "192"])])
def test_quotas(extra, expected):
    done = subprocess.run(
        ["bash", "setup/owner-setup.sh", "--dry-run", "--step", "quotas", "--max-instances", "2", *extra],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert re.findall(r"--desired-value (\d+)", done.stdout) == expected
