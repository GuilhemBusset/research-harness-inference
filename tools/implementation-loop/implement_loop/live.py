"""Live agents: one fresh headless Claude Code (`claude -p`) or Codex (`codex exec`) process per
call, with per-role permissions, schema-validated answers, retries, and a full call record.

Read-only roles (explore, plan, triage, arbitrate) get read-only tools (Claude) or the read-only
sandbox (Codex). Working roles (build, resolve_locked, examine, decide) may edit their worktree,
use the network and run anything the checks run, with the same access for both vendors (Codex
runs without its own sandbox, like Claude Code; ADR 0012): a limit of a vendor's sandbox is an
engine problem, not an owner obligation. The boundary is the engine's: every process started here
runs inside `sandbox` (ADR 0001, decision 3), where the home directory is empty but for the
tools and the running vendor's own login, with a scrubbed environment and a private git directory
per call. The examiner and the request decider work in a disposable copy of the head that is
deleted afterwards. Every prompt and answer is kept under the run directory, and every attempt
and its cost is reported to the engine's meter.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import signal
import subprocess
import threading
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from . import sandbox, schemas
from .agents import (
    AgentFailure,
    AgentStopped,
    BuildResult,
    Decision,
    ExamResult,
    IssueRequest,
    ItemPlan,
    Objection,
    PlanResult,
    Probe,
    Request,
    Ruling,
)
from .config import is_protected, is_safety_surface
from .model import CheckSpec, Finding

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "loop.toml"
DIFF_LIMIT = 60_000
# What a vendor CLI prints when it could not reach its service: an outage, not the agent's fault.
OUTAGE = re.compile(r"rate.?limit|\b429\b|overloaded|\b52[09]\b|\b50[234]\b|service unavailable|internal server error"
                    r"|connection (refused|reset|error)|could not resolve|getaddrinfo|ECONNRESET|ETIMEDOUT|ENOTFOUND"
                    r"|network (is )?unreachable|stream disconnected|temporarily unavailable|api error: 5\d\d",
                    re.IGNORECASE)


def classify_failure(code: int | None, stderr: str) -> str:
    """outage | timeout | crash, for an attempt that produced no usable answer."""
    if OUTAGE.search(stderr or ""):
        return "outage"
    return "timeout" if code is None else "crash"


# How long a model that refused a call (its usage limit reached, or rate limited) is passed over
# for the role's fallback model before it is asked again.
REFUSED_RETRY_S = 1800


def claude_refused(data: dict) -> bool:
    """Whether a Claude result says the model refused the call before any work: the account's
    usage limit for that model is reached, or the API rate limited it. The CLI exits 1 with an
    empty stderr and says so only in its JSON result, and `--fallback-model` does not cover it."""
    return bool(data.get("is_error")) and (data.get("api_error") == "usage_limit_reached"
                                           or data.get("api_error_type") == "rate_limit_error"
                                           or data.get("api_error_status") == 429)


# What `codex exec` says when the model a call asked for refused it: the account's usage limit or
# quota for that model is reached, the API rate limits it, or the account may not use that model.
# Codex reports it in an `error` or a `turn.failed` event of its JSON stream, or on stderr, and exits
# with an error; the answer file stays empty.
CODEX_REFUSED = re.compile(r"usage.?limit|usage_not_included|quota.?exceeded|insufficient.?quota|rate.?limit|\b429\b"
                           r"|too many requests|model_not_found|\bmodel\b[^\n]{0,80}\b(?:is not supported|does not exist"
                           r"|not found|is not available|not enabled)|do(?:es)? not have access to (?:the |this )?model",
                           re.IGNORECASE)


def codex_refused(errors: list[str], stderr: str) -> bool:
    """Whether a Codex call that gave no answer was refused by its model (`CODEX_REFUSED`), from
    the messages of the call's `error` and `turn.failed` events and from its stderr. What the
    agent's own commands printed is not read: it is in other events."""
    return any(CODEX_REFUSED.search(text or "") for text in [*errors, stderr])


