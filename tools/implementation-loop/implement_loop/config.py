"""Caps, ceilings and repo-specific constants (ADR 0012; this repository's values: ADR 0001)."""

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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


# The same scanner once more, immediately before each push, with the owner's list of the values no
# public pattern can name (`tools/hygiene/README.md`, "Values only the owner knows"). That list
# exists on the owner's machine only, outside the sandbox, so these are not check commands of the
# verifier: the engine starts them itself, as the owner (`Engine._local_scan`). Run there, they may
# execute nothing a builder wrote. The scanner and its allow-list are therefore the ones of the
# checkout the engine runs from, named by their absolute paths, never the branch's copy; the branch
# is read as git objects, by commit ID; and the command is an argument vector, with no shell.
# (name, the scanner's arguments): the tree of the head, then every commit the push would publish.
# A finding is handled as a finding of a history check is. The engine keeps the path and the
# rule's name of a finding and nothing else: here the scanner's excerpt surrounds the secret itself.
LOCAL_VALUE_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("local-values", ("--rev", "{head}")),
    ("local-values-history", ("--history", "{base}..{head}")),
)
LOCAL_VALUE_SCANNER = Path(__file__).resolve().parents[2] / "hygiene" / "scan.py"   # <checkout>/tools/hygiene/scan.py
LOCAL_VALUE_ALLOW_LIST = LOCAL_VALUE_SCANNER.with_name("hygiene.toml")
LOCAL_VALUES_ENV = "HYGIENE_LOCAL_VALUES"
LOCAL_VALUES_DEFAULT = ("research-harness-inference", "hygiene-values")   # under the user's configuration directory


def local_values_file(env: Mapping[str, str]) -> Path | None:
    """The owner's list, by the scanner's own rule (`load_local_values` in `tools/hygiene/scan.py`):
    the file `HYGIENE_LOCAL_VALUES` names when it is set, whether or not that file exists (the
    scanner then fails without it, and so the push is withheld), else the default file when it
    exists. None: there is no list here, and nothing is scanned outside the sandbox."""
    if env.get(LOCAL_VALUES_ENV):
        return Path(env[LOCAL_VALUES_ENV])
    try:
        base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    except RuntimeError:   # no home directory to look in
        return None
    path = base.joinpath(*LOCAL_VALUES_DEFAULT)
    return path if path.exists() else None


