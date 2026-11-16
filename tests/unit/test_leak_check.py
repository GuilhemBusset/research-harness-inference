"""The leak check (scripts/pipeline/leak_check.py): a private value of the fetched configuration is
reported by its file and line, never by itself, and the fetched file is not taken for a leak.

Each test runs the script as the synth step does, from the repository root with the import path
of `pyproject.toml` in PYTHONPATH, on a scratch tree, with a configuration file outside it.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from rhi import config

REPO = config.REPO_ROOT
SCRIPT = REPO / "scripts" / "pipeline" / "leak_check.py"

# The fetched configuration: the example's, with private values of its own, which no file of the
# repository but this one holds.
PLANTED = dataclasses.replace(
    config.load({"RHI_CONFIG": "example"}),
    domain="planted-leak.example.com",
    hosted_zone_id="Z0PLANTED0LEAK0ZONE",
    admin_email="planted-admin@example.com",
    instructor_phone="+15555550199",
    login_client_id="Iv1.0000planted0leak",
    github_repo="planted-public-repository",
)
PRIVATE = {key: getattr(PLANTED, key) for key in sorted(config.PRIVATE_KEYS)}


def run(tree: Path, fetched: Path) -> subprocess.CompletedProcess[str]:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    env = os.environ | {
        "RHI_CONFIG_FILE": str(fetched),
        "PYTHONPATH": ":".join(pyproject["tool"]["pytest"]["ini_options"]["pythonpath"]),
    }
    command = [sys.executable, str(SCRIPT), "--root", str(tree)]
    return subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, check=False)


def shows_no_value(result: subprocess.CompletedProcess[str]) -> bool:
    shown = (result.stdout + result.stderr).casefold()
    return not any(str(value).casefold() in shown for value in PRIVATE.values())


@pytest.fixture
def fetched(tmp_path: Path) -> Path:
    """The fetched configuration, outside the scratch tree."""
    path = tmp_path / "fetched" / "config.toml"
    path.parent.mkdir()
    config.write_file(PLANTED, path)
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A clean scratch tree: a source file and a document."""
    root = tmp_path / "tree"
    (root / "docs").mkdir(parents=True)
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "docs" / "notes.md").write_text("# Notes\n\nNothing here.\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("case", [str, str.upper, str.swapcase], ids=["as written", "upper", "swapped"])
@pytest.mark.parametrize("key", sorted(config.PRIVATE_KEYS))
def test_a_private_value_is_reported_by_file_and_line(tree: Path, fetched: Path, key: str, case: object) -> None:
    assert callable(case)
    (tree / "docs" / "notes.md").write_text(f"# Notes\n\nsee {case(PRIVATE[key])} for more\n", encoding="utf-8")

    result = run(tree, fetched)

    assert result.returncode != 0
    assert result.stdout.splitlines() == [f"docs/notes.md:3: the value of the private key {key!r}"]
    assert shows_no_value(result)


def test_the_value_of_a_key_that_is_not_private_is_not_reported(tree: Path, fetched: Path) -> None:
    public = f"{PLANTED.github_owner}/{PLANTED.github_repo} costs {PLANTED.monthly_budget_usd}\n"
    (tree / "docs" / "notes.md").write_text(public, encoding="utf-8")

    result = run(tree, fetched)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "notes.md" not in result.stdout


def test_the_fetched_file_is_not_a_leak(tree: Path, fetched: Path) -> None:
    # Outside the tree, the file that holds every private value leaves the clean tree passing.
    assert all(str(value) in fetched.read_text(encoding="utf-8") for value in PRIVATE.values())
    outside = run(tree, fetched)
    assert outside.returncode == 0, outside.stdout + outside.stderr

    # Inside it, the check refuses to run, and names the path rather than a value.
    inside = tree / "config.local.toml"
    shutil.copyfile(fetched, inside)
    result = run(tree, inside)
    assert result.returncode != 0
    assert "config.local.toml" in result.stderr
    assert result.stdout == ""
    assert shows_no_value(result)


@pytest.mark.parametrize("key", sorted(config.PRIVATE_KEYS))
def test_a_path_that_holds_a_private_value_is_redacted(tree: Path, fetched: Path, key: str) -> None:
    # A hit in a file whose name holds the value: the file is reported, its name redacted.
    (tree / "docs" / f"{PRIVATE[key].upper()}.md").write_text(f"{PRIVATE[key]}\n", encoding="utf-8")
    hit = run(tree, fetched)
    assert hit.returncode != 0
    assert hit.stdout.splitlines() == [f"docs/<{key}>.md:1: the value of the private key {key!r}"]
    assert shows_no_value(hit)

    # The fetched file inside the tree, under such a name: refused, the path named and redacted.
    (tree / "docs" / f"{PRIVATE[key].upper()}.md").unlink()
    inside = tree / f"{PRIVATE[key].swapcase()}.toml"
    shutil.copyfile(fetched, inside)
    refused = run(tree, inside)
    assert refused.returncode != 0
    assert f"<{key}>.toml" in refused.stderr
    assert shows_no_value(refused)
