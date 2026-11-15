"""Independent checks for command test discovery after fault a73189e271."""

import errno
from pathlib import Path

import pytest

from test_deploying import L


@pytest.mark.parametrize("operation", ["glob", "resolve", "is_file"])
def test_filesystem_error_does_not_drop_other_test_tokens(tmp_path, monkeypatch, operation):
    tests = tmp_path / "checks"
    tests.mkdir()
    (tests / "first.py").write_text("assert True\n")
    (tests / "last.py").write_text("assert True\n")
    original = getattr(Path, operation)

    def refuse_one_path(path, *args, **kwargs):
        if path.name == "refused_test.py" or (operation == "glob" and args == ("refused_*.py",)):
            raise OSError(errno.ENAMETOOLONG, "unusable command token")
        return original(path, *args, **kwargs)

    bad = "refused_*.py" if operation == "glob" else "refused_test.py"
    monkeypatch.setattr(Path, operation, refuse_one_path)
    commands = [{"author": "builder", "cwd": "checks",
                 "command": f"pytest first.py::test_one {bad} last.py"}]
    assert L.engine.Engine._command_tests(tmp_path, commands) == {"checks/first.py", "checks/last.py"}


def test_real_overlong_token_and_valid_glob(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_one.py").write_text("assert True\n")
    (tests / "test_two.py").write_text("assert True\n")
    token = "z" * 300 + "_test.py"
    commands = [{"author": "builder", "command": f"pytest {token} tests/test_*.py"}]
    assert L.engine.Engine._command_tests(tmp_path, commands) == {"tests/test_one.py", "tests/test_two.py"}
