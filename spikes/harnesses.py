"""The harnesses the spikes run, at pinned versions (system design, sections 12, 13 and 17).

The student guide names the harness versions the endpoint was tried with (section 13), so a spike
that runs a harness runs a pinned one and says which. This module holds the pins of the four
harnesses installed from npm, each one's npm package at one exact version, and installs them into
the job: spike S2 (`spikes/s2_client_hold.py`) runs Claude Code and Codex, and spike S3 runs all
four at the versions from here.

**The pins.** Each is the newest release of its package that had been published for at least 7
days when the pin was chosen, the cooling-off section 17 asks before an update is adopted, read
from the npm registry's publication times on the day given:

| Harness | Package | Version | Published (UTC) | Read on |
| --- | --- | --- | --- | --- |
| Claude Code | `@anthropic-ai/claude-code` | 2.1.323 | 2026-11-12 | 2026-11-20 |
| Codex | `@openai/codex` | 0.169.0 | 2026-11-13 | 2026-11-20 |
| OpenCode | `opencode-ai` | 1.19.5 | 2026-10-28 | 2026-11-22 |
| Pi | `@earendil-works/pi-coding-agent` | 1.5.2 | 2026-11-10 | 2026-11-22 |
| Node.js, for Pi | `node` | 24.22.3 | 2026-11-12 | 2026-11-22 |

OpenCode's pin skips the `0.0.0-dev-…` snapshots its project publishes between releases: they are
builds of a branch, not releases. Node's pin is the newest release of its long-term support line
24: Pi needs Node 22.19 or later, and the job's machine starts on an older Node of its image.

A pin is an exact version, `MAJOR.MINOR.PATCH` with an optional pre-release, never a range (`^`,
`~`, `>=`, `1.x`, `*`) or a tag (`latest`, `next`): `Harness` refuses any other, and the module
refuses a Node pin that is not one. A bump is a change of this table and of the constants below,
together.

**Installing.** `install` runs `npm install` of the four pinned packages and of the pinned Node into
a folder of the job, with no lock file of its own and no saved manifest. Claude Code, Codex and OpenCode ship their
binary as an optional dependency for each platform, which npm picks. Codex launches it from its own
script; the commands of Claude Code and OpenCode are placeholders until the package's install script
puts the native binary in place, so the install runs the packages' install scripts: without them
`claude` and `opencode` cannot start. The harnesses are then the folder's
`node_modules/.bin/claude`, `codex` and `opencode`, and `bin/pi`. Pi is a Node program that the
machine's own Node cannot start, so `install` writes `bin/pi`, a launcher that runs Pi's
`node_modules/.bin/pi` with the pinned Node, `node_modules/.bin/node`: whatever runs Pi, the job's
version report and its runs, runs it on that Node.

**Reporting.** `versions` gives the version of each harness, as the installed binary prints it with
`--version`, and of each SDK the SDK scripts call, the OpenAI and Anthropic SDKs of the group
`spikes` in `pyproject.toml`, as the installed distribution says. A harness that reports another
version than its pin stops the job: what the record names must be what ran.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# An exact version: three numbers with no leading zero, and an optional pre-release.
EXACT_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?")
# A version as a binary prints it, within other words (`2.1.323 (Claude Code)`, `codex-cli 0.169.0`).
_PRINTED_VERSION = re.compile(r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)")

# The SDKs the SDK scripts call: their distributions, pinned in the group `spikes`.
SDKS = {"openai_sdk": "openai", "anthropic_sdk": "anthropic"}

type Runner = Callable[[Sequence[str]], str]


def is_exact(version: str) -> bool:
    """Whether `version` is one exact version, not a range or a tag."""
    return EXACT_VERSION.fullmatch(version) is not None


@dataclass(frozen=True)
class Harness:
    """One harness: its name in the spikes' numbers, its npm package, the exact version pinned, and
    the command its package installs."""

    name: str
    package: str
    version: str
    command: str
    # Whether it runs on the pinned Node (`NODE_VERSION`), through the launcher `install` writes.
    on_pinned_node: bool = False

    def __post_init__(self) -> None:
        if not is_exact(self.version):
            raise ValueError(f"the harness {self.name} is not pinned at one exact version")

    @property
    def spec(self) -> str:
        """What npm is asked to install: the package at its pin."""
        return f"{self.package}@{self.version}"

    def binary(self, folder: Path) -> Path:
        """The harness's command once `install` has installed it into `folder`."""
        if self.on_pinned_node:
            return folder / "bin" / self.command
        return folder / "node_modules" / ".bin" / self.command


