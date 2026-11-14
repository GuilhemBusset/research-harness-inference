"""The host environment a run depends on: probe it, and heal it from checked-in recipes.

ADR 0011, decision 5. `probe` checks what a run will actually use: here, the pinned Python test
runner through uv. `heal` repairs what a checked-in recipe can, re-probing after each repair;
this repository has no recipe yet, so it only says what it cannot install. It never runs sudo.

Nothing here touches shell profiles or global configuration.

Two more questions about the host, which no recipe can fix: `host_lacks` lists the capabilities
it lacks (configured ones, plus the credentials and platform facts it can verify), and
`vendor_reachable` makes the doctor's smoke call (`smoke_call`) to one vendor's CLI, to tell
whether an outage is over.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess]

# The pytest this repository pins (pyproject.toml, scripts/check.sh), run through uv: the host may
# have no `python` on PATH and no pytest in its system Python.
PYTEST_PIN = "pytest==8.3.5"
PYTEST = ["uv", "run", "--no-project", "--with", PYTEST_PIN, "python", "-m", "pytest"]


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str
    kind: str  # "python-tests": the deliverables a failure blocks


def _run(runner: Runner, cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str]:
    """(exit code, stdout then stderr). A missing command or a timeout is a failure, not an error."""
    try:
        proc = runner(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd[:3])} timed out after {timeout}s"
    except OSError as e:
        return 126, f"{cmd[0]}: {e}"
    return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()


def _tail(out: str, lines: int = 3) -> str:
    return " | ".join(out.splitlines()[-lines:])


def _probe_pytest(root: Path, runner: Runner) -> Probe:
    code, out = _run(runner, [*PYTEST, "--version"], root, 600)
    version = PYTEST_PIN.replace("==", " ")
    if code == 0 and version in out:
        return Probe("pytest", True, f"{version} via uv run --no-project", "python-tests")
    return Probe("pytest", False, _tail(out) or f"{' '.join(PYTEST)} --version failed", "python-tests")


def probe(root: Path, runner: Runner = subprocess.run) -> list[Probe]:
    """The pinned test runner."""
    return [_probe_pytest(root, runner)]


def _by_name(probes: list[Probe]) -> dict[str, Probe]:
    return {p.name: p for p in probes}


def heal(root: Path, runner: Runner = subprocess.run) -> list[str]:
    """Repair what the checked-in recipes can, re-probing after each repair, and say what was
    done. Each repair runs at most once per call. Never sudo."""
    actions: list[str] = []
    probes = _by_name(probe(root, runner))
    if not probes["pytest"].ok and "uv not found" in probes["pytest"].detail:
        actions.append("no recipe installs uv; Python test checks stay unavailable")
    return actions


def fingerprint(probes: list[Probe]) -> str:
    """A short hash of which probes pass, independent of their order and details, so the engine
    can tell whether the environment changed since a failure."""
    text = "\n".join(sorted(f"{p.kind}/{p.name}={'pass' if p.ok else 'fail'}" for p in probes))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- what the host lacks

VERIFIED_CREDENTIALS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
PROC_VERSION = Path("/proc/version")
_WSL = re.compile(r"microsoft|wsl", re.IGNORECASE)


def host_lacks(configured: list[str], env: Mapping[str, str] | None = None, platform_text: str | None = None) -> list[str]:
    """The capabilities this host lacks, sorted and each once: the `configured` ones (declared
    for the host in `loop.toml`, such as "aws:account", "aws:deployed-environment" or
    "gpu:capacity"), plus the facts checked here:
    "credentials:<VAR>" for each API key variable absent or empty in `env` (default: this
    process's environment), and "platform:native-linux" when `platform_text` (default: the
    contents of /proc/version, empty if unreadable) names Microsoft or WSL."""
    env = os.environ if env is None else env
    if platform_text is None:
        try:
            platform_text = PROC_VERSION.read_text()
        except OSError:
            platform_text = ""
    lacks = {c.strip() for c in configured if c.strip()}
    lacks.update(f"credentials:{var}" for var in VERIFIED_CREDENTIALS if not (env.get(var) or "").strip())
    if _WSL.search(platform_text):
        lacks.add("platform:native-linux")
    return sorted(lacks)


# ---------------------------------------------------------------- is a vendor back?

VENDORS = ("claude", "codex")
SMOKE_ROLE = "explore"
SMOKE_PROMPT = 'Do not use any tool. Return {"notes": "ok"}.'
REPO_ROOT = Path(__file__).resolve().parents[3]


def smoke_call(agents, vendor: str, cwd: Path) -> dict:
    """The doctor's smoke call (`implement doctor --smoke`): one tiny structured answer from
    `vendor`, through the live adapter exactly as a run asks for one. Raises when it fails."""
    return agents.call(vendor, SMOKE_ROLE, cwd, SMOKE_PROMPT)


class _NotInstalled(Exception):
    """The vendor's CLI is not on PATH."""


