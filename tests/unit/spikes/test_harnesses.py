"""The harnesses the spikes run (`spikes/harnesses.py`): each is pinned to one exact version, the
job installs exactly the pins and reports each version, and Pi runs on the Node the job installed.
Nothing is installed here: the commands are stubs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import harnesses

VERSIONS = {
    "claude_code": "2.1.323",
    "codex": "0.169.0",
    "opencode": "1.19.5",
    "pi": "1.5.2",
    "openai_sdk": "3.57.0",
    "anthropic_sdk": "2.11.0",
}
PACKAGES = {
    "claude_code": ("@anthropic-ai/claude-code", "claude"),
    "codex": ("@openai/codex", "codex"),
    "opencode": ("opencode-ai", "opencode"),
    "pi": ("@earendil-works/pi-coding-agent", "pi"),
}


def test_the_harnesses_are_pinned(tmp_path: Path) -> None:
    assert len(harnesses.HARNESSES) == 4
    for harness in harnesses.HARNESSES:
        assert harnesses.is_exact(harness.version), harness
    assert {harness.name: (harness.package, harness.command) for harness in harnesses.HARNESSES} == PACKAGES
    for name, (package, command) in PACKAGES.items():
        for written in ("^1.5.2", "~1.5.2", ">=1.5.0", "1.5.x", "1.x", "*", "latest", "next", "1.5", "v1.5.2", ""):
            assert not harnesses.is_exact(written), written
            with pytest.raises(ValueError, match="exact version"):
                harnesses.Harness(name, package, written, command)
    assert harnesses.is_exact("0.170.0-alpha.4")

    # The job installs exactly the pins, and reports each harness's and each SDK's version.
    ran: list[list[str]] = []

    def run(argv: Any) -> str:
        ran.append(list(argv))
        printed = {"claude": "2.1.323 (Claude Code)\n", "codex": "codex-cli 0.169.0\n", "opencode": "1.19.5\n"}
        return (printed | {"pi": "1.5.2\n"}).get(Path(argv[0]).name, "")

    binaries = harnesses.install(tmp_path, run)
    assert ran == [harnesses.install_command(tmp_path)]
    assert ran[0][:2] == ["npm", "install"]
    assert ran[0][-5:] == [
        f"node@{harnesses.NODE_VERSION}",
        "@anthropic-ai/claude-code@2.1.323",
        "@openai/codex@0.169.0",
        "opencode-ai@1.19.5",
        "@earendil-works/pi-coding-agent@1.5.2",
    ]
    assert binaries == {
        name: tmp_path / ("bin" if name == "pi" else "node_modules/.bin") / command
        for name, (_, command) in PACKAGES.items()
    }
    assert harnesses.versions(binaries, run, {"openai": "3.57.0", "anthropic": "2.11.0"}.__getitem__) == VERSIONS
    with pytest.raises(RuntimeError, match="not its pin"):
        harnesses.versions(binaries, lambda argv: "codex-cli 0.170.0", lambda name: "1.0.0")


def test_pi_runs_on_the_pinned_node_not_the_machine_s(tmp_path: Path) -> None:
    # Pi starts on no Node older than 22.19, and the job's machine starts on an older one: what runs
    # `pi` runs Pi's script with the Node the job installed.
    installed = tmp_path / "harnesses" / "node_modules" / ".bin"
    installed.mkdir(parents=True)
    node = installed / "node"
    node.write_text('#!/bin/sh\necho "pinned node: $*"\n', encoding="utf-8")
    node.chmod(0o755)
    (installed / "pi").write_text("#!/usr/bin/env node\n", encoding="utf-8")

    binaries = harnesses.install(tmp_path / "harnesses", lambda argv: "")
    printed = harnesses.run_command([str(binaries["pi"]), "--version"])
    assert printed == f"pinned node: {installed / 'pi'} --version\n"
    assert harnesses.is_exact(harnesses.NODE_VERSION)
    assert int(harnesses.NODE_VERSION.split(".")[0]) >= 22