# The Node that runs the harnesses marked `on_pinned_node`, as npm's package `node` installs it.
NODE_PACKAGE = "node"
NODE_VERSION = "24.22.3"
if not is_exact(NODE_VERSION):
    raise ValueError("the harnesses' Node is not pinned at one exact version")


CLAUDE_CODE = Harness("claude_code", "@anthropic-ai/claude-code", "2.1.323", "claude")
CODEX = Harness("codex", "@openai/codex", "0.169.0", "codex")
OPENCODE = Harness("opencode", "opencode-ai", "1.19.5", "opencode")
PI = Harness("pi", "@earendil-works/pi-coding-agent", "1.5.2", "pi", on_pinned_node=True)
HARNESSES = (CLAUDE_CODE, CODEX, OPENCODE, PI)


def run_command(argv: Sequence[str]) -> str:
    """Runs a command and returns its standard output; a command that fails raises."""
    return subprocess.run(list(argv), check=True, capture_output=True, text=True).stdout


def install_command(folder: Path) -> list[str]:
    """The npm command that installs every pinned harness into `folder`."""
    return [
        "npm",
        "install",
        "--prefix",
        str(folder),
        "--no-save",
        "--no-package-lock",
        "--no-audit",
        "--no-fund",
        f"{NODE_PACKAGE}@{NODE_VERSION}",
        *(harness.spec for harness in HARNESSES),
    ]


def install(folder: Path, run: Runner = run_command) -> dict[str, Path]:
    """Installs the pinned harnesses into `folder`; returns each one's command, by its name."""
    folder.mkdir(parents=True, exist_ok=True)
    run(install_command(folder))
    installed = folder / "node_modules" / ".bin"
    for harness in HARNESSES:
        if harness.on_pinned_node:
            launcher = harness.binary(folder)
            launcher.parent.mkdir(parents=True, exist_ok=True)
            node, script = shlex.quote(str(installed / "node")), shlex.quote(str(installed / harness.command))
            launcher.write_text(f'#!/bin/sh\nexec {node} {script} "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
    return {harness.name: harness.binary(folder) for harness in HARNESSES}


def printed_version(output: str) -> str:
    """The version a binary's `--version` printed. ValueError when it printed none."""
    found = _PRINTED_VERSION.search(output)
    if found is None:
        raise ValueError("the harness printed no version")
    return found.group(1)


def versions(
    binaries: dict[str, Path], run: Runner = run_command, sdk_version: Callable[[str], str] = metadata.version
) -> dict[str, str]:
    """The version of each harness of `binaries` and of each SDK, by name. RuntimeError when a
    harness is not the version pinned."""
    found: dict[str, str] = {}
    for harness in HARNESSES:
        version = printed_version(run([str(binaries[harness.name]), "--version"]))
        if version != harness.version:
            raise RuntimeError(f"the harness {harness.name} reports {version}, not its pin {harness.version}")
        found[harness.name] = version
    for name, distribution in SDKS.items():
        found[name] = sdk_version(distribution)
    return found


def report(found: dict[str, str]) -> str:
    """The versions as the job prints them, one line each."""
    return "\n".join(f"{name}: {version}" for name, version in found.items())
