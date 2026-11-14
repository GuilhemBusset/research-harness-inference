"""Independent check of the pre-merge digest when temporary storage is unavailable."""

import errno
import hashlib
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

from helpers import SHIP, TempRepo, git
from implement_loop.workspace import Workspace
from test_unwritable_probe import Engine


def test_committed_digest_survives_refused_temporary_storage():
    repo = TempRepo()
    try:
        tree = repo.merger
        data = b"#!/bin/sh\r\nexit 0\n\xff\n"
        (tree / "check.sh").write_bytes(data)
        git("add", "check.sh", cwd=tree)
        git("commit", "-qm", "fixture: committed check", cwd=tree)
        engine = Engine.__new__(Engine)
        engine.ws = Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir)
        refusal = OSError(errno.ENOSPC, "temporary storage unavailable")
        with mock.patch.object(Path, "write_bytes", side_effect=refusal), \
                mock.patch.object(tempfile, "TemporaryDirectory", side_effect=refusal):
            assert engine._digest_at(tree, "HEAD", "check.sh") == hashlib.sha256(data).hexdigest()
            assert engine._digest_at(tree, "HEAD", "missing.sh") is None
    finally:
        repo.cleanup()