def local_value_checks(base: str, head: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The scans with the owner's list before a push of `head`, whose commits since `base` the
    push would publish (both commit IDs): (name, argument vector). The interpreter is the engine's
    own, isolated (`-I`: no path from the environment or the working directory). They belong with
    the history checks: where those are off (the engine's test suite, whose repositories hold no
    scanner), these are off too, whatever the machine the suite runs on holds."""
    if not HISTORY_CHECKS:
        return ()
    start = (sys.executable, "-I", str(LOCAL_VALUE_SCANNER), "--config", str(LOCAL_VALUE_ALLOW_LIST))
    return tuple((name, start + tuple(a.replace("{base}", base).replace("{head}", head) for a in arguments))
                 for name, arguments in LOCAL_VALUE_CHECKS)


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
    # From this examination of an issue since its latest plan on, only a finding that names a safety
    # invariant blocks: one that names only an item becomes a follow-up issue, and a withheld
    # certificate no longer holds a head whose items are met (ADR 0001, amendment 11).
    examiner_bound: int = 4
    run_ceiling_factor: float = 1.2
    max_parallel_writers: int = 1     # one issue built at a time: three at once ran the host out of memory (2026-11-15)
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


@dataclass(frozen=True)
class Deploy:
    """What the engine waits for after a merge (ADR 0001, decision 5): the table `[deploy]` of
    loop.toml. With no context nothing is read and nothing waits."""
    contexts: tuple[str, ...] = ()    # commit statuses the pipeline posts on a merge commit (`deploy/<stage>`, ...)
    timeout_s: int = 3600             # polling for a verdict, per run; after it the issue waits for the supervisor
    approval_description: str = ""    # the description of a pending status while the pipeline waits for the owner
    verified_description: str = ""    # the description of a `verify/<stage>` success whose smoke tests ran and passed
    # The smoke test files of which the verification leaves a test out, from the repository's root. None:
    # the table does not say, and a verified deployment then discharges nothing.
    unverified_tests: tuple[str, ...] | None = None


def load_deploy(table) -> Deploy:
    """`[deploy]` as loop.toml gives it. A key the engine does not know is refused (`ValueError`),
    and so is a value of the wrong type (`TypeError`): a misspelt key would leave the engine not
    waiting for a deployment that the owner believes it waits for."""
    if table is None:
        return Deploy()
    if not isinstance(table, dict):
        raise TypeError("loop.toml: [deploy] must be a table")
    known = set(Deploy.__dataclass_fields__)
    unknown = sorted(set(table) - known)
    if unknown:
        raise ValueError(f"loop.toml: [deploy] has no key {', '.join(unknown)} (its keys are {', '.join(sorted(known))})")
    contexts = table.get("contexts", [])
    if not isinstance(contexts, list) or not all(isinstance(c, str) for c in contexts):
        raise TypeError("loop.toml: [deploy] contexts must be a list of commit status contexts")
    if not all(c.strip() for c in contexts):
        raise ValueError("loop.toml: [deploy] contexts cannot hold an empty context")
    timeout_s = table.get("timeout_s", Deploy.timeout_s)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
        raise TypeError("loop.toml: [deploy] timeout_s must be a whole number of seconds")
    if timeout_s < 0:
        raise ValueError("loop.toml: [deploy] timeout_s cannot be negative")
    approval = table.get("approval_description", "")
    if not isinstance(approval, str):
        raise TypeError("loop.toml: [deploy] approval_description must be a string")
    verified = table.get("verified_description", "")
    if not isinstance(verified, str):
        raise TypeError("loop.toml: [deploy] verified_description must be a string")
    unverified = table.get("unverified_tests")
    if unverified is not None and not (isinstance(unverified, list) and all(isinstance(t, str) for t in unverified)):
        raise TypeError("loop.toml: [deploy] unverified_tests must be a list of test files")
    return Deploy(contexts=tuple(contexts), timeout_s=timeout_s, approval_description=approval,
                  verified_description=verified, unverified_tests=None if unverified is None else tuple(unverified))


def load_ci_jobs(host) -> dict[str, str]:
    """`ci_jobs` of the table `[host]` as loop.toml gives it: for a capability the host lacks, the
    name of the CI job that provides it (ADR 0001, amendment 5). The engine reads that job's check
    run on a merged head to discharge an obligation for the capability. No entry: none is read.
    A value that is no job name is refused (`TypeError`)."""
    jobs = (host or {}).get("ci_jobs") or {}
    if not isinstance(jobs, dict) or not all(isinstance(job, str) and job.strip() for job in jobs.values()):
        raise TypeError("loop.toml: [host] ci_jobs must map a capability to the name of a CI job")
    return dict(jobs)


def load_policy(path) -> dict:
    """The host's lacks, the CI jobs that provide some of them, the safety invariants and the
    deployment statuses to wait for, from loop.toml (part of the safety surface)."""
    import tomllib
    data = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
    return {"lacks": list((data.get("host") or {}).get("lacks") or []),
            "ci_jobs": load_ci_jobs(data.get("host")),
            "invariants": list((data.get("invariants") or {}).get("names") or []),
            "deploy": load_deploy(data.get("deploy"))}


def ceiling(size: str, infra: bool) -> int:
    base = CEILINGS.get(size, CEILINGS["M"])
    return int(base * INFRA_FACTOR) if infra else base


def usd_ceiling(size: str, infra: bool) -> float:
    base = USD_CEILINGS.get(size, USD_CEILINGS["M"])
    return base * INFRA_FACTOR if infra else base
