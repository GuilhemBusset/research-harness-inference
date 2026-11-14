"""Caps, ceilings and repo-specific constants (ADR 0012; this repository's values: ADR 0001)."""

from dataclasses import dataclass, field

# Files many issues tend to touch. Only one in-flight issue may own any of them at a time.
SHARED_FILES = frozenset({
    "README.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".gitignore",
    "pyproject.toml", "uv.lock", "cdk.json",
})
SHARED_PREFIXES = (".github/workflows/", ".githooks/")

# Files that can change how tests run without touching a test file.
TEST_CONFIG_NAMES = frozenset({"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"})

# Paths a builder changes only with the examiner's approval (a `protected_change` request),
# unless the issue's own text names them: the loop's machinery, the repository's gates, the
# invariants written as tests (ADR 0001, decision 10), and the agents' own configuration: a hook
# or a setting added under `.claude/`, `.agents/` or `.codex/` would run later in the owner's own
# session, outside the sandbox (amendment 6). A merge to `main` is deployment rights in the account,
# so the pipeline stack and the bootstrap policies are protected too (amendment 1). They are not on
# the safety surface below.
AGENT_CONFIG_PREFIXES = (".claude/", ".agents/", ".codex/")
PROTECTED_PREFIXES = ("tools/implementation-loop/", "tools/hygiene/", ".github/", ".githooks/", "tests/policy/",
                      *AGENT_CONFIG_PREFIXES, "src/rhi/pipeline/", "setup/bootstrap/")
PROTECTED_FILES = frozenset({"scripts/check.sh", "scripts/check-loop-guard.sh", "scripts/implement.sh",
                             "scripts/ship.sh", "setup/pr-only-main.json"})

# The safety surface: what a fault-repair run may never change, whatever any agent approves.
# A fault whose fix needs it stays filed for the owner (ADR 0012; ADR 0001, decision 10).
SAFETY_SURFACE_FILES = frozenset({
    "tools/implementation-loop/implement_loop/verify.py",
    "tools/implementation-loop/implement_loop/loop_pytest.py",
    "tools/implementation-loop/implement_loop/gate.py",
    "tools/implementation-loop/implement_loop/live.py",
    "tools/implementation-loop/implement_loop/sandbox.py",
    "tools/implementation-loop/implement_loop/config.py",
    "tools/implementation-loop/implement_loop/supervisor.py",
    "tools/implementation-loop/loop.toml",
    "scripts/check.sh",
    "scripts/check-loop-guard.sh",
})
SAFETY_SURFACE_PREFIXES = ("tools/hygiene/", ".github/", ".githooks/", *AGENT_CONFIG_PREFIXES, "setup/pr-only-main.json")


# Checks the verifier runs on every head it verifies, before the issue's own commands, from the
# root of that tree: (name, command). The public-repository scan is the gate that stops a leak
# before the examiner sees it and before anything is pushed (ADR 0001, decision 4).
STANDING_CHECKS: tuple[tuple[str, str], ...] = (("hygiene", "python3 tools/hygiene/scan.py"),)


def standing_checks() -> tuple[tuple[str, str], ...]:
    return STANDING_CHECKS


# Checks of the commits a branch adds to the base branch, run from the root of the tree at its
# head: (name, command), where `{range}` becomes `<base commit>..<head commit>`. A tree scan cannot
# see a value that was committed and removed again, nor a commit's message or its author's
# address; it is published with the branch all the same, and CI's `history` job refuses it. The
# engine runs these on every head it verifies and again before every push. Exit status 1 means
# findings: no later commit can take them back, so the branch is abandoned (`Engine._history_refused`).
HISTORY_CHECKS: tuple[tuple[str, str], ...] = (("hygiene-history", "python3 tools/hygiene/scan.py --history {range}"),)
HISTORY_FINDINGS_EXIT = 1


def history_checks(revs: str) -> tuple[tuple[str, str], ...]:
    """The history checks for the commits of `revs` (`<base>..<head>`, both commit IDs)."""
    return tuple((name, command.replace("{range}", revs)) for name, command in HISTORY_CHECKS)


def is_protected(path: str) -> bool:
    return path in PROTECTED_FILES or path.startswith(PROTECTED_PREFIXES)


def is_safety_surface(path: str) -> bool:
    return path in SAFETY_SURFACE_FILES or path.startswith(SAFETY_SURFACE_PREFIXES)


# Per-issue ceilings on answered agent calls, by size label; infra issues get x1.5.
CEILINGS = {"S": 20, "M": 30, "L": 45}
INFRA_FACTOR = 1.5
# Per-issue ceilings on the dollars the vendors report (Claude's calls; Codex reports tokens, so
# its side is bounded by the call ceiling only), by size label; infra issues get x1.5 as well.
# Provisional until the first epic's cost is read from agents.jsonl (ADR 0001, decision 8).
USD_CEILINGS = {"S": 40.0, "M": 70.0, "L": 120.0}
INFRA_PREFIXES = ("tools/", "scripts/", ".github/", "setup/")


@dataclass(frozen=True)
class Caps:
    infra_budget: int = 10            # attempts that timed out or crashed, per issue
    unproductive_replan: int = 2      # consecutive unproductive rounds before a replan
    unproductive_fail: int = 4        # ... before the issue fails (two more after the replan)
    replans: int = 1
    fix_forwards: int = 1             # then a revert PR
    reverts: int = 1
    merge_refusals: int = 3
    run_ceiling_factor: float = 1.2
    max_parallel_writers: int = 3
    max_parallel_agents: int = 6
    check_timeout_s: int = 900
    max_check_timeout_s: int = 1800
    ci_timeout_s: int = 3600
    ci_poll_s: int = 20
    queue_poll_s: int = 30            # polling a merge queue or issue claim another run holds
    queue_wait_s: int = 6 * 3600      # after that, the issue waits for the supervisor instead


@dataclass(frozen=True)
class Config:
    repo: str
    caps: Caps = field(default_factory=Caps)
    base_branch: str = "main"
    remote: str = "origin"
    required_check: str = "required"
    api_version: str = "2022-11-28"
    label_prefix: str = "loop:"


def load_policy(path) -> dict:
    """The host's lacks and the safety invariants from loop.toml (part of the safety surface)."""
    import tomllib
    from pathlib import Path
    data = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
    return {"lacks": list((data.get("host") or {}).get("lacks") or []),
            "invariants": list((data.get("invariants") or {}).get("names") or [])}


def ceiling(size: str, infra: bool) -> int:
    base = CEILINGS.get(size, CEILINGS["M"])
    return int(base * INFRA_FACTOR) if infra else base


def usd_ceiling(size: str, infra: bool) -> float:
    base = USD_CEILINGS.get(size, USD_CEILINGS["M"])
    return base * INFRA_FACTOR if infra else base
