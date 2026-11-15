"""Regression for the engine fault a73189e271 (#318): at the first examination, `_lock_and_adopt`
reads the test files the builder's commands name (`Engine._command_tests`), and a token the file
system refused raised `OSError` there. Such a token names no test file; the real ones still lock."""

from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

# One name longer than any Linux file system takes (255 bytes).
LONG = "x" * 300


def test_tokens_the_file_system_refuses_name_no_test_file(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    commands = [{"author": "builder", "cwd": ".",
                 "command": f"uv run pytest tests/test_x.py {LONG}_test.py tests/{LONG}/*.py {LONG}/test_*.py"}]
    assert L.engine.Engine._command_tests(tmp_path, commands) == {"tests/test_x.py"}
