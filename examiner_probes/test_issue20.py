"""Independent checks of issue 20 against the checked-out deliverables."""

import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path.cwd()
CONFIG = Path("tools/implementation-loop/implement_loop/config.py")
POLICY = Path("tests/policy/test_codeowners.py")
OWNERS = Path(".github/CODEOWNERS")
RULESET = Path("setup/pr-only-main.json")


def test_owner_and_exact_protected_paths():
    config = runpy.run_path(str(ROOT / CONFIG))
    paths = {*config["PROTECTED_PREFIXES"], *config["PROTECTED_FILES"]}
    remote = subprocess.check_output(
        ["git", "remote", "get-url", "origin"], cwd=ROOT, text=True
    ).strip()
    address = remote.split(":", 1)[1] if remote.startswith("git@github.com:") else urlsplit(remote).path
    owner = "@" + address.strip("/").split("/")[0]
    rows = [
        line.split("#", 1)[0].split()
        for line in (ROOT / OWNERS).read_text().splitlines()
        if line.split("#", 1)[0].strip()
    ]
    assert len(rows) == len(paths)
    assert {row[0] for row in rows} == {"/" + path for path in paths}
    assert all(row[1:] == [owner] for row in rows)


def test_ruleset_changes_only_owner_review_and_remains_valid_json():
    before = subprocess.check_output(
        ["git", "show", "acd9eb356bf7:setup/pr-only-main.json"], cwd=ROOT, text=True
    )
    after = (ROOT / RULESET).read_text()
    assert before.count('"require_code_owner_review": false') == 1
    assert after == before.replace('"require_code_owner_review": false', '"require_code_owner_review": true')
    subprocess.run(
        [sys.executable, "-m", "json.tool", str(ROOT / RULESET)],
        check=True, stdout=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("mutation", ["none", "missing", "extra", "disabled", "expanded_config"])
def test_real_policy_command_accepts_only_compliant_files(tmp_path, mutation):
    for relative in (CONFIG, POLICY, OWNERS, RULESET):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    if mutation == "missing":
        lines = (tmp_path / OWNERS).read_text().splitlines()
        first = next(i for i, line in enumerate(lines) if line.startswith("/"))
        del lines[first]
        (tmp_path / OWNERS).write_text("\n".join(lines) + "\n")
    elif mutation == "extra":
        owner = next(
            line.split()[1] for line in (tmp_path / OWNERS).read_text().splitlines()
            if line.startswith("/")
        )
        with (tmp_path / OWNERS).open("a") as stream:
            stream.write(f"/docs/ {owner}\n")
    elif mutation == "disabled":
        data = json.loads((tmp_path / RULESET).read_text())
        for rule in data["rules"]:
            if rule["type"] == "pull_request":
                rule["parameters"]["require_code_owner_review"] = False
        (tmp_path / RULESET).write_text(json.dumps(data))
    elif mutation == "expanded_config":
        with (tmp_path / CONFIG).open("a") as stream:
            stream.write('\nPROTECTED_PREFIXES += ("examiner-only/",)\n')
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", str(tmp_path / POLICY)],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    expected = 0 if mutation == "none" else 1
    assert result.returncode == expected, result.stdout + result.stderr
    if mutation != "none":
        expected_failure = (
            "test_the_ruleset_requires_a_code_owners_review"
            if mutation == "disabled"
            else "test_codeowners_names_the_owner_on_exactly_the_protected_paths"
        )
        assert expected_failure in result.stdout, result.stdout