@dataclass
class RoleSettings:
    effort: str = "high"
    timeout_s: int = 1800
    claude_model: str | None = None
    codex_model: str | None = None
    max_budget_usd: float | None = None   # Claude only: `--max-budget-usd` for one call (ADR 0001, decision 8)
    claude_fallback_model: str | None = None   # used while `claude_model` refuses calls (`claude_refused`)
    # Codex: the model of a call the adapter escalates (one that touches a protected path or settles
    # a dispute: `LiveAgents.examine`, `decide`, `arbitrate`), and the model a Codex call asks for
    # while the one it asked for refuses calls (`codex_refused`).
    codex_escalated_model: str | None = None
    codex_fallback_model: str | None = None


DEFAULT_ROLES = {
    "explore": RoleSettings(effort="low", timeout_s=900, claude_model="sonnet", max_budget_usd=2),
    "plan": RoleSettings(timeout_s=1800, max_budget_usd=8),
    "triage": RoleSettings(timeout_s=1200),
    "build": RoleSettings(timeout_s=5400, max_budget_usd=25),
    "resolve_locked": RoleSettings(timeout_s=1800),
    "examine": RoleSettings(timeout_s=3600),
    "decide": RoleSettings(timeout_s=1800),
    "arbitrate": RoleSettings(timeout_s=1200),
}
READ_ONLY = {"explore", "plan", "triage", "arbitrate"}
CLAUDE_READ_TOOLS = "Read,Glob,Grep"
CLAUDE_WRITE_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
# Convenience only: Claude Code matches these as command prefixes, so `git add x && git commit`
# slips past. The real boundaries are the sandbox (no credentials to find, in files or in the
# environment) and the engine, which absorbs any commit an agent makes and verifies the exact
# head it merges.
CLAUDE_DENY = ["Bash(git commit *)", "Bash(git push *)", "Bash(git reset *)", "Bash(git rebase *)",
               "Bash(git checkout *)", "Bash(git switch *)", "Bash(git merge *)", "Bash(gh *)",
               "Bash(aws *)", "Bash(cdk deploy*)"]
CREDENTIAL_VARS = sandbox.GITHUB_VARS
# What a call that stopped at its `--max-budget-usd` reports instead of an answer.
OVER_BUDGET = "error_max_budget_usd"


def agent_env(no_gh_dir: Path, extra: dict[str, str] | None = None, vendor: str | None = None) -> dict[str, str]:
    """The environment an agent of `vendor` runs in (`sandbox.clean_env`, the same scrub the
    verifier's commands get): no cloud or GitHub credentials, so it cannot deploy, push, call the
    GitHub API, or touch issues and pull requests, whatever its own permission settings allow.
    `extra` is what the engine's own checks get too, so an agent runs the checks exactly as
    the verifier will."""
    return sandbox.clean_env(dict(os.environ) | dict(extra or {}), no_gh_dir, vendor)


def load_roles(path: Path = CONFIG_FILE) -> dict[str, RoleSettings]:
    roles = {k: RoleSettings(**asdict(v)) for k, v in DEFAULT_ROLES.items()}
    if path.exists():
        for role, values in tomllib.loads(path.read_text()).get("roles", {}).items():
            if role in roles:
                for key, value in values.items():
                    if hasattr(roles[role], key):
                        setattr(roles[role], key, value or None if key.endswith("_model") else value)
    return roles


def render(name: str, **values) -> str:
    text = (PROMPTS / f"{name}.md").read_text()
    if name != "explore":
        text = (PROMPTS / "_common.md").read_text() + "\n---\n\n" + text
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


