"""The contract check's pytest plugin (`services/engine/contract/rhi_contract_outcomes.py`; system
design, section 4.1), run as `run.sh` runs it: pytest's own output sent nowhere, the descriptor 3
on a file, and the plugin's packages on the path as the release lays them out.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_a_failed_test_s_reason_is_written_only_in_the_suite_s_words(tmp_path: Path) -> None:
    secret = secrets.token_urlsafe(32)
    (tmp_path / "test_rows.py").write_text(
        "def test_inference():\n"
        "    assert False, 'messages-whole no-text max_tokens\\nthe rest of the message'\n"
        "def test_health():\n"
        f"    assert False, 'Bearer {secret}: the engine said Hello there'\n"
        "def test_metrics():\n"
        "    pass\n",
        encoding="utf-8",
    )
    written = tmp_path / "descriptor-3"
    paths = os.pathsep.join(str(REPO / folder) for folder in ("services/engine/contract", "services/common"))
    run = subprocess.run(
        ["/bin/sh", "-c", 'exec 3>"$0" >/dev/null 2>&1; exec "$@"', str(written)]
        + [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "rhi_contract_outcomes", "test_rows.py"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": paths, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 1
    assert written.read_bytes() == (
        b"test_inference failed\ntest_health failed\ntest_metrics passed\n"
        b"test_inference reason messages-whole no-text max_tokens\n"
        b"test_health reason none unexplained none\n"
    )
