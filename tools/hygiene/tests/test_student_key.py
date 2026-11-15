"""The rule `token` recognises a student's API key (tools/hygiene/scan.py, STUDENT_KEY).

Every key here is generated at run time by the module that defines the format
(services/common/rhi_common/keys.py), so this file holds no key and scans clean itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import scan
from rhi_common import keys

SCAN = Path(scan.__file__).resolve()
REPO = SCAN.parents[2]
MASK = "[redacted token]"


@pytest.fixture
def generated() -> list[str]:
    return [keys.generate_key() for _ in range(25)]


def test_the_scanner_holds_the_format_of_the_key_module() -> None:
    assert scan.STUDENT_KEY == keys.KEY_PATTERN


def test_a_key_is_reported_anywhere_in_a_line(generated: list[str]) -> None:
    for key in generated:
        for line in (f'client = Client(api_key="{key}", timeout=30)', f"{key} is the key", f"export RHI_API_KEY={key}"):
            found = scan.scan_text(f"first line\n{line}\nlast line\n")
            assert [(f.line, f.rule) for f in found] == [(2, "token")]
            assert key not in found[0].render()


def test_a_key_is_reported_in_a_file_name(generated: list[str], tmp_path: Path) -> None:
    for key in generated:
        (tmp_path / f"capture-{key}.json").write_text("{}\n")
    found = scan.scan_paths(["."], root=tmp_path)
    assert [(f.line, f.rule) for f in found] == [(0, "token")] * len(generated)
    assert {f.path for f in found} == {f"capture-{MASK}.json"}


def test_redaction_masks_a_key(generated: list[str]) -> None:
    for key in generated:
        text = f"Authorization: Bearer {key}  # hygiene: allow token (no marker stops the redaction)\n"
        assert scan.redact(text) == text.replace(key, MASK)
    piped = "".join(f"x-api-key: {key}\n" for key in generated)
    result = subprocess.run([sys.executable, str(SCAN), "--redact"], input=piped, capture_output=True, text=True,
                            check=False)
    assert (result.returncode, result.stdout) == (0, f"x-api-key: {MASK}\n" * len(generated))


def test_look_alikes_stay_quiet(generated: list[str]) -> None:
    for key in generated:
        assert scan.scan_text(f"a key one character short: {key[:-1]}\n") == []
        assert scan.scan_text(f"a letter or a digit follows: {key}{key[-1]}\n") == []
    assert scan.scan_text(f"the prefix alone: {keys.PREFIX}\n{keys.PREFIX}\n") == []
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                            capture_output=True, text=True, check=True).stdout
    packages = {part for path in listed.splitlines() for part in Path(path).parts[:-1] if part.startswith(keys.PREFIX)}
    assert {"rhi_common", "rhi_gateway", "rhi_mock_engine"} <= packages
    for package in packages:
        assert scan.scan_text(f"{package}\nfrom {package} import main\nservices/{package}/{package}.py\n") == []
