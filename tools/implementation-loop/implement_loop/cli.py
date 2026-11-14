"""Command line: `implement doctor | plan | run | status | stop | retry | supervise | record-fixture`."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from . import VERSION, environment, report, sandbox
from .config import Config, load_policy
from .github import GhCli, GitHub, RecordingGitHub, RedactingGitHub
from .graph import CycleError, build_plan
from .state import RunStore


def repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        sys.exit("implement: run this inside the repository")
    return Path(out.stdout.strip())


def repo_slug() -> str:
    out = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        sys.exit("implement: `gh repo view` failed; is the GitHub CLI authenticated (gh auth status)?")
    return out.stdout.strip()


def github_client(slug: str) -> GitHub:
    """The client of every command here, and so of every run the supervisor launches: whatever it
    publishes on GitHub is masked first by the hygiene scanner of the checkout the engine runs
    from (`github.RedactingGitHub`). It forwards every other GitHub method."""
    return cast(GitHub, RedactingGitHub(GhCli(slug)))


def cmd_plan(args) -> int:
    root = repo_root()
    try:
        plan = build_plan(github_client(args.repo or repo_slug()), args.issue, root)
    except CycleError as e:
        print(f"implement: {e}. Fix the blocked-by links before running.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"root": plan.root, "mode": plan.mode, "work": plan.work, "waves": plan.waves,
                          "deps": {n: sorted(d) for n, d in plan.deps.items()}, "external": sorted(plan.external),
                          "closed_work": plan.closed_work, "warnings": plan.warnings}, indent=1))
    else:
        print(report.render_plan(plan))
    return 0


def cmd_status(args) -> int:
    store = RunStore.for_root(repo_root(), args.issue)
    issues = store.known()
    if not issues:
        print(f"No run recorded for #{args.issue}.")
        return 0
    for n in issues:
        st = store.issue(n)
        extra = ""
        if st.wait:
            extra = f" · {st.wait.get('kind')}: {st.wait.get('detail', '')}"
        if st.fail:
            extra = f" · {st.fail.get('cause')}: {st.fail.get('detail', '')}"
        owner = f" · obligations: {len(st.obligations)}" if st.obligations else ""
        opened = sum(1 for f in st.findings if f.get("status") == "open")
        b = store.budget(n)
        cost = f" · ${b['usd']:.2f}" if b.get("usd") else ""
        print(f"#{n:<5} {st.phase.value:<10} calls {b['calls']}{cost} · failed attempts {b['infra']} · open findings {opened}"
              + (f" · PR #{st.pr}" if st.pr else "") + owner + extra)
    calls = store.dir / "agents.jsonl"
    if calls.exists():
        records = [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]
        cost = sum(r.get("cost_usd") or 0 for r in records)
        tokens = sum(((r.get("usage") or {}).get("input_tokens") or 0) + ((r.get("usage") or {}).get("output_tokens") or 0)
                     for r in records if r.get("vendor") == "codex")
        print(f"\nAgent calls: {len(records)} · Claude cost ${cost:.2f} · Codex tokens {tokens:,}")
    running = (store.dir / "lock").exists()
    print(f"Run directory: {store.dir}" + (" (a run is active)" if running else ""))
    return 0


def cmd_stop(args) -> int:
    store = RunStore.for_root(repo_root(), args.issue)
    (store.dir / "STOP").write_text("stop requested\n")
    print(f"Stop requested. The run for #{args.issue} finishes its current steps, saves, and exits.")
    return 0


def cmd_retry(args) -> int:
    """Give failed issues a fresh ceiling, and waiting ones an immediate resume, on the next run."""
    from .model import Phase
    store = RunStore.for_root(repo_root(), args.issue)
    if (store.dir / "lock").exists():
        sys.exit(f"implement: the run for #{args.issue} is active; retry after it finishes")
    targets = args.items or [n for n in store.known() if store.issue(n).phase in (Phase.FAILED, Phase.WAITING)]
    for n in targets:
        st = store.issue(n)
        if st.phase == Phase.FAILED:
            b = store.budget(n)
            store.change_budget(n, bonus=max(b["calls"] - b["bonus"], 0), set_infra=0, set_usd_bonus=b.get("usd", 0))
            st.unproductive = st.replans = 0
            st.signature = ""
            print(f"#{n} gets a fresh ceiling on the next run (it failed: {(st.fail or {}).get('cause')})")
            st.fail = None
            st.phase = Phase.BUILD if st.plan and st.worktree else Phase.PENDING
        elif st.phase == Phase.WAITING:
            print(f"#{n} resumes on the next run (it was waiting: {(st.wait or {}).get('kind')})")
            st.phase = Phase((st.wait or {}).get("resume") or "pending")
            st.wait = None
        else:
            print(f"#{n} is {st.phase.value}; nothing to do")
            continue
        store.put(st)
    return 0


def cmd_record(args) -> int:
    rec = RecordingGitHub(github_client(args.repo or repo_slug()))
    build_plan(cast(GitHub, rec), args.issue, repo_root())   # it forwards every GitHub method
    rec.save(Path(args.out))
    print(f"Recorded {sum(len(v) for v in rec.fixture.values())} responses to {args.out}")
    return 0


def detect_operator() -> str | None:
    """Claude Code sets CLAUDECODE=1 for the commands it runs. Codex has no stable marker (and the
    Codex plugin for Claude Code sets CODEX_* variables), so Codex runs pass --operator codex."""
    return "claude" if os.environ.get("CLAUDECODE") == "1" else None


def _out(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def doctor_checks(root: Path, slug: str | None) -> list[tuple[str, str, str]]:
    """(status, name, detail) with status PASS, WARN or FAIL."""
    checks = []

    def add(ok, name, detail, warn_only=False):
        checks.append(("PASS" if ok else ("WARN" if warn_only else "FAIL"), name, detail))

    add(sys.version_info >= (3, 11), "python", sys.version.split()[0])
    code, out = _out(["git", "--version"])
    add(code == 0, "git", out)
    code, out = _out(["gh", "auth", "status"])
    add(code == 0, "gh auth", "authenticated" if code == 0 else out[-200:])
    add(bool(slug), "repository", slug or repository_problem(root))
    if slug:
        add(*base_branch_check(root, Config(repo=slug)))
    add(*identity_check(root))
    add(*git_config_check(root))
    add(*sandbox_check(root))
    for cli, version_cmd in (("claude", ["claude", "--version"]), ("codex", ["codex", "--version"])):
        found = shutil.which(cli)
        code, out = _out(version_cmd) if found else (1, "not found on PATH")
        add(code == 0, cli, out.splitlines()[0] if out else "")
    code, out = _out(["codex", "login", "status"])
    add(code == 0, "codex login", out.splitlines()[0] if out else "")
    if slug:
        code, out = _out(["gh", "api", f"repos/{slug}/rulesets", "--jq", ".[].id"])
        required = False
        for rid in out.split() if code == 0 else []:
            c2, o2 = _out(["gh", "api", f"repos/{slug}/rulesets/{rid}"])
            required |= c2 == 0 and '"context":"required"' in o2.replace(" ", "")
        add(required, "required check", "the ruleset requires `required`" if required else
            "the ruleset does not require `required`; run setup/apply-ruleset.sh", warn_only=True)
    # What the checks need on this host. A failure parks only the deliverables that depend on it
    # (ADR 0011), so it warns instead of failing.
    for p in environment.probe(root):
        add(p.ok, p.name, p.detail if p.ok else f"{p.detail} ({p.kind} checks will park until this is fixed)",
            warn_only=True)
    add(*node_check(), warn_only=True)
    return checks


# The Node.js majors the CDK CLI supports; `cdk synth` is a check of every infrastructure issue.
CDK_NODE_MAJORS = (22, 24)


def node_check() -> tuple[bool, str, str]:
    """(ok, name, detail): whether `node` is one of the majors the CDK CLI supports. A warning,
    never a failure: only the checks that synthesize depend on it."""
    code, out = _out(["node", "--version"]) if shutil.which("node") else (1, "not found on PATH")
    wanted = " or ".join(str(m) for m in CDK_NODE_MAJORS)
    version = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
    try:
        major = int(version.lstrip("v").split(".")[0])
    except ValueError:
        return False, "node", (f"{version or out[-120:] or 'node --version failed'}: the CDK CLI needs Node.js {wanted} "
                               "(`cdk synth` checks will fail until it is installed)")
    if major in CDK_NODE_MAJORS:
        return True, "node", version
    return False, "node", f"{version}: the CDK CLI supports Node.js {wanted} (`cdk synth` checks may fail or warn)"


# Where the hygiene scanner lives in the repository the engine works on (config.STANDING_CHECKS).
HYGIENE_SCANNER = ("tools", "hygiene", "scan.py")


def identity_check(root: Path) -> tuple[bool, str, str]:
    """(ok, name, detail): whether the identity git would commit with here passes the hygiene
    scanner. The engine commits as that identity (the repository's `user.name` and `user.email`,
    or what the environment sets in their place), a commit publishes its author's and its
    committer's name and address for good, and the history scan reads both on every commit of a
    pull request: with an address the scanner does not allow, no branch of the loop could land.
    The scanner itself judges (`scan.py --redact`, with the allow-list of its `hygiene.toml`):
    nothing of that list is repeated here. The detail never shows an address it refuses."""
    scanner = root.joinpath(*HYGIENE_SCANNER)
    if not scanner.is_file():
        return False, "commit identity", (f"{'/'.join(HYGIENE_SCANNER)} is missing: nothing can say whether the commit "
                                          "identity may be published")
    shown = []
    for role, var in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        code, out = _out(["git", "-C", str(root), "var", var])
        ident = re.sub(r"\s+\d+\s+[+-]\d{4}$", "", out.strip()) if code == 0 else ""
        if not ident:
            return False, "commit identity", (f"git has no {role} identity here: set user.name and user.email in this "
                                              "repository (docs/factory/green-light.md)")
        try:
            proc = subprocess.run([sys.executable, str(scanner), "--redact"], input=ident + "\n", capture_output=True,
                                  text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, "commit identity", f"the hygiene scanner could not be asked: {e}"
        if proc.returncode != 0:
            return False, "commit identity", f"the hygiene scanner could not be asked: {proc.stderr.strip()[-200:]}"
        if proc.stdout.strip() != ident:
            return False, "commit identity", (
                f"the {role} git would commit as here is not one the hygiene scanner allows "
                f"({'/'.join(HYGIENE_SCANNER[:-1])}/hygiene.toml): every commit of the loop would publish it, and the "
                "`history` check would refuse every pull request. Set this repository's user.email to your GitHub "
                "no-reply address (docs/factory/green-light.md)")
        if ident not in shown:
            shown.append(ident)
    return True, "commit identity", "; ".join(shown)


def git_config_check(root: Path) -> tuple[bool, str, str]:
    """(ok, name, detail): the repository's own git configuration holds no credential. Agents
    read it: the sandbox shows them the repository's git directory, or a copy of its
    configuration in the private one of their call. A remote URL that carries a password or a
    token, or an extra header git sends with every request, would be a GitHub credential in
    their reach; the login belongs in a credential helper, which the sandbox hides."""
    code, out = _out(["git", "-C", str(root), "config", "--local", "--null", "--list"])
    if code != 0:
        return False, "git config", f"could not read the repository's git configuration: {out[-200:]}"
    held = []
    for entry in out.split("\0"):
        key, _, value = entry.partition("\n")
        if key and ("extraheader" in key.lower() or sandbox.SECRET_VALUE.search(value)
                    or re.search(r"\bhttps?://[^/\s@]+@", value)):   # a token in the user's place
            held.append(key)
    if held:
        return False, "git config", (f"the repository's git configuration carries a credential ({', '.join(sorted(held))}): "
                                     "agents can read it inside the sandbox. Keep the login in a credential helper "
                                     "(`gh auth setup-git`) and the URL bare (docs/factory/isolation.md)")
    return True, "git config", "no credential in the repository's configuration"


def sandbox_check(root: Path) -> tuple[bool, str, str]:
    """(ok, name, detail): bubblewrap is installed, and a canary run inside the sandbox could
    read no credential (`~/.aws`, the `gh` login, `~/.ssh`, nor through any other mount of them),
    saw no cloud or GitHub variable, could write only to its worktree, and could not bring a
    hidden directory back. Always the real sandbox, whatever launcher is installed."""
    version = sandbox.version()
    if version is None:
        return False, "sandbox", ("bubblewrap is not installed (`bwrap` is not on PATH): agents and checks do not run "
                                  "without it (docs/factory/isolation.md)")
    report = sandbox.canary(runs_dir(root) / "canary", root)
    return report.ok, "sandbox", f"{version}; {sandbox.describe(report)}"


def preflight(root: Path, slug: str) -> None:
    """Refuse to go on while the doctor fails: before a run, and before a supervision, which
    would otherwise launch a run that refuses, wait, and launch it again until its bounds."""
    failed = [c for c in doctor_checks(root, slug) if c[0] == "FAIL"]
    if failed:
        for status, name, detail in failed:
            print(f"{status} {name}: {detail}", file=sys.stderr)
        sys.exit("implement: fix the failed checks above (scripts/implement.sh doctor) before running")


def repository_problem(root: Path, remote: str = "origin") -> str:
    """Why the doctor found no GitHub repository. Without a remote the loop cannot run at all
    (ADR 0001, decision 11), so that case is named instead of blaming the GitHub CLI."""
    code, _ = _out(["git", "-C", str(root), "remote", "get-url", remote])
    if code != 0:
        return (f"no `{remote}` remote: the loop needs this repository on GitHub "
                "(docs/factory/green-light.md)")
    return "gh repo view failed"


def base_branch_check(root: Path, cfg: Config) -> tuple[bool, str, str]:
    """(ok, name, detail): whether the remote has the base branch. A repository nothing was pushed
    to has none, and a run could only fail on it later, at its first fetch."""
    ref = f"{cfg.remote}/{cfg.base_branch}"
    code, out = _out(["git", "-C", str(root), "ls-remote", "--exit-code", "--heads", cfg.remote, cfg.base_branch])
    if code == 0:
        return True, "base branch", ref
    if code == 2:   # git's exit status for "no matching ref"
        return False, "base branch", (f"{cfg.remote} has no `{cfg.base_branch}` branch: nothing is pushed yet "
                                      "(docs/factory/green-light.md)")
    return False, "base branch", f"could not read {ref}: {out[-200:]}"


def prepare_environment(root: Path, log=print, cache_dir: Path | None = None) -> dict:
    """Heal what the checked-in recipes can, then describe the environment for the engine: a
    fingerprint (a waiting issue resumes when it changes; whether the sandbox can be built is part
    of it) and what check commands and agents need in their environment: the run's own package
    caches under `cache_dir`, which the sandbox binds read-write. The owner's caches are never
    shared with agents: what an agent leaves in a cache would otherwise run outside the sandbox."""
    for action in environment.heal(root):
        log(f"environment: {action}")
    probes = environment.probe(root)
    for p in probes:
        if not p.ok:
            log(f"environment: {p.name} still failing ({p.kind} checks will park): {p.detail}")
    bwrap = sandbox.version()
    probes = [*probes, environment.Probe("sandbox", bwrap is not None, bwrap or "bubblewrap is not installed", "isolation")]
    check_env = {}
    if cache_dir is not None:
        check_env = {"UV_CACHE_DIR": str(cache_dir / "uv"), "npm_config_cache": str(cache_dir / "npm"),
                     "UV_LINK_MODE": "copy"}   # a cache and a worktree are separate mounts: no hard links
        sandbox.cache_dirs(check_env)
    return {"fingerprint": environment.fingerprint(probes), "check_env": check_env}


def canary_report(root: Path) -> tuple[bool, str]:
    """(passed, text): everything the sandbox canary tried and saw on this host, profile by
    profile (a check command, a Claude call, a Codex call), as the probe reported it from inside:
    what it could read of the credential paths, which planted variables reached it, where it
    could write, what each attempt to bring a hidden directory back returned. The home directory
    is written `~`."""
    report = sandbox.canary(runs_dir(root) / "canary", root)
    lines = [f"sandbox canary: {'PASS' if report.ok else 'FAIL'}", *[f"  problem: {p}" for p in report.problems],
             *[f"  {fact}" for fact in report.facts],
             ("What the probe reported from inside, per profile. `wrote` lists where a write succeeded in there: on the "
              "empty stand-ins of the hidden directories. A marker that reached the real directory is a problem above."),
             json.dumps(report.raw, indent=1, sort_keys=True)]
    return report.ok, "\n".join(lines).replace(str(Path.home()), "~")


def cmd_doctor(args) -> int:
    root = repo_root()
    if getattr(args, "canary", False):
        ok, text = canary_report(root)
        print(text)
        return 0 if ok else 1
    for action in environment.heal(root):
        print(f"FIXED {action}")
    code, slug = _out(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    checks = doctor_checks(root, args.repo or (slug if code == 0 else None))
    for status, name, detail in checks:
        print(f"{status:<5} {name:<15} {detail}")
    failed = [c for c in checks if c[0] == "FAIL"]
    if args.smoke and not failed:
        from .live import LiveAgents
        from .workspace import Workspace
        scratch = runs_dir(root) / "smoke"
        agents = LiveAgents("claude", scratch, Workspace(root, 0, root / "scripts" / "ship.sh"))
        for vendor in environment.VENDORS:
            try:
                environment.smoke_call(agents, vendor, root)
                print(f"PASS  smoke {vendor:<9} a structured call worked")
            except Exception as e:  # noqa: BLE001 - report any failure of the live call
                print(f"FAIL  smoke {vendor:<9} {e}")
                failed.append(("FAIL", vendor, str(e)))
    return 1 if failed else 0


def runs_dir(root: Path) -> Path:
    from .state import runs_root
    return runs_root(root)


def cmd_run(args) -> int:
    from .engine import Engine
    from .live import LiveAgents
    from .workspace import Workspace

    invoked_at = time.time()   # a stop requested after this moment is honoured, an older one is stale
    root = repo_root()
    operator = args.operator or detect_operator()
    if operator not in ("claude", "codex"):
        sys.exit("implement: say who is driving this run with --operator claude or --operator codex")
    slug = args.repo or repo_slug()
    preflight(root, slug)
    # From here on every git process of this run (its own commands, ship.sh) ignores the hooks and
    # the monitor a worktree or an agent's git directory names: they would run outside the sandbox.
    os.environ.update(sandbox.engine_git_env(os.environ, runs_dir(root) / "no-hooks"))
    gh = github_client(slug)
    try:
        plan = build_plan(gh, args.issue, root)
    except CycleError as e:
        sys.exit(f"implement: {e}. Fix the blocked-by links before running.")
    print(report.render_plan(plan))
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("implement: confirm with --yes once the user has agreed that this run merges its own PRs")
        answer = input("\nThis run merges each PR into main itself once every gate passes. Proceed? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return 1
    store = RunStore.for_root(root, args.issue, fresh=args.fresh)
    log_file = store.dir / "engine.log"

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        with log_file.open("a") as f:
            f.write(line + "\n")

    ws = Workspace(root, args.issue, root / "scripts" / "ship.sh")
    env = prepare_environment(root, log, cache_dir=ws.dir / "cache")
    agents = LiveAgents(operator, store.dir, ws, extra_env=env["check_env"])
    log(f"run for #{args.issue}: operator {operator}, reviewer {agents.other}, {len(plan.work)} work item(s)")
    policy = load_policy(root / "tools" / "implementation-loop" / "loop.toml")
    # what the checks lack is judged on the environment they get: no vendor's API key reaches them
    policy["lacks"] = environment.host_lacks(policy["lacks"], env={k: v for k, v in os.environ.items()
                                                                  if not sandbox.dropped(k)})
    policy["repair"] = bool(getattr(args, "repair", False))
    engine = Engine(plan, store, gh, ws, agents, Config(repo=slug), repo_root=root, log=log, environment=env,
                    invoked_at=invoked_at, policy=policy)
    import signal as _signal

    def _terminate(signum, frame):   # the supervisor's deadline: stop agents, then let the run save and exit
        (store.dir / "STOP").write_text("terminated\n")
        agents.terminate_all()
    _signal.signal(_signal.SIGTERM, _terminate)
    summary = engine.run()
    log("finished: " + json.dumps(summary, default=str))
    return 0 if not (summary["waiting"] or summary["failed"]) else 3


def cmd_supervise(args) -> int:
    """Run the loop for a root until every issue is terminal, within the supervisor's bounds."""
    from .supervisor import Supervisor
    root = repo_root()
    operator = args.operator or detect_operator()
    if operator not in ("claude", "codex"):
        sys.exit("implement: say who is driving this run with --operator claude or --operator codex")
    if not args.yes:
        sys.exit("implement: confirm with --yes once the user has agreed that these runs merge their own PRs")
    preflight(root, args.repo or repo_slug())   # every run it would launch does the same and would refuse
    if args.detach:
        log = RunStore.for_root(root, args.issue).dir / "supervisor.log"
        cmd = [sys.executable, str(root / "tools" / "implementation-loop" / "implement.py"), "supervise", str(args.issue),
               "--operator", operator, "--yes", "--max-runs", str(args.max_runs), "--max-hours", str(args.max_hours)]
        with open(log, "a") as out:
            proc = subprocess.Popen(cmd, cwd=root, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        print(f"Supervising #{args.issue} in the background (pid {proc.pid}); log: {log}")
        return 0
    outcome = Supervisor(root, args.issue, operator, max_runs=args.max_runs, max_hours=args.max_hours).supervise()
    print(json.dumps({"finished": outcome.finished, "reason": outcome.reason, "runs": outcome.runs,
                      "repairs": outcome.repairs, "waiting": outcome.waiting, "failed": outcome.failed}, indent=1))
    return 0 if outcome.finished and not outcome.failed else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="implement", description="Implement an epic or an issue, with evidence.")
    parser.add_argument("--version", action="version", version=f"implement-loop {VERSION}")
    parser.add_argument("--repo", help="owner/name (default: the current repo)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="check the toolchain and the sandbox a run needs")
    p.add_argument("--smoke", action="store_true", help="also make one tiny structured call to each CLI (costs cents)")
    p.add_argument("--canary", action="store_true", help="only run the sandbox canary and print everything it saw")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("plan", help="show what a run would do (read-only)")
    p.add_argument("issue", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("status", help="show a run's progress")
    p.add_argument("issue", type=int)
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("stop", help="ask a running run to stop at the next safe point")
    p.add_argument("issue", type=int)
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("run", help="run the loop on an epic or issue (resumes an existing run)")
    p.add_argument("issue", type=int)
    p.add_argument("--operator", choices=["claude", "codex"], help="the agent driving this run (the other one reviews)")
    p.add_argument("--yes", action="store_true", help="the user confirmed that this run merges its own PRs")
    p.add_argument("--fresh", action="store_true", help="archive the previous run state and start over")
    p.add_argument("--repair", action="store_true", help="a fault-repair run: the safety surface is off limits")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("retry", help="give failed issues a fresh ceiling and resume waiting ones, on the next run")
    p.add_argument("issue", type=int, help="the run's root issue")
    p.add_argument("items", type=int, nargs="*", help="issues to retry (default: every failed or waiting one)")
    p.set_defaults(func=cmd_retry)
    p = sub.add_parser("supervise", help="run the loop for a root until every issue is terminal (bounded)")
    p.add_argument("issue", type=int)
    p.add_argument("--operator", choices=["claude", "codex"])
    p.add_argument("--yes", action="store_true", help="the user confirmed that these runs merge their own PRs")
    p.add_argument("--max-runs", type=int, default=10)
    p.add_argument("--max-hours", type=float, default=24)
    p.add_argument("--detach", action="store_true", help="keep supervising after this terminal or session closes")
    p.set_defaults(func=cmd_supervise)
    p = sub.add_parser("record-fixture", help="record GitHub reads for offline tests")
    p.add_argument("issue", type=int)
    p.add_argument("out")
    p.set_defaults(func=cmd_record)
    args = parser.parse_args(argv)
    return args.func(args)
