"""The harnesses the spikes run, at pinned versions (system design, sections 12, 13 and 17).

The student guide names the harness versions the endpoint was tried with (section 13), so a spike
that runs a harness runs a pinned one and says which. This module holds the pins of the two
harnesses, each one's npm package at one exact version, and installs them into the job: spike S2
(`spikes/s2_client_hold.py`) runs them, and spike S3 runs the same versions from here.

**The pins.** Each is the newest release of its package that had been published for at least 7
days when the pin was chosen, the cooling-off section 17 asks before an update is adopted. Read
from the npm registry's publication times on 2026-11-20:

| Harness | Package | Version | Published (UTC) |
| --- | --- | --- | --- |
| Claude Code | `@anthropic-ai/claude-code` | 2.1.323 | 2026-11-12 |
| Codex | `@openai/codex` | 0.169.0 | 2026-11-13 |

A pin is an exact version, `MAJOR.MINOR.PATCH` with an optional pre-release, never a range (`^`,
`~`, `>=`, `1.x`, `*`) or a tag (`latest`, `next`): `Harness` refuses any other. A bump is a change
of this table and of the constants below, together.

**Installing.** `install` runs `npm install` of the two pinned packages into a folder of the job,
with no lock file of its own and no saved manifest. Both packages ship their binary as an optional
dependency for each platform, which npm picks. Codex launches it from its own script; Claude Code's
command is a placeholder until the package's install script copies the native binary over it, so
the install runs the packages' install scripts: without them `claude` cannot start. The harnesses
are then the folder's `node_modules/.bin/claude` and `node_modules/.bin/codex`.

**Reporting.** `versions` gives the version of each harness, as the installed binary prints it with
`--version`, and of each SDK the SDK scripts call, the OpenAI and Anthropic SDKs of the group
`spikes` in `pyproject.toml`, as the installed distribution says. A harness that reports another
version than its pin stops the job: what the record names must be what ran.
"""

from __future__ import annotations

import re
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

    def __post_init__(self) -> None:
        if not is_exact(self.version):
            raise ValueError(f"the harness {self.name} is not pinned at one exact version")

    @property
    def spec(self) -> str:
        """What npm is asked to install: the package at its pin."""
        return f"{self.package}@{self.version}"

    def binary(self, folder: Path) -> Path:
        """The harness's command once `install` has installed it into `folder`."""
        return folder / "node_modules" / ".bin" / self.command


CLAUDE_CODE = Harness("claude_code", "@anthropic-ai/claude-code", "2.1.323", "claude")
CODEX = Harness("codex", "@openai/codex", "0.169.0", "codex")
HARNESSES = (CLAUDE_CODE, CODEX)


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
        *(harness.spec for harness in HARNESSES),
    ]


def install(folder: Path, run: Runner = run_command) -> dict[str, Path]:
    """Installs the pinned harnesses into `folder`; returns each one's command, by its name."""
    folder.mkdir(parents=True, exist_ok=True)
    run(install_command(folder))
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
