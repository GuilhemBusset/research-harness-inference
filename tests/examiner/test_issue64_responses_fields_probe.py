"""Standard client tool fields and history required by issue 64."""

from __future__ import annotations

import pytest

from test_issue64_responses_probe import forward


def test_namespace_keeps_its_client_function_definitions() -> None:
    tool = {
        "type": "namespace",
        "name": "workspace",
        "description": "Workspace tools.",
        "tools": [{"type": "function", "name": "read", "parameters": {"type": "object"}, "strict": False}],
    }
    assert forward(input="Read the notes.", tools=[tool])["tools"] == [tool]


@pytest.mark.parametrize("kind", ["function", "custom"])
def test_standard_client_tool_async_option_is_kept(kind: str) -> None:
    tool = {"type": kind, "name": "run", "async": True}
    assert forward(input="Run the task.", tools=[tool])["tools"] == [tool]


def test_standard_local_shell_history_keeps_its_action() -> None:
    item = {
        "type": "local_shell_call",
        "id": "shell_probe",
        "call_id": "call-1",
        "status": "completed",
        "action": {"type": "exec", "command": ["cat", "notes.txt"], "env": {}},
    }
    assert forward(input=[item])["input"] == [item]