class LiveAgents:
    def __init__(self, operator: str, run_dir: Path, workspace, roles: dict[str, RoleSettings] | None = None,
                 claude_bin: str = "claude", codex_bin: str = "codex", retries: int = 2,
                 backoff_s: tuple[float, ...] = (30, 90), sleep=time.sleep, extra_env: dict[str, str] | None = None,
                 clock=time.monotonic):
        if operator not in ("claude", "codex"):
            raise ValueError("operator must be 'claude' or 'codex'")
        self.operator = operator
        self.other = "codex" if operator == "claude" else "claude"
        self.ws = workspace
        self.roles = roles or load_roles()
        self.bins = {"claude": claude_bin, "codex": codex_bin}
        self.retries, self.backoff, self.sleep = retries, backoff_s, sleep
        self.clock = clock
        self._refused: dict[tuple[str, str], float] = {}   # (vendor, model) -> when it last refused a call
        self.no_gh_dir = run_dir / "no-gh"
        self.extra_env = dict(extra_env or {})
        self.calls_dir = run_dir / "calls"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = run_dir / "agents.jsonl"
        self._seq = itertools.count(len(list(self.calls_dir.glob("*.prompt.md"))) + 1)
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen] = set()
        self._stopped = False
        self.meter = None   # set by the engine: told before and after every attempt

    # ---------------------------------------------------------------- process control
    def _write_pids(self) -> None:
        """The process groups of running agents, for a supervisor that must reap them if this
        engine is killed (agents run in their own sessions)."""
        try:
            (self.calls_dir.parent / "agents.pids").write_text("\n".join(str(p.pid) for p in self._active))
        except OSError:
            pass

    def terminate_all(self) -> None:
        """Stop every running agent (its whole process group)."""
        with self._lock:
            self._stopped = True
            procs = list(self._active)
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _mounts(self, vendor: str, role: str, cwd: Path, out_file: Path | None = None) -> sandbox.Mounts:
        """What a call of `role` may see beyond the tools: its worktree (read-only for a
        read-only role) and that worktree's git; for a working role the run's package caches;
        for Codex the file its schema is read from and the one its answer is written to."""
        write = role not in READ_ONLY
        mounts = sandbox.worktree_mounts(cwd, write, self.ws)
        if write:
            mounts = mounts.plus(writable=sandbox.cache_dirs(self.extra_env))
        if vendor == "codex" and out_file is not None:
            out_file.touch()
            mounts = mounts.plus(readable=[out_file.with_suffix(".schema.json")], writable=[out_file])
        return mounts

    def _sandboxed(self, cmd: list[str], cwd: Path, vendor: str | None, mounts: sandbox.Mounts | None) -> list[str]:
        """`cmd` as it is started: inside the sandbox, always. Raises `SandboxUnavailable` when it
        cannot be built; no agent runs without it."""
        return sandbox.current().command(cmd, cwd, vendor=vendor,
                                         mounts=mounts if mounts is not None else sandbox.Mounts(readable=(Path(cwd),)))

    def _spawn(self, cmd: list[str], prompt: str, cwd: Path, timeout: int, vendor: str | None = None,
               mounts: sandbox.Mounts | None = None) -> tuple[int | None, str, str]:
        if self._stopped:
            raise AgentStopped("run stopped")
        cmd = self._sandboxed(cmd, cwd, vendor, mounts)
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True,
                                env=agent_env(self.no_gh_dir, self.extra_env, vendor))
        with self._lock:
            self._active.add(proc)
            self._write_pids()
            stopped_meanwhile = self._stopped
        if stopped_meanwhile:   # a stop that arrived between the check above and the registration
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            code = None
        finally:
            with self._lock:
                self._active.discard(proc)
                self._write_pids()
        if self._stopped:
            raise AgentStopped("run stopped")
        return code, out or "", err or ""

    def _codex_mcp_off(self, cwd: Path) -> list[str]:
        """`-c` overrides disabling every MCP server Codex would load in `cwd`: inherited MCP tools
        are not bound by the sandbox. An empty-table override does not clear servers defined in
        config files (tables merge), so each one is disabled by name. Listed in the exact directory
        of each call, right before it, because a project `.codex/config.toml` in a worktree (which an
        agent could even write) adds servers. Fails closed if they can't be listed."""
        cmd = self._sandboxed([self.bins["codex"], "mcp", "list", "--json"], cwd, "codex",
                              sandbox.worktree_mounts(cwd, False, self.ws))
        try:
            proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                                  env=agent_env(self.no_gh_dir, self.extra_env, "codex"), check=False)
            servers = json.loads(proc.stdout[proc.stdout.index("["):]) if proc.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            servers = None
        if not isinstance(servers, list):
            raise AgentFailure("could not list Codex MCP servers to disable them; refusing to run Codex")
        names = sorted(s["name"] for s in servers if isinstance(s, dict) and s.get("name"))
        return [arg for name in names for arg in ("-c", f"mcp_servers.{name}.enabled=false")]

    # ---------------------------------------------------------------- one call
    def _own_model(self, vendor: str, role: str, escalate: bool = False) -> str | None:
        """The model a call of `role` is for: the role's Claude model, or its Codex model, or for an
        escalated Codex call its `codex_escalated_model` when it has one. None: the CLI's default."""
        s = self.roles[role]
        if vendor == "claude":
            return s.claude_model
        return (s.codex_escalated_model if escalate else None) or s.codex_model

    def _fallback_model(self, vendor: str, role: str) -> str | None:
        s = self.roles[role]
        return s.claude_fallback_model if vendor == "claude" else s.codex_fallback_model

    def _model(self, vendor: str, role: str, escalate: bool = False) -> str | None:
        """The model a call asks for: its own (`_own_model`), or the role's fallback while its own
        refused a call less than `REFUSED_RETRY_S` ago."""
        own, fallback = self._own_model(vendor, role, escalate), self._fallback_model(vendor, role)
        refused_at = self._refused.get((vendor, own or ""))
        if fallback and refused_at is not None and self.clock() - refused_at < REFUSED_RETRY_S:
            return fallback
        return own

    def _command(self, vendor: str, role: str, cwd: Path, schema: dict, out_file: Path,
                 model: str | None = None) -> list[str]:
        s = self.roles[role]
        write = role not in READ_ONLY
        if vendor == "claude":
            tools = CLAUDE_WRITE_TOOLS if write else CLAUDE_READ_TOOLS + (",WebFetch,WebSearch" if role == "explore" else "")
            cmd = [self.bins["claude"], "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                   "--no-session-persistence", "--strict-mcp-config", "--effort", s.effort,
                   "--tools", tools, "--allowedTools", tools,
                   "--permission-mode", "acceptEdits" if write else "dontAsk"]
            if write:
                cmd += ["--disallowedTools", *CLAUDE_DENY]
            model = model or s.claude_model
            if model:
                cmd += ["--model", model]
            if s.max_budget_usd:
                cmd += ["--max-budget-usd", f"{s.max_budget_usd:g}"]
            return cmd
        schema_file = out_file.with_suffix(".schema.json")
        schema_file.write_text(json.dumps(schema))
        cmd = [self.bins["codex"], "exec", "-C", str(cwd), "--json", "--ephemeral",
               "--output-schema", str(schema_file), "-o", str(out_file),
               "-c", 'approval_policy="never"', "-c", f'model_reasoning_effort="{s.effort}"',
               *self._codex_mcp_off(cwd),
               "--sandbox", "danger-full-access" if write else "read-only"]
        model = model or s.codex_model
        if model:
            cmd += ["-m", model]
        return cmd + ["-"]

    @staticmethod
    def _parse(vendor: str, stdout: str, out_file: Path) -> tuple[dict | None, dict]:
        meta: dict = {}
        if vendor == "claude":
            try:
                data = json.loads(stdout)
            except ValueError:
                return None, {"parse_error": stdout[-300:]}
            if not isinstance(data, dict):
                return None, {"parse_error": stdout[-300:]}
            meta = {"cost_usd": data.get("total_cost_usd"), "usage": data.get("usage"), "is_error": data.get("is_error"),
                    "denials": len(data.get("permission_denials") or [])}
            if data.get("subtype") == OVER_BUDGET:
                meta["over_budget"] = True
            if claude_refused(data):
                meta["refused"] = True
                return None, meta
            value = data.get("structured_output")
            if value is None and isinstance(data.get("result"), str):
                try:
                    value = json.loads(data["result"])
                except ValueError:
                    value = None
            return value, meta
        errors = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed" and "usage" in event:
                meta["usage"] = event["usage"]
            elif event.get("type") == "error":
                errors.append(str(event.get("message") or "")[:300])
            elif event.get("type") == "turn.failed":
                error = event.get("error")
                errors.append(str((error.get("message") if isinstance(error, dict) else error) or "")[:300])
        if errors:
            meta["errors"] = errors[-3:]
        try:
            return json.loads(out_file.read_text()), meta
        except (OSError, ValueError):
            return None, meta

    def call(self, vendor: str, role: str, cwd: Path, prompt: str, issue: int | None = None,
             escalate: bool = False) -> dict:
        """One structured answer. Every attempt is metered: an answer (valid or not) is charged
        to the issue's ceiling, a timeout or crash to its infrastructure budget; an outage to
        neither; and what the attempt cost, when the vendor reports it, to the issue's dollar
        ceiling. After the retries, `AgentFailure` says which kind of failure it was. A call that
        stopped at its own dollar budget is not tried again. `SandboxUnavailable` is raised, and
        nothing is run, when the sandbox cannot be built.

        A model that refuses the call (`claude_refused`, `codex_refused`: its usage limit is
        reached, or it is rate limited) did no work: the attempt is an outage, charged to nothing.
        When the role has a fallback model for that vendor (`claude_fallback_model`,
        `codex_fallback_model`), the same attempt is made again at once on that model, and the role
        keeps using it for `REFUSED_RETRY_S` before it asks its own model again.

        `escalate`: a Codex call runs on the role's `codex_escalated_model` (ADR 0001, amendment
        13), with the same fallback."""
        schema = schemas.BY_ROLE[role]
        s = self.roles[role]
        own, fallback = self._own_model(vendor, role, escalate), self._fallback_model(vendor, role)
        last_error, kind = "", "crash"
        attempt = 0
        while attempt <= self.retries:
            model = self._model(vendor, role, escalate)
            if self.meter:
                self.meter.before(issue, role, vendor)
            seq = next(self._seq)
            stem = self.calls_dir / (f"{seq:04d}-{role}-{vendor}" + (f"-{issue}" if issue else ""))
            out_file = stem.with_suffix(".out.json")
            full_prompt = prompt + (f"\n\nYour previous answer was rejected: {last_error}. Answer again, matching the schema."
                                    if last_error and "schema" in last_error else "")
            stem.with_suffix(".prompt.md").write_text(full_prompt)
            started = time.monotonic()
            try:
                cmd = self._command(vendor, role, cwd, schema, out_file, model)
                code, out, err = self._spawn(cmd, full_prompt, cwd, s.timeout_s, vendor,
                                             self._mounts(vendor, role, cwd, out_file))
            except AgentFailure as e:   # refused before it ran: charged like a crash, so it cannot spin
                if self.meter:
                    self.meter.after(issue, role, vendor, e.kind)
                raise
            except sandbox.SandboxUnavailable:   # nothing ran: charged to nothing, the issue waits for the host
                if self.meter:
                    self.meter.after(issue, role, vendor, "unavailable")
                raise
            value, meta = self._parse(vendor, out, out_file)
            if vendor == "claude":
                out_file.write_text(out)
            elif code is not None and (code != 0 or value is None) and codex_refused(meta.get("errors") or [], err):
                meta["refused"], value = True, None
            problems = schemas.validate(value, schema) if value is not None else ["no structured answer"]
            refused = bool(meta.get("refused"))
            outcome = "outage" if refused else "answered" if code == 0 else classify_failure(code, err)
            record = {"seq": seq, "role": role, "vendor": vendor, "issue": issue, "attempt": attempt + 1,
                      "exit": code, "seconds": round(time.monotonic() - started, 1),
                      "ok": code == 0 and not refused and not problems, "outcome": outcome, "problems": problems[:5],
                      "stderr_tail": err[-400:], **({"model": model} if model else {}),
                      **({"escalated": True} if escalate and vendor == "codex" else {}), **meta}
            with self._lock, self.log_path.open("a") as f:
                f.write(json.dumps(record, default=str) + "\n")
            spent = getattr(self.meter, "spent", None)
            if spent and isinstance(meta.get("cost_usd"), (int, float)):
                spent(issue, role, vendor, float(meta["cost_usd"]))
            if self.meter:
                self.meter.after(issue, role, vendor, outcome)
            if code == 0 and not refused and value is not None and not problems:
                return value
            if meta.get("over_budget"):   # the same call would stop at the same place: no retry
                raise AgentFailure(f"{vendor} {role} stopped at its budget of ${s.max_budget_usd:g} for one call "
                                   f"(loop.toml [roles.{role}] max_budget_usd)", kind="crash", vendor=vendor)
            if refused:
                last_error, kind = f"{vendor} model {model or 'default'} refused the call (usage or rate limit)", "outage"
                if model and model == own and fallback and fallback != model:
                    self._refused[(vendor, model)] = self.clock()
                    continue   # the same attempt, at once, on the fallback model: not a retry
            elif code == 0:
                last_error, kind = "it did not match the schema: " + "; ".join(problems[:3]), "crash"
            else:
                last_error, kind = f"{vendor} exited {code}: {err.strip()[-300:]}", outcome
            if code != 0 and attempt < self.retries:
                self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            attempt += 1
        raise AgentFailure(f"{vendor} {role} failed after {self.retries + 1} attempts: {last_error}", kind=kind,
                           vendor=vendor)

    # ---------------------------------------------------------------- prompt pieces
    @staticmethod
    def _issue_vars(req: IssueRequest, role: str) -> dict:
        ledger = "\n".join(f"- {i.id} ({i.section}): {i.text}" for i in req.spec.ledger)
        feedback = ("Feedback you must address:\n" + "\n".join(f"- {f}" for f in req.feedback)) if req.feedback else ""
        facts = req.facts or {}
        return {"role": role, "number": req.issue.number, "title": req.issue.title, "body": req.issue.body,
                "ledger": ledger or "(no checkboxes)", "context": req.context or "(none)",
                "ledger_ids": ", ".join(i.id for i in req.spec.ledger), "feedback": feedback,
                "lacks": ", ".join(facts.get("lacks") or []) or "(nothing)",
                "invariants": "\n".join(f"- {i}" for i in facts.get("invariants") or []),
                "decisions": json.dumps(facts.get("decisions") or [], indent=1)[:4000],
                "repair": "yes" if facts.get("repair") else "no"}

    def _root(self) -> Path:
        return self.ws.coord_path()

    @staticmethod
    def _touches_protected(worktree: Path, base: str, head: str) -> bool:
        """Whether the change `base..head` adds, changes or removes a protected path (`PROTECTED_*`
        and `SAFETY_SURFACE_*` in config.py). A rename counts as the removal of its old path and the
        addition of its new one. A change whose paths cannot be listed counts as one that does."""
        proc = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", f"{base}..{head}"], cwd=worktree,
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return True
        return any(is_protected(p) or is_safety_surface(p) for p in proc.stdout.split("\0") if p)

    def _diff(self, worktree: Path, base: str, head: str) -> str:
        diff = subprocess.run(["git", "diff", f"{base}..{head}"], cwd=worktree, capture_output=True, text=True,
                              check=False).stdout
        if len(diff) > DIFF_LIMIT:
            diff = diff[:DIFF_LIMIT] + f"\n[diff truncated at {DIFF_LIMIT} characters; read the files directly]"
        return diff or "(empty)"

    # ---------------------------------------------------------------- the Agents interface
    def explore(self, vendor, question, briefing):
        prompt = render("explore", question=question, briefing=briefing)
        return self.call(vendor, "explore", self._root(), prompt)["notes"]

    def plan(self, vendor, req, worktree):
        where = ("Your branch already has work on it (your working directory): read it and plan from where it is."
                 if worktree else "There is no branch yet; you are reading `main`.")
        prompt = render("plan", branch_note=where, **self._issue_vars(req, "builder (planning)"))
        v = self.call(vendor, "plan", worktree or self._root(), prompt, req.issue.number)
        return PlanResult(items=[ItemPlan(**i) for i in v["items"]], zones=v["zones"], decisions=v["decisions"],
                          notes=v["notes"])

    def triage(self, vendor, req, plan):
        prompt = render("triage", plan=json.dumps(plan, indent=1), **self._issue_vars(req, "examiner (triage)"))
        v = self.call(vendor, "triage", self._root(), prompt, req.issue.number)
        return [Objection(**o) for o in v["objections"]]

    def build(self, vendor, req, plan, worktree, feedback):
        req = IssueRequest(issue=req.issue, spec=req.spec, context=req.context, feedback=feedback, facts=req.facts)
        prompt = render("build", plan=json.dumps(plan, indent=1), **self._issue_vars(req, "builder"))
        v = self.call(vendor, "build", worktree, prompt, req.issue.number)
        requests = []
        for r in v["requests"]:
            command = ({"criterion": r.get("item") or "", "command": r["command"], "cwd": r.get("cwd") or ".",
                        "timeout_s": r.get("timeout_s"), "author": "builder"} if r.get("command") else None)
            requests.append(Request(kind=r["kind"], detail=r["detail"], paths=r["paths"], item=r["item"],
                                    finding=r["finding"], capability=r["capability"], depends_on=r["depends_on"],
                                    depends_quote=r["depends_quote"], command=command))
        return BuildResult(status=v["status"], note=v["note"], tests=v["tests"],
                           commands=[CheckSpec(criterion=c["criterion"], command=c["command"], cwd=c["cwd"],
                                               timeout_s=c["timeout_s"]) for c in v["commands"]],
                           dispositions={d["test"]: d["disposition"] for d in v["dispositions"]},
                           decisions=v["decisions"], requests=requests)

    def resolve_locked(self, vendor, req, worktree, files, feedback):
        req = IssueRequest(issue=req.issue, spec=req.spec, context=req.context, feedback=feedback, facts=req.facts)
        prompt = render("resolve_locked", files=", ".join(f"`{f}`" for f in files),
                        **self._issue_vars(req, "conflict resolver for locked tests"))
        self.call(vendor, "resolve_locked", worktree, prompt, req.issue.number)

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        compact = [{k: e.get(k) for k in ("criterion", "kind", "command", "passed", "exit_code", "note", "tests")} |
                   {"output_tail": (e.get("output_tail") or "")[-800:]} for e in evidence]
        scope = (f"This is a follow-up examination. You examined `{since[:12]}` before; the change since then is "
                 f"below. Check the open findings and the delta; certify the new head if it still holds."
                 if since else "This is the first examination: examine the whole change.")
        diff = self._diff(worktree, since or base_sha, head_sha)
        # A change to a protected path is examined on the escalated model (ADR 0001, amendment 13):
        # the whole change against its base, whatever a follow-up examination is shown.
        escalate = self._touches_protected(worktree, base_sha, head_sha)
        with self.ws.disposable(worktree) as scratch:
            prompt = render("examine", plan=json.dumps(plan, indent=1), base=(since or base_sha)[:12], head=head_sha[:12],
                            scope=scope, diff=diff, evidence=json.dumps(compact, indent=1),
                            memory=json.dumps(memory, indent=1)[:12000] if memory else "(none)",
                            **self._issue_vars(req, "examiner"))
            v = self.call(vendor, "examine", scratch, prompt, req.issue.number, escalate=escalate)
        findings = [Finding(id="", **f) for f in v["findings"]]
        return ExamResult(verdicts=v["verdicts"], findings=findings, probes=[Probe(**p) for p in v["probes"]],
                          decisions=[], certify=v["certify"], summary=v["summary"])

    def decide(self, vendor, req, worktree, requests):
        prompt = render("decide", requests=json.dumps(requests, indent=1)[:20000],
                        **self._issue_vars(req, "examiner (deciding requests)"))
        # Deciding a `protected_change` request is escalated (ADR 0001, amendment 13), with the
        # requests decided in the same call.
        escalate = any(r.get("kind") == "protected_change" for r in requests)
        v = self.call(vendor, "decide", worktree, prompt, req.issue.number, escalate=escalate)
        return [Decision(request=d["request"], approve=d["approve"], reason=d["reason"],
                         dispositions={x["test"]: x["disposition"] for x in d["dispositions"]}) for d in v["decisions"]]

    def arbitrate(self, vendor, req, dispute, worktree=None):
        prompt = render("arbitrate", dispute=json.dumps(dispute, indent=1)[:20000], **self._issue_vars(req, "arbiter"))
        # Every arbitration is escalated (ADR 0001, amendment 13): it settles a dispute between the keys.
        v = self.call(vendor, "arbitrate", worktree or self._root(), prompt, req.issue.number, escalate=True)
        return Ruling(upheld=v["upheld"], reason=v["reason"])
