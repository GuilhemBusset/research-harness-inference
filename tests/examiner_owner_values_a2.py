"""The real script rejects list paths in the checkout, including symlinks."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_list_is_never_written_in_the_repository(tmp_path):
    tree = tmp_path / "checkout"
    (tree / "setup").mkdir(parents=True)
    shutil.copy2(ROOT / "setup/owner-setup.sh", tree / "setup/owner-setup.sh")
    target = tmp_path / "external-values"
    target.write_text("retained placeholder\n")
    (tree / "link-values").symlink_to(target)
    alias = tmp_path / "alias"
    alias.symlink_to(tree, target_is_directory=True)
    for path in (tree / "values", "values", tree / "link-values", alias / "values"):
        done = subprocess.run(
            ["bash", "setup/owner-setup.sh", "--step", "values"], cwd=tree,
            env=os.environ | {"HYGIENE_LOCAL_VALUES": str(path)}, input="\ny\n",
            capture_output=True, text=True, check=False,
        )
        assert done.returncode != 0 and "inside the working tree" in done.stderr
        assert "rhi.owner_config" not in done.stdout
    assert not (tree / "values").exists()
    assert target.read_text() == "retained placeholder\n"
