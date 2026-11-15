"""Independent host command, ECR ordering, and failure checks."""

from __future__ import annotations

import base64
import subprocess
from typing import Any

import pytest

from rhi_lifecycle import host

REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
IMAGE = REGISTRY + "/engine@sha256:" + "7" * 64
PASSWORD = "example-only-password"
TOKEN = base64.b64encode(("AWS:" + PASSWORD).encode()).decode()


class Commands:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.events: list[str] = []
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def get_authorization_token(self) -> dict[str, Any]:
        self.events.append("ecr")
        return {"authorizationData": [{"authorizationToken": TOKEN}]}

    def run(self, arguments: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((arguments, kwargs))
        command = arguments[arguments.index("--") + 1 :]
        self.events.append(command[1])
        return subprocess.CompletedProcess(arguments, 19 if command[1] == self.fail else 0)


@pytest.mark.parametrize("action", ["pull", "start", "restart", "stop"])
def test_host_actions_and_login_stdin(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    commands = Commands()
    monkeypatch.setattr(host.subprocess, "run", commands.run)
    machine = host.Host(commands, nsenter="example-nsenter")
    if action == "pull":
        machine.pull(IMAGE)
        assert commands.events == ["ecr", "login", "pull"]
        arguments, kwargs = commands.calls[0]
        assert arguments[arguments.index("--") + 1 :] == (
            "docker", "login", "--username", "AWS", "--password-stdin", REGISTRY
        )
        assert kwargs["input"] == PASSWORD.encode()
    else:
        getattr(machine, action + "_engine")()
        assert commands.events == [action]
    expected = ("docker", "pull", IMAGE) if action == "pull" else ("systemctl", action, "rhi-engine")
    arguments, _ = commands.calls[-1]
    assert arguments[arguments.index("--") + 1 :] == expected
    for arguments, _ in commands.calls:
        options = arguments[1 : arguments.index("--")]
        assert arguments[0] == "example-nsenter"
        assert options[options.index("--target") + 1] == "1"
        assert {"--mount", "--uts", "--ipc", "--net", "--pid"} <= set(options)
        assert all(PASSWORD not in argument and TOKEN not in argument for argument in arguments)


@pytest.mark.parametrize("action", ["login", "pull", "start", "restart", "stop"])
def test_every_command_failure_reaches_the_caller(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    commands = Commands(fail=action)
    monkeypatch.setattr(host.subprocess, "run", commands.run)
    machine = host.Host(commands)
    with pytest.raises(host.HostError) as raised:
        if action in {"login", "pull"}:
            machine.pull(IMAGE)
        else:
            getattr(machine, action + "_engine")()
    assert raised.value.action == action
    assert raised.value.returncode == 19
    assert PASSWORD not in str(raised.value) and TOKEN not in str(raised.value)
    if action == "login":
        assert commands.events == ["ecr", "login"]
