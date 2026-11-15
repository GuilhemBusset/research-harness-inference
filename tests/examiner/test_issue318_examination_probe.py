"""Independent regression checks for filesystem failures during test discovery."""

import errno
from pathlib import Path

from test_deploying import L


def test_refused_filename_keeps_valid_tokens_and_later_commands(tmp_path):
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "before.py").write_text("assert True\n")
    (checks / "after.py").write_text("assert True\n")
    commands = [
        {"author": "builder", "cwd": "checks",
         "command": "pytest before.py::test_one " + "x" * 300 + "_test.py after.py"},
        {"author": "builder", "cwd": "checks", "command": "pytest *.py"},
    ]
    assert L.engine.Engine._command_tests(tmp_path, commands) == {"checks/before.py", "checks/after.py"}


def test_error_while_consuming_glob_keeps_other_test_tokens(tmp_path, monkeypatch):
    (tmp_path / "test_before.py").write_text("assert True\n")
    (tmp_path / "test_after.py").write_text("assert True\n")
    original = Path.glob

    def failing_glob(path, pattern, *args, **kwargs):
        if pattern == "unreadable/*.py":
            raise OSError(errno.EIO, "test discovery read failed")
        yield from original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", failing_glob)
    commands = [{"author": "builder",
                 "command": "pytest test_before.py unreadable/*.py test_after.py"}]
    assert L.engine.Engine._command_tests(tmp_path, commands) == {"test_before.py", "test_after.py"}
