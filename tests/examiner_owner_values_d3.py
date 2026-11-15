"""A dry run neither writes the list nor accepts a path in the working tree."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dry_run_has_no_writes_and_refuses_an_inside_list(tmp_path):
    external = tmp_path / "absent" / "values"
    inside = ROOT / "examiner-uncreated-values"
    assert not inside.exists()
    for path in (external, inside):
        done = subprocess.run(
            ["bash", "setup/owner-setup.sh", "--dry-run", "--step", "values"], cwd=ROOT,
            env=os.environ | {"HYGIENE_LOCAL_VALUES": str(path)},
            capture_output=True, text=True, check=False,
        )
        if path == inside:
            assert done.returncode != 0 and "inside the working tree" in done.stderr
        else:
            assert done.returncode == 0, done.stderr
            assert "rhi.owner_config values" in done.stdout and str(path) in done.stdout
        assert not path.exists()
    assert not external.parent.exists()