def _runner_agents(runner: Runner, run_dir: Path, timeout_s: int):
    """Live agents whose processes go through `runner`, one attempt per call, each process
    limited to `timeout_s`, each inside the sandbox like any agent call; and the exception a
    failed call raises."""
    from .live import AgentFailure, LiveAgents, agent_env

    class RunnerAgents(LiveAgents):
        def _exec(self, cmd: list[str], cwd: Path, timeout: int, prompt: str | None = None, vendor: str | None = None,
                  mounts=None) -> tuple[int | None, str, str]:
            cmd = self._sandboxed(cmd, cwd, vendor, mounts)
            try:
                proc = runner(cmd, cwd=cwd, env=agent_env(self.no_gh_dir, self.extra_env, vendor), input=prompt,
                              capture_output=True, text=True, timeout=timeout)
            except FileNotFoundError:
                raise _NotInstalled(cmd[0]) from None
            except subprocess.TimeoutExpired:
                return None, "", f"{cmd[0]} timed out after {timeout}s"
            except OSError as e:
                return 126, "", f"{cmd[0]}: {e}"
            return proc.returncode, proc.stdout or "", proc.stderr or ""

        def _spawn(self, cmd, prompt, cwd, timeout, vendor=None, mounts=None):
            return self._exec(cmd, cwd, timeout, prompt, vendor, mounts)

        def _codex_mcp_off(self, cwd):
            # LiveAgents._codex_mcp_off through the runner: disable every MCP server, fail closed
            code, out, _ = self._exec([self.bins["codex"], "mcp", "list", "--json"], cwd, min(60, timeout_s),
                                      vendor="codex")
            try:
                servers = json.loads(out[out.index("["):]) if code == 0 else None
            except ValueError:
                servers = None
            if not isinstance(servers, list):
                raise AgentFailure("could not list Codex MCP servers to disable them; refusing to run Codex")
            names = sorted(s["name"] for s in servers if isinstance(s, dict) and s.get("name"))
            return [arg for name in names for arg in ("-c", f"mcp_servers.{name}.enabled=false")]

    agents = RunnerAgents("claude", run_dir, None, retries=0)
    agents.roles[SMOKE_ROLE].timeout_s = timeout_s
    return agents, AgentFailure


def vendor_reachable(vendor: str, runner: Runner = subprocess.run, timeout_s: int = 120) -> bool | None:
    """Whether `vendor` ("claude" or "codex") answers the doctor's smoke call right now: True or
    False, or None when its CLI is not installed. One attempt, no retries, each process limited
    to `timeout_s`, in the repository root, inside the sandbox; for a supervisor deciding whether
    an outage is over. A host where the sandbox cannot be built reaches nobody: nothing is run
    there. It is a real call: it costs cents."""
    from .sandbox import SandboxUnavailable
    if vendor not in VENDORS:
        raise ValueError(f"unknown vendor {vendor!r}: expected one of {', '.join(VENDORS)}")
    with tempfile.TemporaryDirectory(prefix="implement-reachable-") as scratch:
        agents, failure = _runner_agents(runner, Path(scratch), timeout_s)
        try:
            smoke_call(agents, vendor, REPO_ROOT)
        except _NotInstalled:
            return None
        except (failure, SandboxUnavailable):
            return False
    return True
