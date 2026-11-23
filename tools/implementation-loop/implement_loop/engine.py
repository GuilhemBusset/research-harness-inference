"""The per-issue state machine and the scheduling loop (ADR 0012).

Each issue moves through: pending -> plan -> planned -> build -> verify -> examine -> land ->
merged -> delivered | done, with `deploying` between the merge and the evidence step while the
pipeline's verdict on the merge commit is awaited (`loop.toml [deploy]`). Two vendors hold two
keys: the **builder** (the operator's vendor)
plans, writes the code, its tests and the commands that prove each item; the **examiner** (the
other vendor) triages the plan, examines and probes the exact head, approves or refuses the
builder's typed requests, and certifies the head that may land. The engine owns git, the
trusted verifier, the gates and every merge, and derives its bookkeeping from what happened.

A run never waits for a person and never parks:
- a failed verification or a blocking finding is one more build round; two rounds that change
  nothing bring a replan on the branch, two more end the issue `failed`;
- a merge queue another run holds, an outage, a CI check or a deployment with no verdict, a
  deployment that waits for the owner's approval, a certified change to a workflow file that waits
  for the owner to push and merge it, or an environment the doctor can repair is a **wait** that
  resumes by itself (in this run, or under the supervisor);
- every answered agent call is charged to the issue's ceiling, every timeout or crash to its
  infrastructure budget, and every dollar a vendor reports to its dollar ceiling; spending any of
  them ends the issue `failed`, kept in the run's journal, which a new engine version resumes once;
- an item that needs a capability the host lacks becomes an owner obligation; one that needs
  another issue waits for it in the run, or becomes a deferred obligation.

Steps run on a thread pool, so one issue can be examined while another builds and a third
plans. Landing is serialized through a repo-wide merge-queue claim, issues through per-issue
claims, so two runs never land at once or work the same issue. The claim is kept from a merge
until its deployment verdict, so one merge maps to one deployment and one verdict; where the
verdict outlasts the run, a record of the awaited commit keeps every run from landing.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from . import VERSION, gate, graph, issue_parser, report, sandbox, scheduler, verify
from .agents import AgentFailure, Agents, AgentStopped, CeilingReached, IssueRequest
from .config import (
    HISTORY_FINDINGS_EXIT,
    INFRA_PREFIXES,
    Config,
    ceiling,
    history_checks,
    is_protected,
    is_safety_surface,
    standing_checks,
    usd_ceiling,
)
from .github import GitHub, GitHubError
from .graph import Plan
from .model import (
    AGENT_PROOFS,
    SATISFIES_DEPENDENTS,
    TERMINAL,
    WAIT_CI,
    WAIT_CLAIM,
    WAIT_ENGINE,
    WAIT_ENVIRONMENT,
    WAIT_GITHUB,
    WAIT_MERGE_QUEUE,
    WAIT_OUTAGE,
    WAIT_RUN_CEILING,
    WRITING_PHASES,
    CheckSpec,
    Evidence,
    Finding,
    Issue,
    IssueState,
    Phase,
)
from .state import FaultOutbox, RepoClaims, RunLock, RunStore, runs_root
from .workspace import AgentCall, GitError, MergeConflict, UpdateResult, Workspace

try:   # BranchHeld arrives with the workspace that adds a deleted worktree again
    from .workspace import BranchHeld
except ImportError:   # pragma: no cover - older workspace.py, which never raises it
    class BranchHeld(GitError):
        pass

try:   # GitHubUnavailable arrives with the retrying GitHub client
    from .github import GitHubUnavailable
except ImportError:   # pragma: no cover - older github.py
    class GitHubUnavailable(GitHubError):
        pass

try:   # RedactionUnavailable arrives with the redacting GitHub client
    from .github import RedactionUnavailable
except ImportError:   # pragma: no cover - older github.py, whose client never raises it
    class RedactionUnavailable(GitHubError):
        pass

try:   # the deployment verdict arrives with `[deploy]` in config.py, and with the phase and its two waits in model.py
    from .config import Deploy
    from .model import WAIT_DEPLOY, WAIT_OWNER_APPROVAL
    DEPLOYING = Phase.DEPLOYING
except (ImportError, AttributeError):   # pragma: no cover - older config.py and model.py: no context is configured,
    # so nothing waits, and no issue is ever in the phase, which then equals none
    WAIT_DEPLOY, WAIT_OWNER_APPROVAL, DEPLOYING = "deploy", "owner_approval", cast(Phase, None)

    @dataclass(frozen=True)
    class Deploy:
        contexts: tuple[str, ...] = ()
        timeout_s: int = 3600
        approval_description: str = ""
        verified_description: str = ""
        unverified_tests: tuple[str, ...] | None = None
        discharge_contexts: tuple[str, ...] = ()

try:   # the wait for the owner's push and merge arrives with its kind, and with its record, in model.py
    from .model import WAIT_OWNER_LANDING
except ImportError:   # pragma: no cover - older model.py: its issue state has no record, so nothing waits
    WAIT_OWNER_LANDING = "owner_landing"

try:   # the wait for the owner's review arrives with its kind in model.py, and with the decisions that owe one in github.py
    from .github import REVIEW_OWED
    from .model import WAIT_OWNER_REVIEW
except ImportError:   # pragma: no cover - older github.py and model.py: their client reads no review decision, so nothing waits
    REVIEW_OWED, WAIT_OWNER_REVIEW = ("REVIEW_REQUIRED", "CHANGES_REQUESTED"), "owner_review"

try:   # the scan with the owner's local values arrives with its commands in config.py
    from .config import LOCAL_VALUES_ENV, local_value_checks, local_values_file
except ImportError:   # pragma: no cover - older config.py: it names no list and no scan, so none runs
    LOCAL_VALUES_ENV = "HYGIENE_LOCAL_VALUES"

    def local_values_file(env: Mapping[str, str]) -> Path | None:
        return None

    def local_value_checks(base: str, head: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return ()

LABELS = {
    "loop:plan": ("C5DEF5", "implement loop: planning"),
    "loop:building": ("FBCA04", "implement loop: building or verifying"),
    "loop:examine": ("D4C5F9", "implement loop: examined by the other vendor"),
    "loop:landing": ("0E8A16", "implement loop: PR open, merging"),
    "loop:merged": ("BFDADC", "implement loop: merged, verifying on main"),
    "loop:delivered": ("F9D0C4", "implement loop: merged; owner obligations or deferred items remain"),
    "loop:waiting": ("EDEDED", "implement loop: waiting; resumes by itself"),
    "loop:failed": ("B60205", "implement loop: not delivered"),
}
DEFERRED_LABEL = ("loop:deferred", "C2E0C6", "implement loop: an item waits for another issue")
FOLLOWUP_LABEL = ("loop:follow-up", "BFD4F2", "implement loop: non-blocking findings to consider")
LEGACY_LABELS = ("loop:awaiting-human", "loop:needs-human", "loop:parked", "loop:design", "loop:review")
ENGINE_BUG_LABEL = ("loop:engine-bug", "5319E7", "implement loop: a fault in the loop's own engine")
PHASE_LABEL = {
    Phase.PLAN: "loop:plan", Phase.PLANNED: "loop:plan",
    Phase.BUILD: "loop:building", Phase.VERIFY: "loop:building", Phase.EXAMINE: "loop:examine",
    Phase.LAND: "loop:landing", Phase.MERGED: "loop:merged", DEPLOYING: "loop:merged",
    Phase.DELIVERED: "loop:delivered", Phase.WAITING: "loop:waiting", Phase.FAILED: "loop:failed",
}
EXPLORE_QUESTIONS = (
    "The issue graph and acceptance criteria: what must be true when this run is done?",
    "Existing material and conventions these issues must follow (ADRs, AGENTS.md, guides).",
    "Build, check and CI tooling: how is work validated in this repo today?",
    "External references the issues cite: what do they establish that the work depends on?",
)
MERGE_QUEUE = "merge-queue"
STANDBY_PLANS = 1   # plans prepared beyond what the free writer slots can take (`Engine._admit_plans`)
REVIVED_FEEDBACK = 8   # the most entries of a failed round's feedback a revival keeps after its note (#85)
# Deployment verdicts still awaited, one file per issue, under the runs' root. The merge-queue claim
# belongs to a process: the run frees it when it ends, and a claim whose process died is taken
# over. A verdict can outlast the run that merged (`WAIT_DEPLOY`), so what keeps the landing claim
# held until then is this record, which no process owns: while one exists, no run lands.
DEPLOY_HOLDS = "deploying"
DEPLOY_FAILED = ("failure", "error")   # the final states of a commit status, with "success"
# The words of the phase and of its two waits, where `report.py` looks them up for the progress
# comment and the run summary. They are added from here because issue #12 names this file and
# `model.py` for the phase, and not `report.py`.
report.PHASE_TEXT.setdefault(DEPLOYING, "merged, waiting for the deployment verdict")
report.WAIT_HINT.setdefault(WAIT_DEPLOY, ("The deployment gave no verdict yet; the supervisor reads it again, and "
                                          "nothing else lands before it."))
report.WAIT_HINT.setdefault(WAIT_OWNER_APPROVAL, ("The deployment waits for the owner's approval in the pipeline; the "
                                                  "supervisor reads its status again."))
report.WAIT_HINT.setdefault(WAIT_OWNER_LANDING, ("The loop never pushes a change to a workflow file: the owner pushes "
                                                 "the certified head and merges the pull request the loop then opens; "
                                                 "the supervisor looks again."))
report.WAIT_HINT.setdefault(WAIT_OWNER_REVIEW, ("GitHub merges this pull request only with the owner's approving "
                                                "review; the supervisor reads the review decision again, and the "
                                                "loop merges the certified head once it is approved."))
# The folder whose files the engine never pushes a change to, whatever identity it runs as (ADR 0001,
# amendment 2): the factory cannot change its own gates. A branch whose commits change one is landed
# by the owner (`Engine._owner_lands`), and so is a revert of such a change (`Engine._revert`).
WORKFLOWS = ".github/workflows/"
# The comment that hands a certified head to the owner: one per head, found again by its marker.
OWNER_LANDING_MARKER = "<!-- implement-loop:owner-landing:{} -->"
# The comment that says, on a pull request, that its certified head waits for the owner's review: one per head.
OWNER_REVIEW_MARKER = "<!-- implement-loop:owner-review:{} -->"
# The one review decision that ends that wait on an unchanged head: no decision at all does not.
REVIEW_APPROVED = "APPROVED"
# Output that means the host cannot run a check, whatever the implementation does.
ENVIRONMENT_FAILURE = re.compile(
    r"error while loading shared libraries|Host system is missing dependencies|Executable doesn't exist at"
    r"|No module named pytest|\b(uv|node|npx|npm|python3): (command )?not found", re.IGNORECASE)
CI_NO_VERDICT = (None, "missing", "pending", "stale", "cancelled", "startup_failure")
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.MULTILINE)
# A test that starts a container, as a ledger item names it: a Python file under the repository's
# `tests/container/` (ADR 0001, amendment 5), written from the repository's root, with or without a
# leading `./`. The folder alone names no test, and neither does the same folder under another one.
CONTAINER_TEST = re.compile(r"(?<![\w./-])(?:\./)?tests/container/(?:[\w.-]+/)*[\w.-]+\.py\b")
CHECK_RUN_EVIDENCE = "check_run"   # the kind of the evidence record of an item a CI job's check run proved
# What a verified deployment discharges (ADR 0001, decision 6, Consequences): an item that needs this
# capability and names a smoke test, which the pipeline's verification runs against the deployed
# endpoint. A smoke test, as a ledger item names it: a test file (`test_*.py`) under the repository's
# `tests/smoke/`, written from the repository's root, with or without a leading `./`. The folder
# alone names no test, and neither does the same folder under another one, nor a path that leaves it.
DEPLOYED_ENVIRONMENT = "aws:deployed-environment"
SMOKE_TEST = re.compile(r"(?<![\w./-])(?:\./)?(tests/smoke/(?:(?!\.\.?/)[\w.-]+/)*test_[\w.-]*\.py)\b")
# The context of a stage's verification, as the pipeline's reporter posts it: `verify/<stage>`.
VERIFY_CONTEXT = re.compile(r"verify/[^/\s]+")
COMMIT_STATUS_EVIDENCE = "commit_status"   # the kind of the evidence record of an item a verified deployment proved
DISCHARGE_WRITES = 3   # the writes of an evidence comment a discharge makes while other roots' runs write it too
# The key of a discharge's proof saved before it is written to GitHub, until it is recorded: "writing"
# while its run writes it, "failed" once that run gave up on a write GitHub did not answer.
PUBLISHING = "publishing"
# The key of the invocation that saved such a proof: what its run lock held when it saved it. Another
# root's run takes a proof that is "writing" for one being written only while that same lock is held.
WRITER = "writer"
# A pull request's address, as GitHub gives it: the repository's page, then `pull/<number>`. A
# check run's own page is `runs/<id>` under the same repository page.
PULL_ADDRESS = re.compile(r"(https://[^\s?#]+)/pull/\d+$")
CRITERION_LEAD = re.compile(r"\s*([A-Za-z]+\d+)\b")
# Request kinds an earlier engine took: a path outside the plan's zones, a change of a locked
# command or of a locked test. A stored one is refused with the reason; the builder changes the
# file, the test or the command itself.
RETIRED_REQUESTS = ("paths", "command_change", "test_change")
# The capability a ledger item ends with, as the backlog lint writes it: `... (needs aws:account).`
NEEDS_END = re.compile(r"\(needs ([^()\s]+)\)\.?\s*$")


class Engine:
    def __init__(self, plan: Plan, store: RunStore, gh: GitHub, ws: Workspace, agents: Agents, cfg: Config,
                 repo_root: Path, log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
                 environment: dict | None = None, invoked_at: float | None = None, policy: dict | None = None):
        self.plan, self.store, self.gh, self.ws, self.agents, self.cfg = plan, store, gh, ws, agents, cfg
        self.repo_root = repo_root
        self.log, self.sleep = log, sleep
        self.caps = cfg.caps
        self.claims = RepoClaims(repo_root)
        self.faults = FaultOutbox(repo_root)
        self.owner = f"run-{plan.root}"
        self.op, self.other = agents.operator, agents.other
        environment = environment or {}
        self.env_fingerprint: str | None = environment.get("fingerprint")
        self.check_env: dict[str, str] = dict(environment.get("check_env") or {})
        policy = policy or {}
        self.lacks: list[str] = sorted(policy.get("lacks", []))
        self.ci_jobs: dict[str, str] = dict(policy.get("ci_jobs") or {})   # capability -> the CI job that provides it
        self.invariants: tuple[str, ...] = tuple(policy.get("invariants") or gate.DEFAULT_INVARIANTS)
        self.admit = bool(policy.get("admit_prerequisites", plan.mode == "epic"))
        self.repair = bool(policy.get("repair", False))
        self._accept_lock = threading.Lock()   # one evidence step at a time
        self._meter_lock = threading.Lock()
        self._push_lock = threading.Lock()     # one push at a time in this run; across runs, `_hooks_held`
        self._polling: dict[int, float] = {}
        self.deploy: Deploy = policy.get("deploy") or Deploy()
        self._holds_dir = runs_root(repo_root) / DEPLOY_HOLDS
        self._landers: set[int] = set()   # issues whose land or deploying step runs: one at a time
        self.invoked_at = invoked_at if invoked_at is not None else time.time()
        self.run_calls = 0
        self._charged: set[int] = set()   # issues whose last failed attempt the meter already charged
        self._unpublished: dict[int, str] = {}   # issues whose progress comment the hygiene scanner could not mask
        agents.meter = self

    # ------------------------------------------------------------------ public
    def run(self) -> dict:
        with self.store.lock():
            if self.store.clear_stop(older_than=self.invoked_at):
                self.store.event("stale_stop_cleared")
            self.store.event("run_started", root=self.plan.root, mode=self.plan.mode, work=self.plan.work,
                             operator=self.op, other=self.other, version=VERSION, environment=self.env_fingerprint,
                             repair=self.repair, admit=self.admit)
            self._ensure_labels()
            self._reconcile()
            stopped = False
            try:
                self._context()
                stopped = self._loop()
                if not stopped:
                    self._mark_blocked()
            except CeilingReached as e:   # the run's own ceiling, reached while gathering context
                self.store.event("run_ceiling", error=str(e))
            finally:
                self._release_claims()
            self._discharge_delivered()
            self._reconcile_delivered()
            for n in list(self._unpublished):   # a phase that changed outside a step
                self._withhold(n)
            self._flush_faults()
            summary = self._summary(stopped)
            self._post_run_summary()
            self.store.event("run_finished", **summary)
            return summary

    # ------------------------------------------------------------------ the meter (agents.meter)
    def _infra(self, n: int) -> bool:
        issue = self.plan.issues[n]
        return any(p.startswith(INFRA_PREFIXES) for p in self.plan.specs[n].declared_paths) or "loop:infra" in issue.labels

    def _ceiling(self, n: int) -> int:
        return ceiling(self.plan.issues[n].size, self._infra(n)) + self.store.budget(n)["bonus"]

    def _usd_ceiling(self, n: int) -> float:
        """The dollars the issue may cost (what the vendors report: Claude's calls), with the
        fresh ceiling a revival or a retry granted."""
        return usd_ceiling(self.plan.issues[n].size, self._infra(n)) + self.store.budget(n).get("usd_bonus", 0)

    def _run_ceiling(self) -> int:
        return int(sum(ceiling(self.plan.issues[n].size, False) for n in self.plan.work)
                   * self.caps.run_ceiling_factor) + 8   # + the explorers

    def before(self, issue: int | None, role: str, vendor: str) -> None:
        """Refuse a call the issue's or the run's ceiling no longer covers; journal the attempt
        before it starts, so a run killed during the call charges it when it resumes."""
        with self._meter_lock:
            if self.run_calls >= self._run_ceiling():
                raise CeilingReached(f"the run's ceiling of {self._run_ceiling()} calls is spent", scope="run")
            if issue is None or issue not in self.plan.issues:
                return
            b = self.store.budget(issue)
            if b["calls"] >= self._ceiling(issue):
                raise CeilingReached(f"#{issue}'s ceiling of {self._ceiling(issue)} calls is spent")
            if b.get("usd", 0) >= self._usd_ceiling(issue):
                raise CeilingReached(f"#{issue}'s ceiling of ${self._usd_ceiling(issue):.2f} is spent "
                                     f"(${b.get('usd', 0):.2f} so far)")
            if b["infra"] >= self.caps.infra_budget:
                raise CeilingReached(f"#{issue}'s infrastructure budget of {self.caps.infra_budget} failed attempts is spent")
            self.store.change_budget(issue, pending=1)

    def after(self, issue: int | None, role: str, vendor: str, outcome: str) -> None:
        """Charge an attempt: an answer to the work ceiling, a timeout or crash to the
        infrastructure budget; an outage to neither (the issue waits for it)."""
        with self._meter_lock:
            if outcome == "answered":
                self.run_calls += 1
            if issue is None or issue not in self.plan.issues:
                return
            b = self.store.budget(issue)
            delta = {"set_pending": max(b.get("pending", 0) - 1, 0)}
            if outcome == "answered":
                delta["calls"] = 1
            elif outcome in ("timeout", "crash"):
                delta["infra"] = 1
                self._charged.add(issue)
            b = self.store.change_budget(issue, **delta)
            self.store.event("agent_attempt", issue=issue, role=role, vendor=vendor, outcome=outcome,
                             calls=b["calls"], infra=b["infra"])

    def spent(self, issue: int | None, role: str, vendor: str, cost_usd: float) -> None:
        """Charge what one attempt cost, as its vendor reported it, to the issue's dollar ceiling
        (ADR 0001, decision 8). Whatever the attempt's outcome: a call that failed was paid for."""
        if issue is None or issue not in self.plan.issues or not cost_usd or cost_usd < 0:
            return
        with self._meter_lock:
            b = self.store.change_budget(issue, usd=round(float(cost_usd), 6))
            self.store.event("agent_cost", issue=issue, role=role, vendor=vendor, cost_usd=round(float(cost_usd), 6),
                             usd=round(b.get("usd", 0), 6))

    def _settle_interrupted(self, n: int) -> None:
        """Attempts journaled but never reported (the engine was killed mid-call) are charged as
        failed attempts when the run resumes."""
        pending = self.store.budget(n).get("pending", 0)
        if pending:
            self.store.change_budget(n, infra=pending, set_pending=0)
            self.store.event("interrupted_attempts_charged", issue=n, count=pending)

    # ------------------------------------------------------------------ setup
    def _ensure_labels(self) -> None:
        for name, (color, desc) in LABELS.items():
            self._safe(self.gh.ensure_label, name, color, desc)
        for name, color, desc in (DEFERRED_LABEL, FOLLOWUP_LABEL):
            self._safe(self.gh.ensure_label, name, color, desc)

    def _settle_closed_elsewhere(self) -> None:
        """An issue this run recorded once (a prerequisite pulled in from another epic) that is no
        longer its work and is closed on GitHub, by another run or by hand, is done here too: its
        stale phase would otherwise keep the root from ever settling (#2, 2026-11-19)."""
        for n in self.store.known():
            st = self.store.issue(n)
            if n in self.plan.work or st.phase in (Phase.DONE, Phase.DELIVERED, Phase.FAILED):
                continue
            data = self._safe(self.gh.get_issue, n) or {}
            if data.get("state") == "closed":
                st.phase, st.wait = Phase.DONE, None
                self.store.put(st)
                self.store.event("closed_elsewhere", issue=n, state_reason=data.get("state_reason"))

    def _adopt_deliveries(self) -> None:
        """A work item another root's run already delivered (its issue stays open for the owner's
        items) is delivered here too, with that run's record, instead of being built again on the
        branch that run merged, whose history CI's loop guard then refuses (#122, 2026-11-19).
        Once per issue: a round this run starts after that, after an edit, is its own."""
        adopted = self.store.state.get("adopted", {})
        for n in self.plan.work:
            mine = self.store.issue(n)
            if str(n) in adopted or mine.phase in (Phase.MERGED, DEPLOYING, Phase.DELIVERED, Phase.DONE):
                continue
            found = self._delivered_elsewhere(n)
            if found is None:
                continue
            root, st = found
            st.worktree = mine.worktree   # this run's own worktree, never the other run's
            self.store.put(st)
            self.store.put_section("adopted", str(n), root)
            self.store.event("delivery_adopted", issue=n, root=root, phase=st.phase.value,
                             previous=mine.phase.value, pr=st.pr)
            self._sync(n)

    def _delivered_elsewhere(self, n: int) -> tuple[int, IssueState] | None:
        """The record of issue `n` that another root's run delivered or completed, its latest round."""
        found = []
        for d in runs_root(self.repo_root).glob("issue-*"):
            m = re.fullmatch(r"issue-(\d+)", d.name)
            if not m or d.resolve() == self.store.dir.resolve():
                continue
            try:
                raw = json.loads((d / "state.json").read_text())["issues"].get(str(n))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if raw and raw.get("phase") in (Phase.DELIVERED.value, Phase.DONE.value):
                found.append((raw.get("round", 1), int(m.group(1)), raw))
        if not found:
            return None
        _, root, raw = max(found, key=lambda f: (f[0], f[1]))
        return root, IssueState.from_dict(raw)

    def _reconcile(self) -> None:
        self._settle_closed_elsewhere()
        self._adopt_deliveries()
        for n in list(self.plan.work):
            self._settle_interrupted(n)
            st = self.store.issue(n)
            spec = self.plan.specs[n]
            if st.wait and st.wait.get("kind") == "legacy":
                self._migrate(st)
            if not self._restore_branch(n):
                continue
            st = self.store.issue(n)
            if st.worktree and (Path(st.worktree) / ".git").exists():
                self._recover_worktree(n, Path(st.worktree))
            restored = self._restore_ended(st)
            if st.body_hash and st.body_hash != spec.body_hash:
                if st.phase in (Phase.MERGED, DEPLOYING, Phase.DELIVERED, Phase.DONE):
                    self._new_round(st, ["the issue was edited after its work was merged; deliver the edited version"],
                                    owner_edit=True)
                else:
                    self.store.event("requirements_changed", issue=n, previous_phase=st.phase.value)
                    self._owner_edit(st)
            elif st.phase == Phase.DONE:
                self._new_round(st, ["the issue was reopened after it was closed; find what is missing and deliver it"],
                                owner_edit=True)
            elif st.phase == Phase.WAITING:
                self._maybe_resume(st)
            elif st.phase == Phase.FAILED:
                self._maybe_revive(st)
            elif st.phase in (Phase.PLANNED, Phase.BUILD, Phase.VERIFY, Phase.EXAMINE, Phase.LAND) and not st.plan:
                st.phase = Phase.PENDING
            st.body_hash = spec.body_hash
            st.branch = st.branch or spec.branch or f"loop/issue-{n}"
            if st.extra_deps:
                self._restore_deps(n, st)
            self.store.put(st)
            if restored and st.phase in (Phase.DELIVERED, Phase.FAILED):
                self._sync(n)   # its labels and its comment said `waiting`; `_withhold` if they still must
        self._drop_stale_holds()

    def _restore_ended(self, st: IssueState) -> bool:
        """An issue that had ended (`delivered`, `failed`) and waits only because the hygiene scanner
        could not mask something of it (its progress comment, its fault issue, the comment that the
        owner completed it) is what it was again, with all it recorded (`fail`, its obligations),
        before it is reconciled: an edit starts a new round, a new engine version revives it. What
        was not published is published by this run, or the issue waits again at the run's end."""
        w = st.wait or {}
        if (st.phase != Phase.WAITING or w.get("kind") != WAIT_GITHUB
                or w.get("resume") not in (Phase.DELIVERED.value, Phase.FAILED.value)):
            return False
        self._maybe_resume(st)
        return True

    def _migrate(self, st: IssueState) -> None:
        """State an earlier engine wrote (ADR 0008/0011): a parked issue resumes; a design becomes
        the starting point of a plan, its locked checks become the builder's tests (released in
        the CI guard at the next build, `_release_locks`), and nothing already written is lost."""
        legacy = st.wait or {}
        design = legacy.get("design") or {}
        st.wait = None
        old_tests = sorted(set(design.get("check_files", [])) | {f for f in legacy.get("locked", {})
                                                                  if Path(f).suffix in (".py", ".mjs", ".js", ".sh")})
        if old_tests:
            st.feedback = list(st.feedback) + [(f"An earlier version of the loop wrote these tests on your branch: "
                                                f"{', '.join(old_tests)}. They are yours now; keep, fix or replace them.")]
        if legacy.get("reason"):
            st.feedback.append(f"An earlier version of the loop set this issue aside: {str(legacy['reason'])[:600]}")
        if st.phase not in (Phase.MERGED, Phase.DELIVERED, Phase.DONE):
            st.phase = Phase.PENDING
        self.store.event("migrated", issue=st.number, park_kind=legacy.get("park_kind"), tests=old_tests)
        self.store.put(st)

    def _restore_branch(self, n: int) -> bool:
        """A worktree whose branch was deleted from under it gets its branch back from its own
        history. Without such evidence the issue starts a new round on a new branch: the work
        is lost, never guessed, and the issue carries on. False when a new round was started."""
        st = self.store.issue(n)
        wt = Path(st.worktree) if st.worktree else self.ws.dir / f"issue-{n}"
        if not (wt / ".git").exists():
            return True
        anchors = [st.base_sha, st.head_sha, st.verified_sha, st.examined_sha, st.certified_sha]
        try:
            restored = self.ws.restore_branch(wt, [a for a in anchors if a])
        except GitError as e:
            self.store.event("branch_restore_failed", issue=n, error=str(e)[:300])
            self._new_round(st, [(f"the branch {st.branch} was lost and could not be restored from its history; "
                                  f"start again from main")], owner_edit=False)
            st.phase = Phase.PENDING
            self.store.put(st)
            return False
        if restored:
            self.store.event("branch_restored", issue=n, branch=restored[0].removeprefix("refs/heads/"), sha=restored[1])
        return True

    def _recover_worktree(self, n: int, wt: Path) -> None:
        try:
            self.ws.reattach(wt)
            op = self.ws.abort_in_progress(wt)
        except GitError as e:
            self.store.event("worktree_recovery_failed", issue=n, error=str(e)[:300])
            return
        if op:
            self.store.event("worktree_recovered", issue=n, operation=op, head=self.ws.head(wt))

    def _maybe_resume(self, st: IssueState) -> None:
        """A wait ends when what it waits for may have changed. The supervisor launches a run
        only once an outage is over and runs the doctor (which repairs the environment) first."""
        w = st.wait or {}
        kind = w.get("kind")
        resume = (kind in (WAIT_MERGE_QUEUE, WAIT_CLAIM, WAIT_CI, WAIT_GITHUB, WAIT_OUTAGE, WAIT_RUN_CEILING, "prerequisite",
                           WAIT_DEPLOY, WAIT_OWNER_APPROVAL,   # each run reads the merge commit's statuses again
                           WAIT_OWNER_LANDING)                 # and the remote branch and its pull request
                  or (kind == WAIT_OWNER_REVIEW and not self._review_owed(st, w))
                  or (kind == WAIT_ENVIRONMENT and (self.env_fingerprint is None or w.get("env") != self.env_fingerprint))
                  or (kind == WAIT_ENGINE and w.get("version") != VERSION))
        if not resume:
            return
        st.phase = Phase(w.get("resume") or Phase.PENDING.value)
        if st.phase in (Phase.PLANNED, Phase.BUILD, Phase.VERIFY, Phase.EXAMINE, Phase.LAND) and not st.plan:
            st.phase = Phase.PENDING
        st.wait = None
        self.store.event("resumed", issue=st.number, wait=kind, phase=st.phase.value)

    def _review_owed(self, st: IssueState, w: dict) -> bool:
        """Whether an issue that waits for the owner's review (`WAIT_OWNER_REVIEW`) still does. It
        does while its pull request is open at the head the wait is for and the review decision is
        not APPROVED: a review owed, no decision at all, or a GitHub that cannot be read, which
        says nothing. Then nothing is run for the issue: no land step, so no verification and no
        merge queue. It no longer does once the decision is APPROVED: the land step tries the merge
        of that head again. Nor once the pull request is merged or closed, or has another head:
        the land step reads that as it always did, and a head that moved is verified again before
        anything lands."""
        pr, head = w.get("pr"), w.get("head")
        try:
            pull = (self.gh.find_pr(st.branch) if st.branch else None) or {}
            if (pull.get("number") != pr or pull.get("state") != "open" or not head or head != st.certified_sha
                    or (pull.get("head") or {}).get("sha", head) != head):
                return False
            decision = self._review_decision(pull["number"])
            if decision in REVIEW_OWED:
                self._say_review_owed(pull["number"], head, decision)   # where GitHub did not take the comment before
        except (GitHubError, OSError) as e:
            self.store.event("github_call_failed", call="_review_owed", issue=st.number, error=str(e)[:300])
            return True
        return decision != REVIEW_APPROVED

    def _maybe_revive(self, st: IssueState) -> None:
        """A failed issue resumes once, with a fresh ceiling, on a new engine version: the loop
        repairs itself (as it did for #74 and #78) and tries again."""
        f = st.fail or {}
        if f.get("version") == VERSION or f.get("resumed"):
            return
        b = self.store.budget(st.number)
        self.store.change_budget(st.number, bonus=b["calls"] - b["bonus"] if b["calls"] > b["bonus"] else 0, set_infra=0,
                                 set_usd_bonus=b.get("usd", 0))
        st.unproductive = st.replans = 0
        st.fail = {**f, "resumed": True}
        st.phase = Phase.BUILD if st.plan and st.worktree else Phase.PENDING
        retired = self._retire_uncommanded(st)   # heals a state saved before the fix (#148)
        # What the failed round left (its problems, the decisions on the last build's requests) is
        # kept after the note: #85 was revived straight after a failed examination, and replacing
        # its feedback would have sent the build back without the refusals it had been given.
        note = (f"An earlier attempt failed ({f.get('cause')}): {str(f.get('detail'))[:500]}. "
                f"A new engine version gives it one more try.")
        st.feedback = [note] + list(dict.fromkeys(t for t in st.feedback if t != note))[-REVIVED_FEEDBACK:]
        if retired:
            st.feedback.append(f"These commands were retired, because the plan no longer proves their items by "
                               f"a command: {retired[:6]}.")
        self.store.event("revived", issue=st.number, cause=f.get("cause"), phase=st.phase.value)

    def _drop_probes(self, n: int, wt: Path) -> bool:
        """Probes an earlier engine committed leave the branch, with the examiner's commands that
        ran them, before it is verified again: no examiner probe reaches main. An open finding one
        witnessed keeps the probe's command and text, as a finding does now. True when it did."""
        st = self.store.issue(n)
        examiner = [c for c in st.commands if c.get("author") == "examiner"]
        if not st.probes and not examiner:
            return False
        for f in st.findings:
            cmd = next((c for c in examiner if f.get("probe") and _names_probe(c, f["probe"])), None)
            if (f.get("status") == "open" and cmd and self._inside(wt, f["probe"])
                    and (wt / f["probe"]).is_file()):
                f.update({"reproduction": cmd["command"], "cwd": cmd.get("cwd") or ".",
                          PROBE_TEXT: (wt / f["probe"]).read_text(errors="replace")})
        dropped = set(st.probes)
        for p in sorted(dropped):
            if self._inside(wt, p) and (wt / p).is_file():
                (wt / p).unlink()
        sha = self.ws.commit_all(wt, f"Drop the examiner's probes for #{n}", "acceptance-checks", n,
                                 unlocks=sorted(dropped) or None)
        st.commands = [c for c in st.commands if c.get("author") != "examiner"]
        st.probes = []
        self.store.put(st)
        self.store.event("probes_dropped", issue=n, sha=sha, files=sorted(dropped), commands=len(examiner))
        return True

    def _owner_edit(self, st: IssueState) -> None:
        """The owner edited the issue: new evidence. Plan again on the branch, with the previous
        decisions as a starting point; budgets are reset."""
        notes = ["The owner edited the issue; plan for the edited version, on your branch."]
        if st.decisions:
            notes.append("Decisions so far, to keep where they still fit: " + json.dumps(st.decisions)[:1500])
        st.plan = None
        st.feedback = notes
        self.store.change_budget(st.number, set_calls=0, set_infra=0, set_bonus=0, set_pending=0, set_usd=0,
                                 set_usd_bonus=0)
        st.unproductive = st.replans = 0
        st.fix_forwards = st.reverts = st.merge_refusals = st.restarts = 0
        st.signature = ""
        st.certified_sha = st.verified_sha = None
        st.fail = st.wait = None
        st.deploy = None   # with it goes the record that held the landing claim (`_new_round`, `_drop_stale_holds`)
        if (self._owner_record(st) or {}).get("revert"):   # as any revert that waited: the edited issue is planned
            st.owner_landing = None                        # again; the branch's own record stays with the branch
        st.phase = Phase.PENDING

    def _context(self) -> None:
        if self.store.state.get("context"):
            return
        briefing = report.render_briefing(self.plan)
        with ThreadPoolExecutor(max_workers=self.caps.max_parallel_agents) as pool:
            notes = list(pool.map(lambda q: self.agents.explore(self.op, q, briefing), EXPLORE_QUESTIONS))
        self.store.state["context"] = "\n\n".join(notes)
        self.store.save()
        self.store.event("context_gathered", explorers=len(notes))

    # ------------------------------------------------------------------ loop
    def _loop(self) -> bool:
        running: dict[int, Future] = {}
        stopped = False
        with ThreadPoolExecutor(max_workers=self.caps.max_parallel_agents) as pool:
            while True:
                for n, fut in list(running.items()):
                    if fut.done():
                        del running[n]
                        self._finished(n, fut.exception())
                        self._withhold(n)
                if self.store.stop_requested():
                    if not stopped:
                        self.store.event("stop_requested")
                        stopped = True
                        terminate = getattr(self.agents, "terminate_all", None)
                        if terminate:
                            terminate()
                    if not running:
                        return True
                else:
                    self._dispatch(pool, running)
                if not running:
                    if self._polling and not self.store.stop_requested():
                        self.sleep(self.caps.queue_poll_s)
                        self._expire_polls()
                        continue
                    return stopped
                wait(list(running.values()), timeout=30, return_when=FIRST_COMPLETED)
                if self.store.stop_requested() and not stopped:
                    self.store.event("stop_requested")
                    stopped = True
                    terminate = getattr(self.agents, "terminate_all", None)
                    if terminate:
                        terminate()   # a long agent call is stopped now, not when it ends

    def _finished(self, n: int, exc: BaseException | None) -> None:
        if exc is None:
            return
        if isinstance(exc, AgentStopped) or type(exc).__name__ == "AgentStopped":
            self.store.event("step_interrupted", issue=n, phase=self.store.issue(n).phase.value)
        elif isinstance(exc, CeilingReached):
            if exc.scope == "run":
                self._wait(n, WAIT_RUN_CEILING, str(exc))
            else:
                self._fail(n, "ceiling", str(exc))
        elif isinstance(exc, AgentFailure) or type(exc).__name__ == "AgentFailure":
            kind = getattr(exc, "kind", "crash")
            self.store.event("agent_unavailable", issue=n, failure=kind, error=str(exc)[:500])
            if kind == "outage":
                self._wait(n, WAIT_OUTAGE, str(exc)[:300], vendor=getattr(exc, "vendor", ""))
                return
            # a timeout or crash: charged here too (a failure before the meter started is not
            # charged by it), so retrying the step always ends within the infrastructure budget
            if n not in self._charged:
                self.store.change_budget(n, infra=1)
            self._charged.discard(n)
            if self.store.budget(n)["infra"] >= self.caps.infra_budget:
                self._fail(n, "ceiling", f"#{n}'s infrastructure budget of {self.caps.infra_budget} failed attempts "
                                         f"is spent: {str(exc)[:300]}")
        elif isinstance(exc, GitHubUnavailable):
            self._wait(n, WAIT_GITHUB, str(exc)[:300])
        elif isinstance(exc, sandbox.SandboxUnavailable):
            # Nothing an agent controls ran, and nothing is concluded from that: the issue waits
            # until the host can isolate agents and checks again (the doctor says what is missing).
            self._wait(n, WAIT_ENVIRONMENT, f"the sandbox is unavailable, so nothing was run: {str(exc)[:300]}")
        else:
            self._engine_fault(n, exc)

    def _phases(self) -> dict[int, Phase]:
        return {n: self.store.issue(n).phase for n in self.plan.work}

    def _claim_issue(self, n: int) -> bool:
        """Two runs never work the same issue: a repo-wide claim per issue, held for the run."""
        if self.claims.acquire(f"issue-{n}", self.owner):
            self._polling.pop(-n, None)
            return True
        self._polling.setdefault(-n, time.monotonic())
        return False

    def _release_claims(self) -> None:
        for n in self.plan.work:
            self.claims.release(f"issue-{n}", self.owner)
        self.claims.release(MERGE_QUEUE, self.owner)

    def _expire_polls(self) -> None:
        """A merge queue or an issue another run holds is polled for `queue_wait_s`; past that
        the issue waits for the supervisor."""
        now = time.monotonic()
        for key, since in list(self._polling.items()):
            if now - since < self.caps.queue_wait_s:
                continue
            del self._polling[key]
            n = abs(key)
            self._wait(n, WAIT_CLAIM if key < 0 else WAIT_MERGE_QUEUE,
                       "another run held it for longer than the in-run wait")

    def _dispatch(self, pool: ThreadPoolExecutor, running: dict[int, Future]) -> None:
        phases = self._phases()
        writers = [n for n, ph in phases.items() if ph in WRITING_PHASES]
        # A land step keeps the merge queue until it ends, which with a deployment to wait for is
        # after its issue left `land`: the step counts, whatever phase its issue is in by now.
        self._landers &= set(running)
        landing = bool(self._landers)
        steps = {Phase.PLAN: self._step_plan, Phase.BUILD: self._step_build, Phase.VERIFY: self._step_verify,
                 Phase.EXAMINE: self._step_examine, Phase.MERGED: self._step_accept}
        fresh: list[int] = []   # dependency-ready issues with no saved plan: admitted after the runnable work
        foreign: set[int] = set()   # issues another run holds: they take none of this run's planning room
        # An issue that waits for its deployment verdict takes the merge queue before any issue lands.
        for n in sorted(self.plan.work, key=lambda m: phases.get(m) != DEPLOYING):
            if n in running:
                continue
            ph = phases.get(n, self.store.issue(n).phase)
            if ph in TERMINAL or ph == Phase.WAITING:
                continue
            if not self._claim_issue(n):
                foreign.add(n)
                continue
            if ph in (Phase.PENDING, Phase.PLAN) and not self.store.issue(n).plan:
                # a first plan, or one a stopped run left unfinished
                if ph == Phase.PLAN or scheduler.prerequisites_met(self.plan, phases, n):
                    fresh.append(n)
                continue
            if ph == Phase.PENDING:
                if not scheduler.prerequisites_met(self.plan, phases, n):
                    continue
                self._set_phase(n, Phase.PLANNED)   # already planned: it may start building in this pass
                ph = Phase.PLANNED
            if ph == Phase.PLANNED:
                if scheduler.can_start_writing(self.plan, n, writers, self.caps.max_parallel_writers):
                    writers.append(n)
                    self._set_phase(n, Phase.BUILD)
                    running[n] = pool.submit(self._step_build, n)
            elif ph in (Phase.LAND, DEPLOYING):
                if landing:
                    continue
                if self._take_queue(n, lands=ph == Phase.LAND):
                    landing = True
                    self._landers.add(n)
                    self._polling.pop(n, None)
                    running[n] = pool.submit(self._step_land if ph == Phase.LAND else self._step_deploying, n)
                    continue
                hold = self._unreleasable_hold(n, phases) if ph == Phase.LAND else None
                if hold:   # polling would not see it end: the verdict is read again by the next run
                    self._polling.pop(n, None)
                    self._wait(n, WAIT_MERGE_QUEUE, f"nothing lands before #{hold.get('issue')} has the deployment "
                                                    f"verdict of {str(hold.get('sha'))[:12]}", resume=Phase.LAND)
                else:
                    self._polling.setdefault(n, time.monotonic())   # another run is landing, or waits for a verdict: poll
            elif ph in steps:
                running[n] = pool.submit(steps[ph], n)
        if fresh:
            self._admit_plans(pool, running, fresh, foreign)

    def _admit_plans(self, pool: ThreadPoolExecutor, running: dict[int, Future], fresh: list[int],
                     foreign: set[int]) -> None:
        """Plan close to when a writer can take the issue: first plans are admitted, after every
        runnable step, for the free writer slots plus one standby, and no more. Plans in flight and
        completed plans awaiting a build of this run take that room first, so the count holds
        across passes, whatever their paths. A plan fills a slot only when it can write alongside
        the writers and the plans already counted for the other slots; any other plan can only be
        the standby."""
        phases = self._phases()
        held = [n for n in self.plan.work if phases.get(n) in WRITING_PHASES]
        room = max(self.caps.max_parallel_writers - len(held), 0) + STANDBY_PLANS
        standby = 0

        def fits(n: int) -> bool:
            return scheduler.can_start_writing(self.plan, n, held, self.caps.max_parallel_writers)

        for n in self.plan.work:   # an issue waiting on a prerequisite found while planning is not counted
            if n in fresh or n in foreign or phases.get(n) not in (Phase.PLAN, Phase.PLANNED):
                continue
            room -= 1
            if fits(n):
                held.append(n)
            else:
                standby += 1
        for n in fresh:
            slot = room > 0 and fits(n)
            if not slot and not (room > 0 and standby < STANDBY_PLANS):
                if phases.get(n) == Phase.PLAN:
                    self._set_phase(n, Phase.PENDING)   # an unfinished plan waits for its turn like a first one
                continue
            room -= 1
            if slot:
                held.append(n)
            else:
                standby += 1
            self._set_phase(n, Phase.PLAN)
            running[n] = pool.submit(self._step_plan, n)

    # ------------------------------------------------------------------ helpers
    def _req(self, n: int, feedback: list[str] | None = None) -> IssueRequest:
        st = self.store.issue(n)
        facts = {"lacks": self.lacks, "invariants": list(self.invariants), "repair": self.repair,
                 "decisions": st.decisions[-20:], "operator": self.op, "other": self.other}
        return IssueRequest(issue=self.plan.issues[n], spec=self.plan.specs[n],
                            context=self.store.state.get("context") or "", feedback=list(feedback or []), facts=facts)

    def _item_text(self, n: int) -> dict[str, str]:
        return {i.id: i.text for i in self.plan.specs[n].ledger}

    def _items(self, st: IssueState) -> list[dict]:
        return (st.plan or {}).get("items", [])

    def _agent_items(self, st: IssueState) -> set[str]:
        return {i["id"] for i in self._items(st) if i["proof"] in AGENT_PROOFS}

    def _owner_items(self, st: IssueState) -> set[str]:
        return {i["id"] for i in self._items(st) if i["proof"] in ("owner", "deferred")}

    def _review_items(self, st: IssueState) -> set[str]:
        return {i["id"] for i in self._items(st) if i["proof"] == "review"}

    def _agent_part_items(self, st: IssueState) -> set[str]:
        """Owner items whose agent part must still be proven (by a command or a verdict). An item
        whose text ends with a capability no CI job provides (`(needs aws:...)`, `(needs human:...)`)
        is the owner's alone: no verdict is asked on it, and it ends as an obligation."""
        texts = self._item_text(st.number)
        return {i["id"] for i in self._items(st) if i["proof"] == "owner"
                and (i.get("agent_part") or "none").strip().lower() not in ("", "none")
                and not self._owner_only(texts.get(i["id"], ""))}

    def _owner_only(self, text: str) -> bool:
        m = NEEDS_END.search(text)
        return bool(m) and m.group(1) not in self.ci_jobs

    def _verdict_items(self, st: IssueState) -> set[str]:
        """Items the examiner must judge: review items, and owner items' agent parts no command proves."""
        commanded = {c["criterion"] for c in st.commands if c.get("author") == "builder"}
        return self._review_items(st) | (self._agent_part_items(st) - commanded)

    def _commanded_items(self, st: IssueState) -> set[str]:
        """Items a builder's command may prove: `test` and `command` items, and owner items' agent
        parts. A review or deferred item has no builder command."""
        return {i["id"] for i in self._items(st) if i["proof"] in ("test", "command", "owner")}

    def _retire_uncommanded(self, st: IssueState) -> list[str]:
        """Retire the builder's commands for items the current plan no longer proves by a command.
        In #148, a replan moved A2 from `command` to `review`, but A2's old command kept running,
        and failing, until the replan could change nothing (no_progress). Only items the plan names
        with another proof are retired; a command for an item the plan does not list is left alone."""
        uncommanded = {i["id"] for i in self._items(st)} - self._commanded_items(st)
        stale = [c for c in st.commands if c.get("author") == "builder" and c.get("criterion") in uncommanded]
        if not stale:
            return []
        st.commands = [c for c in st.commands if c not in stale]
        retired = [f"{c.get('criterion')}: {c['command'][:160]}" for c in stale]
        self.store.event("commands_retired", issue=st.number, commands=retired[:8])
        return retired

    def _specs(self, st: IssueState) -> list[CheckSpec]:
        fields = CheckSpec.__dataclass_fields__
        return [CheckSpec(**{k: v for k, v in c.items() if k in fields}) for c in st.commands]

    def _set_phase(self, n: int, phase: Phase, **changes) -> None:
        st = self.store.issue(n)
        previous = st.phase
        st.phase = phase
        b = self.store.budget(n)   # a mirror for reports; the budget section is the source of truth
        st.calls, st.infra, st.ceiling_bonus = b["calls"], b["infra"], b["bonus"]
        for k, v in changes.items():
            setattr(st, k, v)
        self.store.put(st)
        if previous != phase:
            self.store.event("phase", issue=n, frm=previous.value, to=phase.value)
            self.log(f"#{n} {previous.value} -> {phase.value}")
            self._sync(n)

    def _wait(self, n: int, kind: str, detail: str, resume: Phase | None = None, **extra) -> None:
        """Not terminal: the issue resumes by itself once its condition clears."""
        st = self.store.issue(n)
        resume_phase = resume or (st.phase if st.phase != Phase.WAITING else Phase.PENDING)
        st.wait = {"kind": kind, "resume": resume_phase.value, "detail": detail[:600], "since": time.time(), **extra}
        if kind == WAIT_ENVIRONMENT:
            st.wait["env"] = self.env_fingerprint
        if kind == WAIT_ENGINE:
            st.wait["version"] = VERSION
        self.store.put(st)
        self.store.event("waiting", issue=n, wait=kind, detail=detail[:600], resume=resume_phase.value)
        self.log(f"#{n} waiting ({kind}): {detail[:200]}")
        self._set_phase(n, Phase.WAITING)

    def _fail(self, n: int, cause: str, detail: str) -> None:
        """Explicit non-delivery: nothing partial is merged, nothing is called delivered. A stall is
        no loop fault: no issue is filed for it, and the run's journal (the event `failed`) keeps
        its cause and detail. A new engine version resumes it once."""
        st = self.store.issue(n)
        st.fail = {"cause": cause, "detail": detail[:800], "version": VERSION, "resumed": bool((st.fail or {}).get("resumed"))}
        self.store.put(st)
        self.store.event("failed", issue=n, cause=cause, detail=detail[:800])
        self.log(f"#{n} failed ({cause}): {detail[:200]}")
        self._set_phase(n, Phase.FAILED)

    def _new_round(self, st: IssueState, feedback: list[str], owner_edit: bool) -> None:
        """Deliver an already-merged issue again (it was edited, reopened, or broke `main`): a new
        branch from `main`; the plan is kept unless the owner edited the issue."""
        n = st.number
        if st.worktree:
            self.ws.remove(Path(st.worktree))
        st.round += 1
        st.branch = f"{self.plan.specs[n].branch or f'loop/issue-{n}'}-r{st.round}"
        st.worktree = st.pr = st.head_sha = st.base_sha = None
        st.verified_sha = st.examined_sha = st.certified_sha = None
        st.deploy = None   # the next merge has its own deployment and its own verdict
        self._release_hold(n)
        if self._owner_record(st) is not None:
            st.owner_landing = None   # it named the branch this round leaves
        st.probes = []
        st.commands = [c for c in st.commands if c.get("author") != "examiner"]   # their probes went with the branch
        st.findings = [f for f in st.findings if f.get("status") == "follow_up"]
        st.human_tasks, st.dropped = [], []
        st.feedback = list(feedback)
        if owner_edit:
            self._owner_edit(st)
            st.feedback = list(feedback) + st.feedback
        st.phase = Phase.PENDING
        self.store.event("new_round", issue=n, round=st.round, reason=feedback[0][:400] if feedback else "",
                         owner_edit=owner_edit)

    def _engine_fault(self, n: int, exc: BaseException) -> None:
        """An engine bug: journal it, queue one GitHub issue per distinct fault; the issue waits
        for a new engine version."""
        detail = redact("".join(traceback.format_exception(exc))[-2500:], self.repo_root)
        fingerprint = fault_fingerprint(exc)
        phase = self.store.issue(n).phase.value
        self.store.event("step_crashed", issue=n, error=redact(repr(exc), self.repo_root)[:500], traceback=detail,
                         fingerprint=fingerprint)
        title = f"implement loop: {type(exc).__name__} in the engine ({fingerprint})"
        body = (f"{report.FAULT_MARKER.format(fingerprint)}\nThe implement loop's engine raised `{type(exc).__name__}` "
                f"in phase `{phase}` while working on #{n} (run for #{self.plan.root}, engine {VERSION}). The issue "
                f"waits for a new engine version and the run carried on. The message and full traceback are in the "
                f"local run log (`events.jsonl`, event `step_crashed`, fingerprint `{fingerprint}`).\n\nEngine frames:\n\n"
                f"```\n{engine_frames(exc)}\n```\n\n"
                f"## Deliverables\n- [ ] A regression test reproduces this fault's path ({engine_frames(exc).splitlines()[-1] if engine_frames(exc) else 'unknown'}).\n"
                f"- [ ] The engine handles that path, and the test passes.\n"
                f"- [ ] The engine version (`implement_loop/__init__.py`) is bumped, so waiting issues resume.\n\n"
                f"## Acceptance criteria\n- [ ] The engine's full test suite passes.\n"
                f"- [ ] Nothing on the loop's safety surface changes.\n")
        self.faults.record(fingerprint, title, body, issue=n, run=self.plan.root)
        resume = Phase(phase) if phase not in (Phase.WAITING.value, Phase.FAILED.value) else Phase.PENDING
        self._wait(n, WAIT_ENGINE, f"engine error {type(exc).__name__} in phase {phase} (fault {fingerprint})",
                   resume=resume, fingerprint=fingerprint)

    def _flush_faults(self) -> None:
        self._safe(self.gh.ensure_label, *ENGINE_BUG_LABEL)
        for fingerprint, entry in self.faults.pending():
            try:
                number = entry.get("filed") or self.gh.find_issue(ENGINE_BUG_LABEL[0], report.FAULT_MARKER.format(fingerprint))
                if number:
                    self.gh.create_comment(number, f"Seen again: {entry['count']} occurrence(s) so far, latest on "
                                                   f"#{entry['issues'][-1]} (run for #{entry['runs'][-1]}).")
                else:
                    number = self.gh.create_issue(entry["title"], entry["body"], [ENGINE_BUG_LABEL[0]])["number"]
                self.faults.mark_reported(fingerprint, number)
                self.store.event("fault_filed", fingerprint=fingerprint, issue=number)
            except RedactionUnavailable as e:
                # Not best effort: a failed issue is settled for the supervisor, which would stop
                # with the fault never filed. The fault's issues of this run wait, as they are
                # (`fail` is kept); the fault stays in the outbox and the next run files it.
                self.store.event("fault_filing_failed", fingerprint=fingerprint, error=str(e)[:300])
                for n in sorted({n for n, root in zip(entry["issues"], entry["runs"], strict=False)
                                 if root == self.plan.root and n in self.plan.work}):
                    if self.store.issue(n).phase not in (Phase.WAITING, Phase.DONE):
                        self._wait(n, WAIT_GITHUB, f"the fault issue of #{n} ({fingerprint}) was not filed: {str(e)[:300]}")
            except Exception as e:  # noqa: BLE001 - filing is best effort, by design
                self.store.event("fault_filing_failed", fingerprint=fingerprint, error=str(e)[:300])

    def _safe(self, fn, *args, **kwargs):
        """A GitHub call the engine can do without: a failure is journaled and the caller goes on.
        Not when the hygiene scanner could not mask what was to be published: nothing was sent, and
        no step may go on as if it had been (`_accept` would close an issue whose follow-up was
        never filed). That one is raised: the step ends there and `_finished` makes its issue wait
        (`WAIT_GITHUB`); outside a step, `_sync` and the run summary catch it."""
        try:
            return fn(*args, **kwargs)
        except RedactionUnavailable:
            raise
        except (GitHubError, OSError) as e:
            self.store.event("github_call_failed", call=getattr(fn, "__name__", str(fn)), error=str(e)[:300])
            return None

    def _sync(self, n: int) -> None:
        st = self.store.issue(n)
        wanted = PHASE_LABEL.get(st.phase)
        for label in (*LABELS, *LEGACY_LABELS):
            if label != wanted:
                self._safe(self.gh.remove_label, n, label)
        if wanted:
            self._safe(self.gh.add_labels, n, [wanted])
        try:
            self._upsert_comment(n, report.PROGRESS_MARKER, report.render_progress(st, self.plan.root))
            self._unpublished.pop(n, None)
        except RedactionUnavailable as e:
            # A phase changes in many places that are no step (the dispatch, `_wait`, `_fail`), so
            # this is not raised: the phase stays recorded, and `_withhold` makes the issue wait.
            self._unpublished[n] = str(e)
            self.store.event("progress_not_published", issue=n, phase=st.phase.value, error=str(e)[:300])

    def _withhold(self, n: int) -> None:
        """The hygiene scanner could not mask the progress comment of issue `n`, so it was not
        written: the issue waits, and the next run writes it at the issue's next change of phase.
        A failed or delivered issue waits too, and resumes as failed or delivered
        (`_restore_ended`): ended, it would be settled for the supervisor with its comment never
        written. An issue that already waits keeps its reason, and a done one is closed: before
        `done`, `_announce` has written the comment already."""
        detail = self._unpublished.pop(n, None)
        if detail and self.store.issue(n).phase not in (Phase.WAITING, Phase.DONE):
            self._wait(n, WAIT_GITHUB, detail)
            self._unpublished.pop(n, None)   # the wait's own comment was not written either

    def _announce(self, n: int, st: IssueState, phase: Phase) -> None:
        """Write the progress comment of the phase `st` is about to end in, before that phase is
        recorded. `_sync` writes it again with the phase, but an ended issue is not worked again:
        a comment the hygiene scanner could not mask then would never be written. Here that
        failure is raised (`RedactionUnavailable`) while the issue can still wait."""
        self._upsert_comment(n, report.PROGRESS_MARKER, report.render_progress(replace(st, phase=phase), self.plan.root))

    def _upsert_comment(self, n: int, marker: str, body: str) -> dict | None:
        comments = self._safe(self.gh.list_comments, n) or []
        for c in comments:
            if marker in (c.get("body") or ""):
                return self._safe(self.gh.update_comment, c["id"], body)
        return self._safe(self.gh.create_comment, n, body)

    def _requirements_current(self, n: int) -> bool:
        data = self.gh.get_issue(n)
        st = self.store.issue(n)
        if issue_parser.body_hash(data.get("body") or "") == st.body_hash:
            return True
        issue = Issue.from_api(data)
        self.plan.issues[n] = issue
        self.plan.specs[n] = issue_parser.parse(issue)
        return False

    def _lookup_issue(self, m: int | None) -> gate.OpenIssue | None:
        if not m:
            return None
        if m in self.plan.issues:
            i = self.plan.issues[m]
        else:
            data = self._safe(self.gh.get_issue, m)
            if not data:
                return None
            i = Issue.from_api(data)
        return gate.OpenIssue(number=i.number, state=i.state, title=i.title, body=i.body,
                              tracking=i.sub_issue_total > 0 or m in self.plan.tracking)

    def _absorb_agent_commits(self, n: int, wt: Path, call: AgentCall, merge_head: str | None = None) -> None:
        """Only the engine creates history. The agent ran against a private git directory
        (`Workspace.agent_call`): its working tree is kept as it is, and is committed, verified and
        examined like everything else; the events say what was dropped."""
        if call.after_branch != call.branch:
            self.store.event("agent_checkout_undone", issue=n, was=call.after_branch or call.after_head,
                             branch=call.branch)
        if call.after_head and call.after_head != call.head:
            self.store.event("agent_commits_absorbed", issue=n, from_head=call.after_head, to=call.head)
        if call.branch:
            now = self.ws.git("rev-parse", "-q", "--verify", call.branch, check=False) or None
            if now != call.head:
                self.store.event("issue_branch_moved_externally", issue=n, branch=call.branch,
                                 **{"from": call.head, "to": now})
        if merge_head and call.after_merge_head is None and not call.merge_committed:
            self.ws.git("merge", "--quit", cwd=wt)
            self.store.event("agent_merge_dropped", issue=n, merge_head=merge_head)
        if call.created:
            self.store.event("agent_refs_deleted", issue=n, refs=call.created)
        if call.discarded:
            self.store.event("agent_ref_changes_discarded", issue=n, refs=call.discarded)

    def _delivery(self, st: IssueState, merged_head: str, reviewed: str | None = None) -> tuple[str, list[str] | None]:
        reviewed, repo = reviewed or st.certified_sha, self.ws.repo
        if not reviewed or merged_head == reviewed:
            return "same", []
        self.ws.git("fetch", "--quiet", self.ws.remote, merged_head, cwd=repo, check=False)
        if not self.ws.has_commit(repo, merged_head):
            return "later", None
        if not self.ws.is_ancestor(repo, reviewed, merged_head):
            return "stale", None
        # Unreviewed: what the later commits changed of the files this issue's change touches.
        touched = {f for f in self.ws.git("diff", "--name-only", reviewed, merged_head, cwd=repo).splitlines() if f}
        own = (set(self.ws.git("diff", "--name-only", st.base_sha, reviewed, cwd=repo).splitlines()) if st.base_sha
               else touched)
        return "later", sorted(touched & own)

    def _mounts(self, scratch: Path) -> sandbox.Mounts:
        """What a check command may see of the tree under judgement: that throwaway copy and its
        private git directory read-write, the shared object store read-only."""
        return sandbox.worktree_mounts(scratch, True, self.ws)

    def _decide(self, st: IssueState, entry: dict) -> None:
        st.decisions = [d for d in st.decisions if d.get("id") != entry.get("id")] + [entry]

    # ------------------------------------------------------------------ plan
    def _step_plan(self, n: int) -> None:
        st = self.store.issue(n)
        spec = self.plan.specs[n]
        wt = Path(st.worktree) if st.worktree and (Path(st.worktree) / ".git").exists() else None
        req = self._req(n, st.feedback)
        item_text = self._item_text(n)
        ledger_ids = [i.id for i in spec.ledger]
        res = self.agents.plan(self.op, req, wt)
        items = [asdict(i) for i in res.items]
        problems = gate.lint_plan(items, ledger_ids, item_text, self.lacks, self._lookup_issue)
        if problems:   # one free retry, then the engine makes it usable itself
            res = self.agents.plan(self.op, replace(req, feedback=req.feedback + problems), wt)
            items = [asdict(i) for i in res.items]
            problems = gate.lint_plan(items, ledger_ids, item_text, self.lacks, self._lookup_issue)
        st = self.store.issue(n)
        for d in res.decisions:
            self._decide(st, d)
        if problems:
            items, fixes = gate.repair_plan(items, ledger_ids, item_text)
            for d in fixes:
                self._decide(st, d)
            fixed = []
            for i in items:
                bad = gate.lint_plan([i], [i["id"]], item_text, self.lacks, self._lookup_issue)
                if bad and i["proof"] in ("owner", "deferred"):
                    i, d = gate.demote(i, "; ".join(bad))
                    self._decide(st, d)
                fixed.append(i)
            items = fixed
        plan = {"items": items, "notes": res.notes}
        st.plan = plan
        st.examinations = 0   # the examiner bound counts from the latest plan (`_examination_note`)
        self.store.put(st)
        objections = [asdict(o) for o in self.agents.triage(self.other, self._req(n), plan)]
        kept = [o for o in objections if gate.keep_objection(o, item_text, self._lookup_issue)]
        st = self.store.issue(n)
        st.plan = plan   # one canonical plan from here on: dependency handling edits st.plan in place
        feedback = []
        infra = self._infra(n)
        for o in kept:
            if o["kind"] == "classification":
                ruling = self.agents.arbitrate(self.other, self._req(n), {
                    "kind": "classification", "item": o["item"], "item_text": item_text.get(o["item"]),
                    "plan": next((i for i in items if i["id"] == o["item"]), None), "objection": o,
                    "lacks": self.lacks})
                if ruling.upheld and o.get("proof") in ("test", "command", "review", "owner", "deferred"):
                    item = next(i for i in plan["items"] if i["id"] == o["item"])
                    candidate = {**item, "proof": o["proof"], "capability": o.get("capability"),
                                 "depends_on": o.get("depends_on"), "depends_quote": o.get("depends_quote"),
                                 "agent_part": item.get("agent_part") or "the parts agents can do"}
                    if not gate.lint_plan([candidate], [o["item"]], item_text, self.lacks, self._lookup_issue):
                        plan["items"] = [candidate if i["id"] == o["item"] else i for i in plan["items"]]
                        self._decide(st, {"id": f"classification-{o['item']}", "choice": f"{o['item']} is {o['proof']}",
                                          "rationale": f"the arbiter upheld the examiner: {ruling.reason}"})
            elif o["kind"] == "dependency":
                self._depend(n, st, o["item"], o["depends_on"], o.get("depends_quote") or "")
            elif o["kind"] == "misreading":
                feedback.append(f"The examiner says the plan misreads {o['item']} (\"{o['quote']}\"): {o['text']} "
                                f"Accept or reject this reading, with a reason, in your decisions.")
            elif o["kind"] == "scenario" and infra:
                feedback.append(f"Failure scenario to cover: {o['text']}")
        for i in plan["items"]:
            if i["proof"] == "deferred" and i.get("depends_on"):
                self._depend(n, st, i["id"], int(i["depends_on"]), i.get("depends_quote") or "")
            if i["proof"] == "owner":
                self._obligate(st, {"item": i["id"], "kind": "capability", "detail": i.get("capability"),
                                    "agent_part": i.get("agent_part")})
        # A protected path is granted here only when the issue's own text names it (ADR 0001,
        # decision 10): the path itself, or a protected directory above what changes. An issue that
        # names `scripts/` or `tools/` has not named `scripts/check.sh` or `tools/hygiene/`. Any
        # other protected path needs a typed `protected_change` request, which the examiner
        # decides. Never the safety surface in a fault-repair run. Earlier grants stay, with their
        # provenance: a replan never takes away a path the branch was allowed to change.
        named = [p.rstrip("/") for p in issue_parser.declared_paths(self.plan.issues[n].body)]
        named = sorted({p for p in named if (is_protected(p) or is_protected(p + "/"))
                        and not (self.repair and is_safety_surface(p))})
        added = [p for p in named if not any(g["path"] == p for g in st.grants)]
        st.grants = st.grants + [{"path": p, "source": "issue", "protected": True} for p in added]
        if added:
            self._decide(st, {"id": "protected-paths", "choice": f"protected paths the issue names are granted: "
                                                                 f"{', '.join(added)}",
                              "rationale": "a protected path changes only when the issue names it or the examiner "
                                           "approves a `protected_change` request"})
        plan = st.plan
        retired = self._retire_uncommanded(st)
        if retired:
            feedback.append(f"These commands were retired, because the plan no longer proves their items by a "
                            f"command: {retired[:6]}.")
        st.feedback = feedback
        self.store.put(st)
        self.store.event("planned", issue=n, items={i["id"]: i["proof"] for i in plan["items"]},
                         objections=len(objections), kept=len(kept), problems=problems)
        if not scheduler.prerequisites_met(self.plan, self._phases(), n):
            self._set_phase(n, Phase.PENDING)    # a prerequisite was added: wait for it
        else:
            self._set_phase(n, Phase.PLANNED)

    def _obligate(self, st: IssueState, ob: dict) -> None:
        st.obligations = [o for o in st.obligations if o["item"] != ob["item"]] + [ob]

    def _depend(self, n: int, st: IssueState, item: str, m: int, quote: str) -> None:
        """An item needs another open issue. In this run it waits for it; an open issue of the
        run's epic is admitted when the launch allowed it; otherwise the item is a deferred
        obligation that reopens when that issue lands."""
        other = self._lookup_issue(m)
        if other is None or other.state != "open" or m == n:
            return
        edges = {k: set(v) for k, v in self.plan.deps.items()}
        edges.setdefault(n, set()).add(m)
        in_epic = m in self.plan.parent or m in self.plan.closed_work or m in self.plan.work
        try:
            if m in self.plan.work:
                graph.topological_order(set(self.plan.work), edges)
                self.plan.deps[n] = edges[n]
                self._wait_for(st, item, m)
                self._decide(st, {"id": f"depends-{item}", "choice": f"{item} waits for #{m}, in this run",
                                  "rationale": f"it needs #{m}: \"{quote}\""})
                self.store.event("dependency_added", issue=n, on=m, item=item, admitted=False)
                return
            if self.admit and in_epic and not other.tracking:
                data = self.gh.get_issue(m)
                issue = Issue.from_api(data)
                self.plan.issues[m] = issue
                self.plan.specs[m] = issue_parser.parse(issue)
                graph.topological_order(set(self.plan.work) | {m}, edges)
                self.plan.work.insert(0, m)
                self.plan.deps[n] = edges[n]
                self.plan.deps.setdefault(m, set())
                self._wait_for(st, item, m)
                self._decide(st, {"id": f"depends-{item}", "choice": f"#{m} joined the run; {item} waits for it",
                                  "rationale": f"it needs #{m}: \"{quote}\" (the launch allowed prerequisites from the epic)"})
                self.store.event("prerequisite_added", issue=n, on=m, item=item)
                return
        except graph.CycleError:
            self.store.event("dependency_cycle", issue=n, on=m, item=item)
        self._obligate(st, {"item": item, "kind": "deferred", "detail": f"#{m}", "issue": m})
        if st.plan:   # the item is owed, never proven or ticked here
            st.plan["items"] = [{**i, "proof": "deferred", "depends_on": m, "depends_quote": quote}
                                if i["id"] == item else i for i in st.plan["items"]]
        self._decide(st, {"id": f"depends-{item}", "choice": f"{item} is deferred to #{m}",
                          "rationale": f"it needs #{m} (\"{quote}\"), which this run does not build"})
        self.store.event("dependency_deferred", issue=n, on=m, item=item)

    def _wait_for(self, st: IssueState, item: str, m: int) -> None:
        """An item waiting on a prerequisite in the run: the edge is kept across runs, and the
        item is proven (by the examiner's review, unless a replan says otherwise) once it lands."""
        st.extra_deps = sorted(set(st.extra_deps) | {m})
        if st.plan:
            st.plan["items"] = [{**i, "proof": "review" if i["proof"] == "deferred" else i["proof"],
                                 "depends_on": m} if i["id"] == item else i for i in st.plan["items"]]

    def _restore_deps(self, n: int, st: IssueState) -> None:
        """Prerequisites found in earlier runs: back into the graph (admitted again when the launch
        allows it); one no longer reachable becomes a deferred obligation."""
        for m in list(st.extra_deps):
            if m in self.plan.work:
                self.plan.deps.setdefault(n, set()).add(m)
                continue
            other = self._lookup_issue(m)
            if other is None or other.state != "open":
                st.extra_deps = [x for x in st.extra_deps if x != m]
                continue
            item = next((i["id"] for i in self._items(st) if i.get("depends_on") == m), "")
            self._depend(n, st, item, m, "")

    # ------------------------------------------------------------------ build
    def _step_build(self, n: int) -> None:
        """Every round starts from current main: main is merged in first. On a conflict the merge
        stays in progress for the builder to resolve, and is concluded or aborted within this step."""
        st = self.store.issue(n)
        # set at startup (_reconcile); a prerequisite _depend admits mid-run has none yet and stops here
        assert st.branch is not None
        try:
            wt = self.ws.issue_worktree(n, st.branch)
        except BranchHeld:
            # Not an engine fault, and nothing the engine may undo: that worktree may hold work.
            # The issue waits, and each later run tries again.
            return self._wait(n, WAIT_CLAIM, f"the branch {st.branch} is checked out in another worktree of this "
                                             f"repository (`git worktree list`); the issue is built once it is free")
        if str(wt) != st.worktree:
            st.worktree = str(wt)
            self.store.put(st)
        if not self._restore_branch(n):
            return
        self._recover_worktree(n, wt)
        self._release_locks(n, wt)
        self.ws.fetch()
        sync: UpdateResult | None = None
        held = self._uncommittable(n, wt) if self.ws.uncommitted(wt) else []
        if held:
            # Work the standing checks refuse is never committed, so there is nothing to merge main
            # over: this round runs on the branch as it is, and main is merged in once it is fixed.
            self.store.event("update_postponed", issue=n, problems=[p[:300] for p in held])
            head = self.ws.head(wt)
            sync = UpdateResult("current", [], head, head)
        elif self.ws.uncommitted(wt) and not self.ws.is_ancestor(wt, self.ws.base_ref(), "HEAD"):
            sha = self._commit_work(n, wt, f"Checkpoint #{n} before merging main")
            self.store.event("checkpoint_before_update", issue=n, sha=sha)
        if not st.base_sha:
            self._set_phase(n, Phase.BUILD, base_sha=self.ws.git("merge-base", "HEAD", self.ws.base_ref(), cwd=wt))
        try:
            if sync is None:
                sync = self.ws.update(wt)
        except GitError:
            # main moved between the fetch above and update's own, and git refused to merge over
            # the uncommitted work: checkpoint it after all and merge again
            if not self.ws.uncommitted(wt):
                raise
            sha = self._commit_work(n, wt, f"Checkpoint #{n} before merging main")
            self.store.event("checkpoint_before_update", issue=n, sha=sha, retried=True)
            sync = self.ws.update(wt)
        try:
            self._build_round(n, wt, sync)
        finally:
            if self.ws.in_progress(wt):
                self.ws.abort_in_progress(wt)

    def _uncommittable(self, n: int, wt: Path) -> list[str]:
        """What the standing checks say of the worktree as it stands, before the engine commits it:
        their problems, or nothing. A change they refuse is not committed, so no commit of a loop
        branch carries what the hygiene scan stops, and a commit that is never made is never
        pushed. The scan runs in the sandbox, with the worktree read-only."""
        checks = standing_checks()
        if not checks:
            return []
        _, problems = verify.standing_checks(wt, checks, self.ws.head(wt), self.store.issue_dir(n) / "uncommitted",
                                             self.caps.check_timeout_s, self.check_env,
                                             sandbox.worktree_mounts(wt, False, self.ws))
        return problems

    def _history_scan(self, n: int, wt: Path, base: str, head: str, where: str) -> tuple[list[str], list[str]]:
        """Run the history checks on the commits `base..head` of the worktree's branch, in a
        throwaway copy of that head, in the sandbox. Returns (findings, problems): the output of
        each check that reported findings, and what any other failure says (a check that could
        not run proves nothing, so that head is not pushed either)."""
        checks = history_checks(f"{base}..{head}")
        if not checks:
            return [], []
        with self.ws.disposable(wt) as scratch:
            evidence, problems = verify.standing_checks(scratch, checks, head, self.store.issue_dir(n) / where,
                                                        self.caps.check_timeout_s, self.check_env, self._mounts(scratch))
        findings = history_findings(evidence)
        return findings, ([] if findings else problems)

    def _local_scan(self, wt: Path, base: str, head: str) -> tuple[list[str], list[str]] | None:
        """The scan with the owner's list of local values, immediately before a push of `head`:
        its tree, and the commits `base..head` the push would publish (`LOCAL_VALUE_CHECKS`). The
        list exists outside the sandbox only, so this runs in the engine's own process, as the
        owner, and executes nothing of the branch: the scanner and the allow-list of the checkout
        the engine runs from, from the engine's repository, reading the branch as git objects.

        None where there is no list: nothing is started. Else (places, problems). A place is
        `<path>: <rule>` and no more (`local_places`): what the scanner printed stays in this
        method's memory, because its excerpt surrounds the owner's value itself. A problem says, in
        the engine's own words, that a scan could not run; that head is not pushed either."""
        values = local_values_file(os.environ)
        checks = local_value_checks(base, head) if values is not None else ()
        if not checks:
            return None
        if self._own_hooks(wt) is None:
            return [], [("the scan with the owner's local values was not run: the engine's git takes its hooks from no "
                         "directory of the engine's own, so a push could not be held to the commit that was scanned")]
        env = {**os.environ, LOCAL_VALUES_ENV: str(values), "PYTHONIOENCODING": "utf-8"}
        places, problems = [], []
        for name, argv in checks:
            said = f"the scan with the owner's local values (`{name}`)"
            try:
                proc = self._outside(argv, env)
            except subprocess.TimeoutExpired:
                problems.append(f"{said} did not end within {self.caps.check_timeout_s} s")
                continue
            except OSError as e:
                problems.append(f"{said} could not be started ({type(e).__name__})")
                continue
            found = local_places(proc.stdout) if proc.returncode == HISTORY_FINDINGS_EXIT else []
            places += found
            if proc.returncode != 0 and not found:
                problems.append(f"{said} could not run (exit {proc.returncode}); the same command on the owner's "
                                f"machine shows why")
        return list(dict.fromkeys(places)), ([] if places else problems)

    def _outside(self, argv: tuple[str, ...], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """Run `argv` outside the sandbox, as the engine itself, from the engine's repository. Only
        the scan with the owner's local values comes here, with an argument vector the engine's
        configuration holds; nothing an agent wrote is ever started this way."""
        return subprocess.run(argv, cwd=self.ws.repo, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=self.caps.check_timeout_s, check=False)

    def _own_hooks(self, wt: Path) -> Path | None:
        """The directory git takes its hooks from in `wt`, when the engine's own environment names
        it (`sandbox.engine_git_env`, as in every run): a directory the engine owns, which no file
        of a repository or a worktree can replace. None otherwise."""
        scope, _, path = self.ws.git("config", "--show-scope", "--get", "core.hooksPath", cwd=wt, check=False).partition("\t")
        return Path(path) if scope == "command" and Path(path).is_absolute() and Path(path).is_dir() else None

    def _push(self, wt: Path, scanned: str | None) -> bool:
        """Push the branch of `wt` through the ship script. `scanned` is the head the scan with
        the owner's local values passed, None where there is no list: the push is then as it
        always was.

        Shipping fetches main once more and, if main moved since the scan, merges it or rebases
        onto it: a head, and commits, that scan never saw. So for the time of a scanned push the
        engine's hooks directory holds a `pre-push` of the engine's own, which lets git publish
        that one commit and nothing else. False: shipping made another head, and nothing was
        pushed; the caller has it verified and scanned like any new head.

        Every run of the repository has that same hooks directory, so a push, scanned or not,
        holds it from before the guard is written until it is removed (`_hooks_held`): the guard
        git meets is the one of the push it is making."""
        hooks = self._own_hooks(wt)
        if scanned and hooks is None:
            raise GitError("a scanned push needs the engine's own hooks directory")
        guard = hooks / "pre-push" if hooks else None
        with self._push_lock, self._hooks_held(hooks):
            try:
                if guard and scanned:
                    guard.write_text(PUSH_GUARD.replace("{only}", scanned))
                    guard.chmod(0o755)
                    if not os.access(guard, os.X_OK):   # git would skip it in silence
                        raise GitError("the engine's hooks directory cannot hold an executable hook")
                elif guard:
                    guard.unlink(missing_ok=True)   # one a stopped run left behind: no live run has it, the lock is held
                self.ws.push(wt)
            except MergeConflict:
                raise
            except GitError:
                if scanned and self.ws.head(wt) != scanned:
                    return False
                raise
            finally:
                if guard:
                    guard.unlink(missing_ok=True)
        return True

    @staticmethod
    @contextmanager
    def _hooks_held(hooks: Path | None) -> Generator[None]:
        """Hold the engine's hooks directory for one push: an flock on the directory itself, until
        the push has ended and its guard is gone. Every run of a repository is given the same
        directory, and a guard is one file in it, so without this a second run would replace the
        guard of a push under way, or remove it, and git would publish a head no scan saw. Each
        call opens its own file description, so two engines in one process exclude each other as
        two processes do, and the lock of a run that died is released with it. Without a hooks
        directory there is no guard, and nothing to hold."""
        if hooks is None:
            yield
            return
        try:
            fd = os.open(hooks, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as e:
            raise GitError(f"the engine's hooks directory cannot be held for a push ({type(e).__name__})") from e
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as e:
                raise GitError(f"the engine's hooks directory cannot be held for a push ({type(e).__name__})") from e
            yield
        finally:
            os.close(fd)   # which releases the lock

    def _history_refused(self, n: int, head: str, outputs: list[str], places: list[str] | None = None) -> None:
        """The branch's own commits carry what a history check refuses: a value that reached a
        commit, whatever the files say now, a commit message, or an author's address. No later
        commit takes it back, so no round can repair it and the builder is not asked to: the
        branch is abandoned unpushed, the issue ends `failed`, and its next attempt (a retry, or
        a new engine version) starts on a new branch from main, with the plan kept.

        `places`: the finding is the scan's with the owner's local values (`_local_scan`), already
        reduced to paths and rules. There is no output of it to journal."""
        st = self.store.issue(n)
        old, pr = st.branch, st.pr
        scan, owed = "the history scan", ""
        if places is not None:
            scan = "the scan with the owner's local values, before the push,"
            owed = (" No agent knows the owner's values: one reaches a branch through text the owner wrote, such as the "
                    "issue, and is corrected there before a retry.")
        else:
            places = history_places(outputs)
        said = ", ".join(places[:8]) + (f" and {len(places) - 8} more" if len(places) > 8 else "")
        self.store.event("history_refused", issue=n, branch=old, head=head, findings=places[:40],
                         output=[o[-1500:] for o in outputs])
        st.restarts += 1
        self._new_round(st, [(f"An earlier branch of this issue, `{old}`, was abandoned: its commits carry what the "
                              f"public-repository scan refuses ({said}). What reaches a commit stays in the branch's "
                              f"history whatever later commits do, so that branch is never pushed. This is a new branch "
                              f"from main: build the issue again here, and never write such a value into a file, not "
                              f"even for one round (use the documented placeholders).")], owner_edit=False)
        self.store.put(st)
        self._fail(n, "history", (
            f"the commits of `{old}` carry what {scan} refuses: a value that reached a commit, a commit "
            f"message or an author's address. No later commit can take it back, so the branch was abandoned at "
            f"`{head[:12]}`, this step pushed nothing of it, and the next attempt (`retry`, or a new engine version) "
            f"builds on a new branch from main, `{st.branch}`. Delete `{old}` once you have read it (`git branch -D`)"
            + (f", and close its pull request #{pr}" if pr else "") + f". Found: {said}.{owed}"))

    def _release_locks(self, n: int, wt: Path) -> None:
        """The loop no longer locks tests, but a branch an earlier engine worked on may hold
        acceptance-checks commits, and CI's loop guard (`scripts/check-loop-guard.sh`) refuses any
        later commit that changes what they locked. Before the builder works on such a branch, one
        acceptance-checks commit releases every file still locked there (`Loop-Unlocks`)."""
        locked = locked_on_branch(self.ws, wt)
        if locked:
            sha = self.ws.commit_unlocks(wt, f"Release the locked tests of #{n}", n, locked)
            self.store.event("locks_released", issue=n, sha=sha, files=locked)
        st = self.store.issue(n)
        pending = [r for r in st.requests if r.get("kind") == "legacy_unlock" and r.get("status") == "pending"]
        for r in pending:   # an earlier engine's checks, released with the rest
            r["status"], r["reason"] = "approved", "the builder takes over tests an earlier engine locked"
        if pending:
            self.store.put(st)

    def _build_round(self, n: int, wt: Path, sync: UpdateResult) -> None:
        st = self.store.issue(n)
        assert st.plan is not None   # an issue without one goes back to pending at startup
        feedback = list(st.feedback) + self._open_feedback(st)
        files = sync.files if sync.status == "conflict" else []
        if st.conflict_pending and not files:
            st.conflict_pending = False
            self.store.put(st)
        if sync.status == "merged":
            self.store.event("updated_from_main", issue=n, phase=Phase.BUILD.value, frm=sync.before,
                             to=self.ws.head(wt), main=sync.main)
        elif files:
            st.conflict_paths = sorted(set(st.conflict_paths) | set(files))
            st.conflict_pending = True
            self.store.event("conflict", issue=n, phase=Phase.BUILD.value, files=files)
            self.store.put(st)
            feedback.append(conflict_feedback(files))
        with self.ws.agent_call(wt) as call:
            result = self.agents.build(self.op, self._req(n, feedback), st.plan, wt, feedback)
        self._absorb_agent_commits(n, wt, call, merge_head=sync.main if files else None)
        unresolved = []
        if files:
            st = self.store.issue(n)
            st.conflict_pending = False
            self.store.put(st)
            unresolved = (files if self.ws.in_progress(wt) != "merge"
                          else [f for f in files if has_conflict_markers(wt / f)])
            if unresolved and result.status == "done":
                return self._back_to_build(n, wt, "conflict_unresolved", unresolved,
                                           f"Conflict markers are still in {unresolved}, so main's merge was aborted. "
                                           f"The next round merges main again: resolve every marker.")
            if unresolved:
                self.ws.abort_in_progress(wt)
        refused = self._uncommittable(n, wt)
        if refused:
            # Not committed, and the answer's commands and requests are not kept: the builder gets
            # the scanner's output and builds again on the same files. A merge of main in progress
            # is aborted, as for an unresolved conflict; the next clean round merges it again.
            if self.ws.in_progress(wt):
                self.ws.abort_in_progress(wt)
            st = self.store.issue(n)
            st.conflict_pending = False
            self.store.put(st)
            self.store.event("build_not_committed", issue=n, status=result.status, problems=[p[:600] for p in refused])
            return self._round_failed(n, refused)
        st = self.store.issue(n)
        requests = self._new_requests(n, st, result.requests)
        subject = (f"Merge main and build #{n}" if files and not unresolved else f"Build #{n}")
        sha = self._commit_work(n, wt, subject)
        st = self.store.issue(n)
        self._absorb_build(st, result, wt)
        self.store.put(st)
        self.store.event("built", issue=n, sha=sha, status=result.status, note=result.note[:600],
                         requests=[r["id"] for r in requests])
        if requests:
            self._decide_requests(n, wt)
            st = self.store.issue(n)
            if not scheduler.prerequisites_met(self.plan, self._phases(), n):
                self.store.event("waits_for_prerequisite", issue=n, on=sorted(self.plan.deps.get(n, ())))
                return self._set_phase(n, Phase.PENDING, feedback=self._request_feedback(
                    [r for r in st.requests if r["id"] in {q["id"] for q in requests}]))
        if result.status != "done":
            decided = [r for r in st.requests if r["id"] in {q["id"] for q in requests}]
            if any(r["status"] in ("approved", "ruled_for") for r in decided):
                return self._set_phase(n, Phase.BUILD, feedback=self._request_feedback(decided))
            return self._round_failed(n, [f"blocked: {result.note[:400]}" + (
                "" if requests else " — a blocked answer needs a typed request the other vendor can decide")])
        self._set_phase(n, Phase.VERIFY, head_sha=self.ws.head(wt), feedback=self._request_feedback(
            [r for r in st.requests if r["id"] in {q["id"] for q in requests}]))

    def _absorb_build(self, st: IssueState, result, wt: Path | None = None) -> None:
        """The builder's commands: an item it gives commands for runs those from now on."""
        proofs = {i["id"]: i["proof"] for i in self._items(st)}
        commanded = self._commanded_items(st)
        inside = (lambda rel: self._inside(wt, rel)) if wt is not None else (lambda rel: True)
        current: dict[str, list[dict]] = {}
        for c in st.commands:
            if c.get("author") == "builder":
                current.setdefault(c["criterion"], []).append(c)
        probes = [c for c in st.commands if c.get("author") == "examiner"]
        given: dict[str, list[dict]] = {}   # an item may need several commands; each one is kept
        dropped = []
        for c in result.commands:
            item = criterion_item(c.criterion, set(proofs))
            why = ("its criterion names no plan item" if item is None
                   else f"{item} is proven by {proofs[item]}, not by a command" if item not in commanded
                   else "it is empty" if not c.command
                   else "its cwd is outside the worktree" if not inside(c.cwd or ".") else None)
            if item is None or why:   # said to the builder, never dropped in silence (#24 lost every probe command this way)
                dropped.append(f"`{(c.command or '')[:120]}` (criterion {(c.criterion or '')[:80]!r}): {why}")
                continue
            given.setdefault(item, []).append(asdict(c) | {
                "criterion": item, "description": c.description or c.criterion,
                "author": "builder", "timeout_s": min(c.timeout_s or self.caps.check_timeout_s, self.caps.max_check_timeout_s)})
        if dropped:
            self.store.event("commands_dropped", issue=st.number, commands=dropped[:8])
        st.dropped = dropped
        current.update(given)
        st.commands = [e for entries in current.values() for e in entries] + probes
        self._retire_uncommanded(st)
        for d in result.decisions or []:
            self._decide(st, d)

    def _new_requests(self, n: int, st: IssueState, requests) -> list[dict]:
        added = []
        for r in requests:
            entry = asdict(r)
            entry["id"] = f"R{n}-{len(st.requests) + 1}"
            entry["status"] = "pending"
            st.requests.append(entry)
            added.append(entry)
        self.store.put(st)
        return added

    def _request_feedback(self, decided: list[dict]) -> list[str]:
        out = []
        for r in decided:
            verdict = {"approved": "approved", "ruled_for": "ruled in your favour"}.get(r["status"], "refused")
            out.append(f"Your {r['kind']} request {r['id']} ({r.get('detail', '')[:200]}) was {verdict}: {r.get('reason', '')[:400]}")
        return out

    def _decide_requests(self, n: int, wt: Path) -> None:
        """The other vendor decides the builder's typed requests; disputes go to the arbiter. The
        engine enforces its own rules first: no safety surface in a repair run, `owner` only for
        a capability the host lacks."""
        st = self.store.issue(n)
        pending = [r for r in st.requests if r.get("status") == "pending" and r.get("kind") != "legacy_unlock"]
        if not pending:
            return
        for r in pending:
            refusal = self._engine_refusal(st, r)
            if refusal:
                r["status"], r["reason"] = "refused", refusal
        disputes = [r for r in pending if r["status"] == "pending" and r["kind"] == "dispute"]
        others = [r for r in pending if r["status"] == "pending" and r["kind"] != "dispute"]
        self.store.put(st)
        if others:
            with self.ws.disposable(wt) as scratch:
                decisions = self.agents.decide(self.other, self._req(n), scratch, others)
            by_id = {d.request: d for d in decisions}
            for r in others:
                d = by_id.get(r["id"])
                if d is None:
                    r["status"], r["reason"] = "refused", "no decision was given; ask again with evidence"
                    continue
                r["status"], r["reason"] = ("approved" if d.approve else "refused"), d.reason
                if d.approve:
                    self._apply_request(n, st, r)
        for r in disputes:
            f = next((x for x in st.findings if x["id"] == r.get("finding")), None)
            if f is None:
                r["status"], r["reason"] = "refused", "no such open finding"
                continue
            with self.ws.disposable(wt) as scratch:
                ruling = self.agents.arbitrate(self.other, self._req(n), {
                    "kind": "finding", "finding": f, "item_text": self._item_text(n).get(f.get("item") or ""),
                    "invariants": list(self.invariants), "argument": r.get("detail"), "lacks": self.lacks,
                    "history": [x for x in st.findings if x.get("item") == f.get("item")][-5:]}, scratch)
            if ruling.upheld:
                r["status"], r["reason"] = "refused", f"the arbiter upheld the finding: {ruling.reason}"
            else:
                r["status"], r["reason"] = "ruled_for", f"the arbiter ruled the item does not require it: {ruling.reason}"
                f["status"], f["blocking"], f["reason"] = "closed", False, f"ruled out by the arbiter: {ruling.reason}"
                f.pop(VERIFIER_RUN, None)
        self.store.put(st)
        self.store.event("requests_decided", issue=n, decided={r["id"]: r["status"] for r in pending})

    def _engine_refusal(self, st: IssueState, r: dict) -> str | None:
        paths = r.get("paths") or []
        if r["kind"] in RETIRED_REQUESTS:   # kept by an earlier engine's run: nothing restricts it any more
            return (f"the loop no longer takes `{r['kind']}` requests: change the file, the test or the command "
                    f"directly; the examiner reviews it in the diff")
        if self.repair and any(is_safety_surface(p) for p in paths):
            return "a fault-repair run may never change the loop's safety surface"
        if r["kind"] == "reclassify":
            if r.get("capability") not in self.lacks:
                return f"`owner` is only for a capability this host lacks ({', '.join(self.lacks)})"
            if r.get("item") not in self._agent_items(st):
                return "only an agent item can be reclassified"
        if r["kind"] == "depends" and not r.get("depends_on"):
            return "name the issue it depends on"
        return None

    def _apply_request(self, n: int, st: IssueState, r: dict) -> None:
        kind = r["kind"]
        if kind == "protected_change":
            for p in r.get("paths") or []:
                st.grants.append({"path": p.rstrip("/"), "source": f"approved {r['id']}", "protected": True})
        elif kind == "reclassify":
            assert st.plan is not None   # requests come from a build, which needs the plan
            st.plan["items"] = [{**i, "proof": "owner", "capability": r.get("capability"),
                                 "agent_part": r.get("detail")} if i["id"] == r["item"] else i for i in st.plan["items"]]
            self._obligate(st, {"item": r["item"], "kind": "capability", "detail": r.get("capability"),
                                "agent_part": r.get("detail")})
        elif kind == "depends":
            self._depend(n, st, r.get("item") or "", int(r["depends_on"]), r.get("depends_quote") or "")
        self._decide(st, {"id": r["id"], "choice": f"{kind}: {r.get('detail', '')[:200]}",
                          "rationale": f"approved by {self.other}: {r.get('reason', '')[:300]}"})

    def _open_feedback(self, st: IssueState) -> list[str]:
        out = []
        for f in st.findings:
            if f.get("status") == "open":
                where = f"item {f['item']} (\"{f.get('quote')}\")" if f.get("item") else f"invariant: {f.get('invariant')}"
                repro = f.get("reproduction") or (f"bash {f['probe']}" if f.get("probe") else "")
                if f.get(PROBE_TEXT):   # the examiner's probe is in no tree: the verifier writes it into its copy
                    repro += (f" (with the examiner's probe at `{f['probe']}`, which is not in your worktree and is "
                              f"never committed; cover the defect with a behaviour test of your own: "
                              f"{str(f[PROBE_TEXT])[:RUN_OUTPUT]})")
                # What the engine's own run of the reproduction printed: #32's builder was told ten
                # times that a finding "still reproduces" and answered nine times that the command
                # passed in its worktree. It fails where the verifier runs it, and only this shows why.
                ran = f.get(VERIFIER_RUN)
                said = (f" The verifier ran that reproduction on a copy of `{str(ran.get('head_sha'))[:12]}`, in its own "
                        f"environment, which is where it has to pass, whatever it does in your worktree: "
                        f"{run_said(ran)}") if ran else ""
                out.append(f"Open finding {f['id']} on {where}: {f['title']}. Observed: {f['observed']}. "
                           f"Expected: {f['expected']}. Reproduce: {repro}. It closes when that passes, or dispute it "
                           f"with a `dispute` request if the item as written does not require it.{said}")
        for i, v in st.verdicts.items():
            if v.get("verdict") == "unmet":
                out.append(f"The examiner judges {i} unmet (\"{v.get('quote')}\"): {v.get('note', '')[:400]}")
        return out

    def _back_to_build(self, n: int, wt: Path, kind: str, files: list[str], note: str) -> None:
        self.ws.abort_in_progress(wt)
        st = self.store.issue(n)
        st.conflict_pending = False
        self.store.put(st)
        self.store.event(kind, issue=n, files=files)
        self._set_phase(n, Phase.BUILD, feedback=[note])

    def _conflict(self, n: int, wt: Path, files: list[str], phase: str) -> None:
        """Bringing the branch up to date with main hit conflicts: work for the builder, not a
        fault, and no round is charged (main moving is nobody's design fault)."""
        if self.ws.in_progress(wt) or self.ws.git("status", "--porcelain", cwd=wt):
            raise GitError(f"the worktree of #{n} is not clean after aborting the conflicted update")
        st = self.store.issue(n)
        st.conflict_pending = True
        st.conflict_paths = sorted(set(st.conflict_paths) | set(files))
        self.store.put(st)
        self.store.event("conflict", issue=n, phase=phase, files=files)
        self._set_phase(n, Phase.BUILD, feedback=[
            (f"Main moved on and conflicts with this branch in: {', '.join(files)}. The next round starts with main's "
             f"merge in progress; resolve the conflicts in those files.")])

    def _commit_work(self, n: int, wt: Path, subject: str) -> str | None:
        """Commit the builder's work, whatever it changed: the examiner reviews it in the diff."""
        return self.ws.commit_all(wt, subject, "implementation", n)

    # ------------------------------------------------------------------ verify
    def _step_verify(self, n: int) -> None:
        st = self.store.issue(n)
        assert st.worktree is not None   # verify follows a build, which records it
        wt = Path(st.worktree)
        if self._drop_probes(n, wt):
            st = self.store.issue(n)
        head = self.ws.head(wt)
        problems: list[str] = []
        notes: list[str] = []
        if st.dropped:
            items = sorted(self._agent_items(st) | {i["id"] for i in self._items(st) if i["proof"] == "owner"})
            notes.append(f"These commands were not kept, so they did not run: {st.dropped[:6]}. A command's "
                         f"`criterion` is exactly one plan item id ({', '.join(items)}); a command that "
                         f"proves several items is listed once for each.")
            st.dropped = []
        if notes:
            st.feedback = list(st.feedback) + notes
            self.store.put(st)
        # No examiner probe fills a gap: each test/command item needs a command of its own, or it
        # has no evidence on main.
        uncommanded = sorted(self._agent_items(st) - self._review_items(st)
                             - {c["criterion"] for c in st.commands if c.get("author") == "builder"})
        if uncommanded:
            problems.append(f"no command proves {', '.join(uncommanded)}; return at least one command per item")
        # Checks are agent-written commands: they run in a throwaway copy of the head on a private git
        # directory, so nothing they do reaches the worktree or another run's refs.
        # The standing checks look at the tree of this head; the history checks at every commit the
        # branch adds to main, which is what a push would publish.
        history = history_checks(f"{self.ws.git('rev-parse', self.ws.base_ref(), cwd=wt)}..{head}")
        with self.ws.disposable(wt) as scratch:
            ok, evidence, vproblems = verify.verify(scratch, self._specs(st), head, self.store.issue_dir(n),
                                                    self.caps.check_timeout_s, extra_env=self.check_env,
                                                    standing=standing_checks() + history, mounts=self._mounts(scratch))
        refused = history_findings(evidence)
        if refused:   # nothing the builder could do about it in a next round
            return self._history_refused(n, head, refused)
        problems += vproblems
        base = self.ws.git("merge-base", "HEAD", self.ws.base_ref(), cwd=wt)
        closed = self._close_findings(n, st, wt, evidence)
        st = self.store.issue(n)
        still = [f for f in st.findings if f.get("status") == "open"]
        # The builder may change any file: only a protected path without a grant, or the safety
        # surface in a fault-repair run, is a problem here.
        problems += gate.protected_problems(self.ws.changed_files(wt, base), st.grants, self.repair)
        problems += [f"open finding {f['id']} still reproduces: {f['title']}" for f in still]
        self.store.event("verified", issue=n, head=head, ok=ok and not problems, problems=problems, closed=closed)
        st.evidence, st.base_sha = evidence, base
        self.store.put(st)
        if not problems:
            carried = self._carried(n, st, wt, head)
            if carried is not None:
                if not any(e.get("issue") == n and e.get("kind") == "certificate_carried" and e.get("head") == head
                           for e in self.store.events()):   # one event per carry, a resumed one included
                    self.store.event("certificate_carried", issue=n, head=head, **carried)
                return self._set_phase(n, Phase.LAND, certified_sha=head, verified_sha=head, head_sha=head)
            nxt = Phase.LAND if head == st.certified_sha and not self._carry_lost(n, st, head) else Phase.EXAMINE
            return self._set_phase(n, nxt, verified_sha=head, head_sha=head)
        env = environment_problem(evidence)
        if env:
            return self._wait(n, WAIT_ENVIRONMENT, f"the host cannot run this issue's checks: {env}", resume=Phase.VERIFY)
        # The feedback here is what the build left for its builder: the decisions on its requests
        # (and the notes above). A failed verification used to replace it, so #32's builder never
        # learnt that its restated commands were refused, nor why, and restated them nine times.
        self._round_failed(n, problems, [f["id"] for f in still], notes=notes,
                           told=[f for f in st.feedback if f not in notes] + failed_output(evidence))

    def _carried(self, n: int, st: IssueState, wt: Path, head: str) -> dict | None:
        """{certified, main} when `head` may carry the certificate of the head it merged (ADR 0001,
        amendment 14): `head` is exactly `main` merged into the certified head without a conflict,
        whoever made the merge (the land step, or `scripts/ship.sh` at the push). That is two
        parents, the certified head first and a commit of `main` second, and the tree git's own
        merge of the two makes. A head that resolves a conflict is no such merge, nor is a rebase
        or a head with any other change: those are examined. So is a merged head whose
        verification has failed once (the journal's `verified` events). Read from git and the
        journal, so a run that resumes reaches the same answer."""
        certified = st.certified_sha
        if not certified or head == certified:
            return None
        # A merged head that once failed its verification has lost the right to carry: whatever
        # the builder changes next (a command, without a new commit), the examiner sees it.
        if any(e.get("issue") == n and e.get("kind") == "verified" and e.get("head") == head and not e.get("ok")
               for e in self.store.events()):
            return None
        parents = self.ws.git("rev-list", "--parents", "-n1", head, cwd=wt).split()[1:]
        if len(parents) != 2 or parents[0] != certified:
            return None
        main = parents[1]
        if not self.ws.is_ancestor(wt, main, self.ws.base_ref()):
            return None
        merge = subprocess.run(["git", "merge-tree", "--write-tree", certified, main], cwd=wt,
                               capture_output=True, text=True, check=False)
        tree = merge.stdout.split("\n", 1)[0].strip() if merge.returncode == 0 else ""
        if not tree or tree != self.ws.git("rev-parse", f"{head}^{{tree}}", cwd=wt):
            return None
        return {"certified": certified, "main": main}

    def _carry_lost(self, n: int, st: IssueState, head: str) -> bool:
        """True when `head` holds a carried certificate (a `certificate_carried` event names it) and
        a verification of it has failed since: its certificate no longer stands on the examiner's
        and the engine's word, since what the builder repaired next (a command, without a new
        commit) was never examined. Such a head goes to the examiner, as `_carried` refuses a
        carry to a head that failed before it. A head the examiner examined itself keeps its own
        certificate. Read from the journal, so a run that resumes reaches the same answer."""
        if st.examined_sha == head:
            return False
        events = [e for e in self.store.events() if e.get("issue") == n and e.get("head") == head]
        carried = [i for i, e in enumerate(events) if e.get("kind") == "certificate_carried"]
        return bool(carried) and any(e.get("kind") == "verified" and not e.get("ok") for e in events[carried[0]:])

    def _carried_note(self, st: IssueState, head: str) -> str:
        """What the pull request and its evidence comment say of a head that carries the
        certificate of another (`_carried`): the head the examiner examined, and who certified it,
        the examiner or the engine's examiner bound. Empty for a head that was itself examined.
        Read from the issue's state, which a run that adopts the issue keeps. The examiner is named
        by its role: the run that writes this may not be the run whose examiner certified."""
        examined = st.examined_sha
        if not head or not examined or examined == head:
            return ""
        bound = any(str(d.get("id", "")).startswith("examiner-bound-") and f"`{examined[:12]}`" in str(d.get("choice", ""))
                    for d in st.decisions)
        by = ("the engine certified it under the examiner bound, the examiner having withheld its certificate"
              if bound else "the examiner certified it")
        return (f"The examiner examined `{examined}` and {by}. This head `{head}` was not examined: it merges "
                f"main into that head without a conflict, so it carries its certificate (ADR 0001, amendment "
                f"14). It was verified again on this exact head, and the `required` check runs on it before "
                f"the merge.")

    def _pr_body(self, n: int, st: IssueState, head: str) -> str:
        """The pull request's body for `head`, whose examination section names the head the
        examiner examined when `head` carries its certificate."""
        carried = self._carried_note(st, head)
        # `examination` is passed only for a carried certificate: a head the examiner examined
        # renders the body exactly as before, through the same call as before.
        extra = {"examination": carried} if carried else {}
        return report.render_pr_body(n, self.plan.issues[n].title, st, self.op, self.other, **extra)

    def _render_evidence(self, n: int, records: list[dict], head: str, verdicts: dict) -> str:
        """The evidence comment of issue `n` for `head`, naming the head the examiner examined
        when `head` carries its certificate."""
        text, carried = report.render_evidence(n, records, head, verdicts), self._carried_note(self.store.issue(n), head)
        return f"{text}\n\n{carried}" if carried else text

    def _close_findings(self, n: int, st: IssueState, wt: Path, evidence: list[dict]) -> list[str]:
        """An open finding closes only when its reproduction passes on the new head (a probe's
        with the probe written into the verifier's copy)."""
        closed = []
        kept = False
        for f in st.findings:
            if f.get("status") != "open":
                continue
            if f.get("reproduction"):
                probe = (f["probe"], f[PROBE_TEXT]) if f.get("probe") and f.get(PROBE_TEXT) else None
                ran = self._reproduction(wt, f.get("reproduction"), f.get("cwd") or ".", probe)
                if run_verdict(ran) is False:
                    f["status"], f["reason"] = "closed", "its probe passes" if probe else "its reproduction passes"
                    f.pop(VERIFIER_RUN, None)
                    closed.append(f["id"])
                elif ran is not None:   # still open: the builder is given this run with the finding
                    f[VERIFIER_RUN] = run_record(ran)
                    kept = True
        if closed or kept:
            self.store.put(st)
        return closed

    def _reproduces(self, wt: Path, command: str | None, cwd: str, probe: tuple[str, str] | None) -> bool | None:
        """Run a reproduction (and an optional probe file) in a throwaway copy of the head: True
        when it fails (the finding holds), False when it passes, None when it cannot run."""
        return run_verdict(self._reproduction(wt, command, cwd, probe))

    def _reproduction(self, wt: Path, command: str | None, cwd: str, probe: tuple[str, str] | None) -> Evidence | None:
        """The verifier's run of a reproduction (and an optional probe file) in a throwaway copy of
        the head, or None when there is nothing to run or the probe's path is refused."""
        if not command:
            return None
        if probe and not self._inside(Path("/nonexistent-root"), probe[0]):
            self.store.event("probe_path_refused", path=str(probe[0])[:200])
            return None
        with self.ws.disposable(wt) as scratch:
            if probe:
                target = scratch / probe[0]
                if not self._inside(scratch, probe[0]):   # a symlink pointing out of the copy
                    self.store.event("probe_path_refused", path=str(probe[0])[:200])
                    return None
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(probe[1])
            spec = CheckSpec(criterion="repro", command=command, cwd=cwd)
            ev, _ = verify.run_check(scratch, spec, self.ws.head(wt), self.store.issue_dir(0) / "repro",
                                     self.caps.check_timeout_s, extra_env=self.check_env, mounts=self._mounts(scratch))
        return ev

    @staticmethod
    def _replace_file(target: Path, text: str) -> None:
        """Write a file the engine owns by replacing its directory entry with a freshly created one:
        never through a symlink or a hard link an agent left at that path."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".loop-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _inside(root: Path, rel: str) -> bool:
        """A probe path is repo-relative and stays inside `root` once resolved (symlinks included)."""
        p = Path(rel)
        if not rel or p.is_absolute() or ".." in p.parts or ".git" in p.parts:
            return False
        try:
            return (root / p).resolve().is_relative_to(root.resolve())
        except (OSError, RuntimeError):
            return False

    def _round_failed(self, n: int, problems: list[str], open_ids: list[str] | None = None,
                      unmet: list[str] | None = None, notes: list[str] | None = None,
                      told: list[str] | None = None) -> None:
        """The progress rule: a round is productive when what fails changes. Two unproductive
        rounds bring a replan on the branch, two more end the issue `failed`. Notes reach the
        builder but are not failures, so they stay out of the signature.

        `told` is what the next call is given after the failures: the decisions on the last
        build's requests, and what the verifier's failed runs printed. It is feedback and nothing
        else: a command's output changes from run to run and is nobody's to publish, so it is
        in no signature, no event of the round and no failure's detail."""
        st = self.store.issue(n)
        sig = gate.signature(problems, open_ids or [], unmet or [])
        st.unproductive = st.unproductive + 1 if sig == st.signature else 0
        st.signature = sig
        self.store.put(st)
        problems = list(notes or []) + problems
        self.store.event("round_failed", issue=n, unproductive=st.unproductive, problems=problems[:8])
        if st.unproductive >= self.caps.unproductive_fail:
            return self._fail(n, "no_progress", "the same failure repeated after a replan: " + "; ".join(problems)[:600])
        if st.unproductive >= self.caps.unproductive_replan and st.replans < self.caps.replans:
            st.replans += 1
            self.store.put(st)
            self.store.event("replan", issue=n, count=st.replans)
            return self._set_phase(n, Phase.PLAN, feedback=[
                "Two rounds changed nothing. Replan on your branch: the same failure keeps coming back. "
                "Keep what works; change the approach or the proof kinds (classification changes go through "
                "a request). The failures were: " + "; ".join(problems)[:1500]] + list(told or []))
        self._set_phase(n, Phase.BUILD, feedback=problems + [t for t in told or [] if t not in problems])

    # ------------------------------------------------------------------ examine
    def _step_examine(self, n: int) -> None:
        st = self.store.issue(n)
        assert st.worktree is not None and st.plan is not None and st.base_sha is not None   # set by plan and build
        wt = Path(st.worktree)
        head = self.ws.head(wt)
        if head != st.verified_sha:
            self.store.event("head_changed_before_examination", issue=n, verified=st.verified_sha, head=head)
            return self._set_phase(n, Phase.VERIFY)
        since = st.examined_sha
        memory = [f for f in st.findings if f.get("status") in ("open", "closed", "follow_up")]
        exam = self._examinations(n, st) + 1
        bounded = exam >= self.caps.examiner_bound
        feedback = [self._examination_note(n, exam)] + list(st.exam_feedback)
        with self.ws.disposable(wt) as scratch:   # whatever the examiner changes is discarded with its copy
            res = self.agents.examine(self.other, self._req(n, feedback), st.plan, scratch, st.base_sha, head,
                                      st.evidence, memory, [], since)
        st = self.store.issue(n)
        st.examinations = exam
        item_text = self._item_text(n)
        rnd = len({f.get("round") for f in st.findings}) + 1
        # A probe is a finding's reproduction with a file: only one a finding names is linted and
        # run, and it is never committed. The finding keeps its text, and the verifier writes it
        # into its own copy of each later head (`_close_findings`), so no probe reaches main.
        named = {f.probe for f in res.findings if f.probe}
        probes, failed, rejected = {}, {}, []
        for p in res.probes:
            if p.file not in named or p.file in probes:
                continue
            problem = self._probe_problem(p)
            if problem:
                rejected.append({"file": p.file, "reason": problem})
                continue
            probes[p.file] = p
            failed[p.file] = self._reproduces(wt, p.command, p.cwd, (p.file, p.content))
        new: list[Finding] = []
        runs: dict[str, dict] = {}   # the engine's run of a finding's own reproduction, when it failed
        for i, f in enumerate(res.findings):
            f.id = f"E{n}-{rnd}-{i + 1}"
            f.round = rnd
            if f.probe not in probes:   # refused or not given: the finding stands on its own reproduction
                f.probe = None
            if f.probe:
                reproduced = failed[f.probe]
                f.reproduction, f.cwd = probes[f.probe].command, probes[f.probe].cwd or "."
            else:
                ran = self._reproduction(wt, f.reproduction, f.cwd or ".", None)
                reproduced = run_verdict(ran)
                if ran is not None and reproduced:
                    runs[f.id] = run_record(ran)
            new.append(gate.classify(f, item_text, self.invariants, reproduced, self._agent_items(st),
                                     self._owner_items(st), {i["id"]: i.get("agent_part") or "" for i in self._items(st)}))
        # The examiner bound (ADR 0001, amendment 12): from this examination on, a finding that
        # names no safety invariant is a follow-up issue, however well it quotes an item. Several
        # issues spent their ceilings while every round closed its findings and each examination
        # raised new edge cases. Findings of earlier examinations still block until they close.
        bound = [f.id for f in new if bounded and f.blocking and not gate.invariant_ok(f.invariant, self.invariants)]
        for f in new:
            if f.id in bound:
                f.status, f.blocking = "follow_up", False
                f.reason = f"examination {exam}: past the examiner bound only a safety invariant blocks ({f.reason})"
        # An owner item is judged on its agent part alone, like a finding on one (`gate.classify`):
        # the words an `unmet` must quote are that part's. One that quotes anything else is about
        # what the owner still owes, which is an obligation and nothing the builder can change (#20
        # was sent back four times for one). It is no verdict: the examiner still owes one, and is
        # asked again below.
        parts = {i["id"]: i.get("agent_part") or "" for i in self._items(st) if i["id"] in self._agent_part_items(st)}
        accepted, unmet, missing = gate.review_verdicts(res.verdicts, {**item_text, **parts}, self._verdict_items(st))
        misjudged = sorted(i for i in missing if i in parts
                           and any(v.get("item") == i and v.get("verdict") == "unmet" for v in res.verdicts))
        for i, v in accepted.items():
            st.verdicts[i] = {**v, "head": head}
        st.findings += [asdict(f) | ({VERIFIER_RUN: runs[f.id]} if f.status == "open" and f.id in runs else {})
                        | ({PROBE_TEXT: probes[f.probe].content} if f.probe else {}) for f in new]
        st.examined_sha = head
        self.store.put(st)
        blocking = [f for f in new if f.blocking] + [Finding(**{k: v for k, v in f.items() if k in Finding.__dataclass_fields__})
                                                     for f in st.findings if f.get("status") == "open"
                                                     and f.get("round") != rnd]
        self.store.event("examined", issue=n, head=head, findings={f.id: f.status for f in new},
                         unmet=unmet, missing=missing, certify=res.certify, rejected=rejected, examination=exam,
                         **({"bounded": bound} if bound else {}), **({"owner_evidence": misjudged} if misjudged else {}))
        problems = []
        if unmet:
            problems += [f"{i} judged unmet by the examiner" for i in unmet]
        if blocking:
            problems += [f"blocking finding {f.id}: {f.title}" for f in blocking]
        if not res.certify and not problems and not missing:
            if not bounded:
                problems.append(f"the examiner withheld its certificate: {res.summary[:400]}")
            else:   # every item is met and nothing names an invariant: past the bound, that certifies
                self._decide(st, {"id": f"examiner-bound-{exam}",
                                  "choice": f"examination {exam} certified `{head[:12]}` without the examiner's certificate",
                                  "rationale": "past the examiner bound only an unmet item or a finding that names a "
                                               "safety invariant withholds a certificate; the examiner said: "
                                               + res.summary[:400]})
                self.store.put(st)
                self.store.event("certified_by_bound", issue=n, head=head, examination=exam, summary=res.summary[:400])
        if problems:
            bundled = [f for f in new if f.status == "follow_up"]
            if bundled:
                problems += [f"also worth fixing (not blocking): {f.title}" for f in bundled]
            self._set_phase(n, Phase.EXAMINE, exam_feedback=[], exam_retries=0)
            # As after a failed verification (#32): the decisions on the last build's requests
            # reach the next build. #85's refusals were dropped here, then again by its revival.
            return self._round_failed(n, problems, [f.id for f in blocking], unmet, told=list(st.feedback))
        # A verdict the examiner still owes is asked of it again, at most twice; it is not the
        # builder's failure. No probe is owed: the builder's tests prove the items.
        st = self.store.issue(n)
        owed = ([f"give a verdict on {', '.join(missing)}"] if missing else []) + (
            [f"judge {i} on its agent part alone (\"{parts[i][:300]}\"): `met` when that part is delivered on this "
             f"commit, whatever the owner still owes; `unmet` only with a quote of that part" for i in misjudged])
        if missing and st.exam_retries < 2:
            self.store.event("examination_incomplete", issue=n, missing=missing, retry=st.exam_retries + 1)
            return self._set_phase(n, Phase.VERIFY, exam_retries=st.exam_retries + 1, exam_feedback=[
                f"Your previous examination is incomplete: {'; '.join(owed)}."])
        if missing:
            return self._round_failed(n, [f"no verdict on {', '.join(missing)} after two requests"], [], missing)
        self._set_phase(n, Phase.VERIFY, certified_sha=head, feedback=[], exam_feedback=[], exam_retries=0)

    def _examinations(self, n: int, st: IssueState) -> int:
        """The examinations of `n` since its latest plan. A state an earlier engine wrote has no
        count (None): it is read from the journal, as the `examined` events after `n`'s last
        `planned` one, so an issue in flight keeps the examinations it already had."""
        if st.examinations is not None:
            return st.examinations
        count = 0
        for e in self.store.events():
            if e.get("issue") == n and e.get("kind") == "planned":
                count = 0
            elif e.get("issue") == n and e.get("kind") == "examined":
                count += 1
        return count

    def _examination_note(self, n: int, exam: int) -> str:
        """What the examiner is told of the examiner bound (`Caps.examiner_bound`): which
        examination this is, and what blocks in it."""
        bound = self.caps.examiner_bound
        said = f"This is examination {exam} of #{n} since its latest plan."
        if exam < bound:
            return (f"{said} A finding blocks when it quotes an item or names a safety invariant. From examination "
                    f"{bound} on, only a finding that names a safety invariant will block.")
        return (f"{said} From examination {bound} on, only a finding that names a safety invariant blocks: a "
                f"finding that names only an item becomes a follow-up issue and does not block. Certify this commit "
                f"once every item an agent must prove is met; withhold the certificate only with an `unmet` verdict "
                f"or a finding that names a safety invariant.")

    def _probe_problem(self, p) -> str | None:
        if not p.file or not p.command or not p.content:
            return "a probe needs a file, its content and the command that runs it"
        if Path(p.file).is_absolute() or ".." in Path(p.file).parts or ".git" in Path(p.file).parts:
            return "a probe file must be a repo-relative path outside .git"
        if Path(p.cwd or ".").is_absolute() or ".." in Path(p.cwd or ".").parts:
            return "a probe's directory must be inside the repository"
        if is_protected(p.file) or (self.repair and is_safety_surface(p.file)):
            return "a probe may not be written on a protected path"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / p.file
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(p.content)
            except OSError:
                # A path no file can be created at (a name longer than the file system takes, a
                # file where a directory is needed) is the examiner's mistake, not an engine fault.
                # The reason is the engine's own words: what the system said would carry the path
                # of the temporary directory into the run log.
                return "a probe file must be a path the file system can create"
            bad = verify.unconditional_failures(Path(tmp), [p.file])
        return f"it fails unconditionally: {bad[0]}" if bad else None

    def _step_land(self, n: int) -> None:
        try:
            st = self.store.issue(n)
            # set by startup and build, and only a certified head reaches land
            assert st.worktree is not None and st.branch is not None and st.certified_sha is not None
            wt = Path(st.worktree)
            if st.certified_sha != st.verified_sha:
                return self._set_phase(n, Phase.VERIFY)
            existing = self.gh.find_pr(st.branch)
            if existing and (existing.get("merged") or existing.get("merged_at")):
                merged_head = (existing.get("head") or {}).get("sha") or st.certified_sha
                relation, unreviewed = self._delivery(st, merged_head)
                if relation == "same":
                    self.store.event("merge_found_on_resume", issue=n, pr=existing["number"])
                    return self._merged(n, existing.get("merge_commit_sha"), pr=existing["number"])
                if relation == "later":
                    self.store.event("merged_externally", issue=n, pr=existing["number"], head=merged_head,
                                     certified=st.certified_sha, unreviewed=unreviewed)
                    if unreviewed != []:
                        st.verdicts = {}
                    return self._merged(n, existing.get("merge_commit_sha"), pr=existing["number"], verdicts=st.verdicts)
                self.store.event("stale_pr_ignored", issue=n, pr=existing["number"], head=merged_head)
            if not self._requirements_current(n):
                self.store.event("requirements_changed", issue=n, previous_phase=Phase.LAND.value)
                st = self.store.issue(n)
                self._owner_edit(st)
                st.body_hash = self.plan.specs[n].body_hash
                self.store.put(st)
                return self._set_phase(n, Phase.PENDING)
            self.ws.fetch()
            if self.ws.nothing_to_land(wt):
                self.store.event("nothing_to_land", issue=n, head=st.certified_sha)
                # no merge of its own: the commit that carries the change is the head of `main`
                return self._merged(n, self.ws.git("rev-parse", self.ws.base_ref(), cwd=wt) if self.deploy.contexts
                                    else None)
            record = self._owner_record(st)
            if record is not None and record.get("revert"):   # of a revert that an edit of the issue abandoned
                record = st.owner_landing = None
                self.store.put(st)
            if record is None:
                if st.probes or any(c.get("author") == "examiner" for c in st.commands):
                    return self._set_phase(n, Phase.VERIFY)   # an earlier engine's probes leave the branch first
                # Main may have moved since the certificate. Merging it makes a new head, which is
                # verified again on its exact head before it may land. Merged without a conflict, it
                # keeps the certificate of the head it merged (`_carried`; ADR 0001, amendment 14):
                # main's change was certified in its own issue, and CI's `required` still runs on
                # it. A conflict is the builder's work, and the head that resolves it is examined.
                sync = self.ws.update(wt)
                if sync.status == "conflict":
                    self.ws.abort_in_progress(wt)
                    return self._conflict(n, wt, sync.files, Phase.LAND.value)
                if sync.status == "merged":
                    self.store.event("updated_from_main", issue=n, phase=Phase.LAND.value, frm=sync.before,
                                     to=self.ws.head(wt), main=sync.main)
                    return self._set_phase(n, Phase.VERIFY)
            else:
                # A head the owner lands is not brought up to date: however far main moved, nothing is
                # verified or certified again while that head still merges into it. Only a conflict
                # is work: the builder's, on main's merge, as in an ordinary round.
                files = self._conflicts_with_main(wt, existing, st.certified_sha)
                if files:
                    record["conflict"] = True   # what the comment of the next certified head says of it
                    self.store.put(st)
                    self.store.event("owner_landing_conflict", issue=n, head=st.certified_sha, files=files,
                                     main=self.ws.git("rev-parse", self.ws.base_ref(), cwd=wt))
                    return self._conflict(n, wt, files, Phase.LAND.value)
            # The last look before anything leaves the machine: every commit this push would publish.
            # A head that reaches this point was verified with the same checks; this one holds
            # whatever state the run resumed from and whatever moved since. A scan that could not
            # run is a failed check like any other: a round for the builder, under the progress
            # rule, so it cannot go round between here and the verifier for ever.
            unpushed = self.ws.head(wt)
            main = self.ws.git("rev-parse", self.ws.base_ref(), cwd=wt)
            local = None
            if record is None or record.get("head") != unpushed:   # a head the owner was given was scanned then
                findings, unscanned = self._history_scan(n, wt, main, unpushed, "push")
                if findings:
                    return self._history_refused(n, unpushed, findings)
                if unscanned:
                    self.store.event("push_withheld", issue=n, head=unpushed, problems=[p[:600] for p in unscanned])
                    return self._round_failed(n, unscanned)
                # And the owner's own look, where the owner's list of local values exists: outside the
                # sandbox, with the scanner of the engine's checkout. A finding abandons the branch as
                # a finding in its history does; a scan that could not run withholds the push.
                local = self._local_scan(wt, main, unpushed)
                if local and local[0]:
                    return self._history_refused(n, unpushed, [], places=local[0])
                if local and local[1]:
                    self.store.event("push_withheld", issue=n, head=unpushed, problems=local[1])
                    return self._round_failed(n, local[1])
            # Before any push: do the commits it would publish change a workflow file? The engine
            # never pushes such a change, whatever identity it runs as (ADR 0001, amendment 2). The
            # branch is the owner's to push, and stays so once it was handed over.
            workflows = self._workflow_paths(wt, main, unpushed)
            if record is not None or workflows:
                if unpushed != st.certified_sha:
                    self.store.event("head_changed_after_certificate", issue=n, certified=st.certified_sha, head=unpushed)
                    return self._set_phase(n, Phase.VERIFY)
                return self._owner_lands(n, unpushed, workflows)
            try:
                pushed = self._push(wt, unpushed if local is not None else None)
            except MergeConflict as e:
                return self._conflict(n, wt, e.files, Phase.LAND.value)
            if not pushed:   # main moved after the scan: the head shipping made is verified and scanned first
                self.store.event("push_withheld", issue=n, head=unpushed, shipped=self.ws.head(wt), problems=[
                    "shipping made another head than the one scanned with the owner's local values; nothing was pushed"])
                return self._set_phase(n, Phase.VERIFY)
            head = self.ws.head(wt)
            if head != st.certified_sha:
                self.store.event("head_changed_after_certificate", issue=n, certified=st.certified_sha, head=head)
                return self._set_phase(n, Phase.VERIFY)
            self._open_pull(n, st, head)
            assert st.pr is not None   # recorded by `_open_pull`
            conclusion = self._wait_required(head)
            if conclusion != "success":
                return self._ci_failed(n, head, conclusion)
            try:
                merge = self.gh.merge_pr(st.pr, head)
            except GitHubError as e:
                again = self._safe(self.gh.find_pr, st.branch) or {}
                if (again.get("merged") or again.get("merged_at")) and (again.get("head") or {}).get("sha", head) == head:
                    self.store.event("merged", issue=n, pr=st.pr, head=head, note="the merge answer was lost")
                    return self._merged(n, again.get("merge_commit_sha"))
                # Why GitHub refused is read from the pull request's review decision, never from the
                # words of the refusal. With a review owed on this very head, what is missing is a
                # person: no refusal is counted and nothing is verified again. A pull request whose
                # head is another one was refused for that, and is handled as every other refusal.
                decision = ""
                if (again.get("head") or {}).get("sha", head) == head:
                    decision = self._safe(self._review_decision, st.pr) or ""
                if decision in REVIEW_OWED:
                    return self._owner_reviews(n, st.pr, head, decision)
                st = self.store.issue(n)
                st.merge_refusals += 1
                self.store.put(st)
                self.store.event("merge_refused", issue=n, error=str(e)[:300], count=st.merge_refusals)
                if st.merge_refusals >= self.caps.merge_refusals:
                    return self._wait(n, WAIT_GITHUB, f"GitHub refused the merge {st.merge_refusals} times: {str(e)[:300]}",
                                      resume=Phase.LAND)
                return self._set_phase(n, Phase.VERIFY)
            self.store.event("merged", issue=n, pr=st.pr, head=head)
            self._merged(n, (merge or {}).get("sha"))
        finally:
            self.claims.release(MERGE_QUEUE, self.owner)

    def _review_decision(self, pr: int) -> str:
        """The review decision of pull request `pr` (`reviewDecision` in GitHub's GraphQL API):
        APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED, or empty where GitHub gives none, and where the
        client cannot read one (an older `github.py`)."""
        read = getattr(self.gh, "review_decision", None)
        return (read(pr) or "") if read else ""

    def _say_review_owed(self, pr: int, head: str, decision: str) -> None:
        """Say on pull request `pr`, once for the certified head `head`, that the loop waits for the
        owner's review. The comment is found again by its marker, which carries the head: a second
        refusal, or a resumed run, writes no second one, and a later certified head gets its own.
        Where the comments cannot be listed, none is written."""
        marker = OWNER_REVIEW_MARKER.format(head)
        comments = self._safe(self.gh.list_comments, pr)
        if comments is None or any(marker in (c.get("body") or "") for c in comments):
            return
        self._safe(self.gh.create_comment, pr, (
            f"{marker}\n**implement loop**: this pull request waits for the owner's review. Its head `{head}` is "
            f"certified by {self.other} and the required check is green on it, and GitHub refused the merge while the "
            f"pull request's review decision is `{decision}`. Nothing is verified again meanwhile. Once the pull request "
            f"is approved, the loop merges this head at its next run. A push to this branch makes another head, which "
            f"the loop verifies and has certified again before it lands.\n"))

    def _owner_reviews(self, n: int, pr: int, head: str, decision: str) -> None:
        """GitHub refused to merge the certified head `head` of issue `n`, on which the required
        check is green, and the review decision of its pull request `pr` owes a review
        (`REVIEW_OWED`): a pull request that touches a protected path needs the owner's approving
        review (ADR 0001, amendment 3). That is no merge refusal to count and nothing to verify
        again: the pull request says so, once, and the issue waits (`WAIT_OWNER_REVIEW`) with no
        landing claim, the land step that calls this releasing the merge queue. `_maybe_resume`
        reads the decision again at each run (`_review_owed`) and resumes the land step only
        once it is APPROVED, or the pull request is no longer open at this head."""
        self.store.event("owner_review_owed", issue=n, pr=pr, head=head, decision=decision)
        self._say_review_owed(pr, head, decision)
        self._wait(n, WAIT_OWNER_REVIEW, f"pull request #{pr} is certified at `{head[:12]}` with the required check green, "
                                         f"and GitHub merges it only with the owner's approving review (review decision: "
                                         f"{decision})", resume=Phase.LAND, pr=pr, head=head)

    def _open_pull(self, n: int, st: IssueState, head: str) -> dict:
        """Open the pull request of the issue's branch, or update the open one, and write its
        evidence comment for `head`: the same for a branch the engine pushed and for one the owner
        pushed (`_owner_lands`)."""
        assert st.branch is not None
        issue = self.plan.issues[n]
        body = self._pr_body(n, st, head)
        pr = self.gh.find_pr(st.branch)
        if pr is None or pr.get("state") != "open":
            pr = self.gh.create_pr(st.branch, self.cfg.base_branch, issue.title, body)
        else:
            self.gh.update_pr(pr["number"], body)
        st.pr = pr["number"]
        self.store.put(st)
        self._upsert_comment(st.pr, report.EVIDENCE_MARKER, self._render_evidence(n, st.evidence, head, st.verdicts))
        return pr

    # ------------------------------------------------------------------ landed by the owner
    @staticmethod
    def _owner_record(st: IssueState) -> dict | None:
        """The record of a change the owner lands (`IssueState.owner_landing`), None when the
        issue has none."""
        return getattr(st, "owner_landing", None)   # an older model.py has no such field, and no such issue

    def _workflow_paths(self, wt: Path, base: str, head: str) -> list[str]:
        """The paths under `.github/workflows/` that the commits `base..head` change: what a push
        of `head` would publish, `base` being the head of main. Every commit is read by itself, so a
        workflow file changed in one commit and put back in a later one counts: both commits would
        be published. A merge commit counts for what differs from every parent, so merging a main
        that changed a workflow file is not a change of this branch; and the whole difference with
        main counts, so a merge that drops main's change of one is. Renames are not followed: both
        names are changed paths."""
        listed = self.ws.git("log", "--format=", "--name-only", "--no-renames", "-c", "-z", f"{base}..{head}",
                             cwd=wt, strip=False)
        listed += "\0" + self.ws.git("diff", "--name-only", "--no-renames", "-z", base, head, cwd=wt, strip=False)
        return sorted({f.strip("\n") for f in listed.split("\0") if f.strip("\n").startswith(WORKFLOWS)})

    def _conflicts_with_main(self, wt: Path, pr: dict | None, head: str | None) -> list[str]:
        """The files in which the head of `wt` no longer merges into main without a conflict; none
        while it does. Once the pull request is open at that head, GitHub's own answer is taken
        where its record carries one (`mergeable`). Before that, and to name the files, the merge
        is tried in the worktree and always undone: the worktree stays at its head, clean."""
        main = self.ws.git("rev-parse", self.ws.base_ref(), cwd=wt)
        if self.ws.is_ancestor(wt, main, "HEAD"):
            return []
        if (pr and pr.get("state") == "open" and pr.get("mergeable") is True
                and (pr.get("head") or {}).get("sha") == head):
            return []
        self.ws.git("merge", "--no-commit", "--no-ff", main, cwd=wt, check=False)
        files = self.ws.conflicted_files(wt)
        if self.ws.abort_in_progress(wt) != "merge" or self.ws.git("status", "--porcelain", cwd=wt):
            raise GitError(f"the merge of {self.ws.base_ref()} could not be tried and undone in {wt}")
        return files

    def _remote_head(self, branch: str) -> str | None:
        """The head of `branch` on the remote, read from the remote itself; None when it has no such branch."""
        out = self.ws.git("ls-remote", "--heads", self.ws.remote, f"refs/heads/{branch}")
        return out.split()[0] if out else None

    def _hand_over(self, n: int, record: dict, text: str) -> None:
        """Post the comment that gives the owner the head of `record`: one comment per head, on the
        issue, found again by its marker, so a step that ends between the comment and its record
        writes no second one. `text` says what the head is; the rest is the same for every head."""
        branch, head = record["branch"], record["head"]
        marker = OWNER_LANDING_MARKER.format(head)
        changed = ", ".join(f"`{f}`" for f in record.get("paths") or []) or f"a file under `{WORKFLOWS}`"
        body = (f"{marker}\n**implement loop**: {text} Its commits change {changed}, and the loop never pushes a change "
                f"to a workflow file (ADR 0001, amendment 2). It is yours to land.\n\n"
                f"| | |\n|---|---|\n| Branch | `{branch}` |\n| Certified head | `{head}` |\n\n"
                f"Push the branch as it is, from the repository the loop runs in:\n\n"
                f"```sh\ngit push {self.ws.remote} {head}:refs/heads/{branch}\n```\n\n"
                f"The loop then opens the pull request, and you merge it: the loop does not merge this one. Do not bring "
                f"the branch up to date on GitHub (\"Update branch\"): the head that makes is not certified. If `main` "
                f"moves and no longer merges with this head, the loop says so here.\n")
        if self._upsert_comment(n, marker, body):
            record["announced"] = head

    def _owner_lands(self, n: int, head: str, workflows: list[str]) -> None:
        """The certified head `head` of issue `n` changes a workflow file, so the engine pushes
        nothing: the owner pushes the branch, the engine opens the pull request, the owner merges
        it. The branch and the head are recorded and given to the owner in one comment per head,
        and the issue waits (`WAIT_OWNER_LANDING`), with no landing claim: the land step that
        calls this releases the merge queue, and no deployment is awaited yet. Each resumed land
        step comes back here, until it finds the pull request merged (`_step_land`)."""
        st = self.store.issue(n)
        assert st.branch is not None
        record = self._owner_record(st)
        if record is None or record.get("head") != head:
            earlier = record or {}
            record = {"branch": st.branch, "head": head, "paths": workflows or earlier.get("paths") or [],
                      "announced": None, "published": None, "pr": earlier.get("pr"), "replaces": earlier.get("head"),
                      "conflict": bool(earlier.get("conflict"))}
            st.owner_landing = record
            self.store.put(st)
            self.store.event("owner_landing", issue=n, branch=st.branch, head=head, paths=record["paths"],
                             replaces=record["replaces"])
        if record.get("announced") != head:
            replaces = str(record.get("replaces") or "")
            if not replaces:
                text = f"this change is certified by {self.other}."
            else:
                why = ("`main` moved and no longer merged with the head posted before, so the loop merged `main` and "
                       "the builder resolved the conflict" if record.get("conflict") else
                       "the branch moved on from the head posted before")
                text = (f"{why}, and {self.other} certified the new head, which takes the place of "
                        f"`{replaces[:12]}`. It descends from that one: the push below moves the remote "
                        f"branch forward, and an open pull request follows it.")
            self._hand_over(n, record, text)
            self.store.put(st)
        detail = self._owner_pull(n, record, lambda: self._open_pull(n, st, head))
        self.store.put(st)
        self._wait(n, WAIT_OWNER_LANDING, detail, resume=Phase.LAND)

    def _owner_pull(self, n: int, record: dict, publish: Callable[[], dict]) -> str:
        """Where the owner's part of `record` stands, as the detail of the wait. While the remote
        has no such branch, or has it at another head than the certified one, nothing is opened.
        With the remote branch at the certified head, `publish` opens the pull request, once for
        a head; it is never merged from here. A pull request the owner closed is not opened again."""
        branch, head = record["branch"], record["head"]
        remote = self._remote_head(branch)
        if remote is None:
            return (f"the remote has no branch `{branch}`: the owner pushes the certified head `{head[:12]}` "
                    f"(the command is in the comment on this issue)")
        if remote != head:
            return (f"the remote branch `{branch}` is at `{remote[:12]}`, another head than the certified one, "
                    f"`{head[:12]}`, which the owner pushes (the command is in the comment on this issue); no pull "
                    f"request is opened for another head")
        pr = self.gh.find_pr(branch) or {}
        if record.get("published") == head and pr.get("number") == record.get("pr") and pr.get("state") != "open":
            return (f"pull request #{pr.get('number')} of `{branch}` is closed, and was not merged with the certified "
                    f"head `{head[:12]}`: the owner reopens and merges it")
        if record.get("published") != head or pr.get("state") != "open":
            pr = publish()
            record.update(published=head, pr=pr["number"])
            self.store.event("owner_landing_pull_request", issue=n, pr=pr["number"], branch=branch, head=head)
        return (f"pull request #{pr.get('number')} is open with the certified head `{head[:12]}` of `{branch}`: "
                f"the owner merges it")

    # ------------------------------------------------------------------ deploying
    def _merged(self, n: int, sha: str | None, **changes) -> None:
        """The issue's change is on `main` in the commit `sha`: its merge commit, or the head of
        `main` when the branch had nothing left to land. With deployment contexts configured, the
        verdict on that commit is awaited before the evidence step (ADR 0001, decision 5), by the
        step that merged: the merge queue is not released in between. With none, the evidence step
        is next, as before."""
        if self._owner_record(self.store.issue(n)) is not None:
            changes["owner_landing"] = None   # the owner landed it: from here it is an issue like any other
        if not self.deploy.contexts:
            return self._set_phase(n, Phase.MERGED, deploy=None, **changes)
        sha = sha or (self._safe(self.gh.find_pr, self.store.issue(n).branch) or {}).get("merge_commit_sha")
        if not sha:   # the next run finds the merged pull request, and its merge commit with it
            return self._wait(n, WAIT_GITHUB, "GitHub did not name the merge commit whose deployment is awaited",
                              resume=Phase.LAND)
        self._hold(n, sha)
        self._set_phase(n, Phase.MERGED, deploy={"sha": sha}, **changes)
        self._await_deployment(n)

    def _verdict_owed(self, st: IssueState) -> bool:
        """Merged, with contexts configured, and not every one of them has reported success on the
        merge commit yet: the evidence step is not reached."""
        if not self.deploy.contexts:
            return False
        d = st.deploy or {}
        return bool(d.get("sha") and not d.get("states"))

    def _step_deploying(self, n: int) -> None:
        """An issue resumed while its deployment verdict was owed (a wait, or a run that stopped):
        it holds the merge queue, like the land step it continues, and reads the statuses of the
        same merge commit again. Nothing is pushed or merged here."""
        try:
            if not self._verdict_owed(self.store.issue(n)):   # the contexts were emptied since
                return self._set_phase(n, Phase.MERGED)
            self._await_deployment(n)
        finally:
            self.claims.release(MERGE_QUEUE, self.owner)

    def _await_deployment(self, n: int) -> None:
        """Poll the configured contexts on the issue's merge commit, with the merge queue held by
        the calling step. Every context `success`: the verdict is recorded and the evidence step is
        next. One `failure` or `error`: the broken-main path, at once. One pending with the
        configured approval description, and none failed: the deployment waits for the owner, so
        the landing claim is released and the issue waits (`WAIT_OWNER_APPROVAL`), for as long as
        it takes. Otherwise, after `timeout_s` of polling, the issue waits (`WAIT_DEPLOY`) with the
        landing claim still held, and the next run polls the same commit again."""
        sha = (self.store.issue(n).deploy or {})["sha"]
        self._hold(n, sha)
        self._set_phase(n, DEPLOYING)
        waited = 0
        while True:
            try:
                statuses = {c: self.gh.deploy_status(sha, c) or {} for c in self.deploy.contexts}
            except GitHubError as e:   # no answer is no verdict: the next run asks again, the claim stays held
                return self._wait(n, WAIT_GITHUB, f"the commit statuses of {sha[:12]} could not be read: {str(e)[:300]}",
                                  resume=DEPLOYING)
            states = {c: s.get("state") for c, s in statuses.items()}
            failed = {c: state for c, state in states.items() if state in DEPLOY_FAILED}
            if failed:
                return self._deployment_failed(n, sha, failed)
            if all(state == "success" for state in states.values()):
                return self._deployed(n, sha, dict.fromkeys(states, "success"),
                                      {c: str(s.get("description") or "") for c, s in statuses.items()})
            approval = [c for c, s in statuses.items() if s.get("state") == "pending" and self.deploy.approval_description
                        and s.get("description") == self.deploy.approval_description]
            if approval:
                self._release_hold(n)
                return self._wait(n, WAIT_OWNER_APPROVAL, f"the deployment of {sha[:12]} waits for the owner's approval "
                                                          f"in the pipeline ({', '.join(approval)})", resume=DEPLOYING)
            if waited >= self.deploy.timeout_s:
                owed = ", ".join(f"{c} {'is ' + state if state else 'has no status'}" for c, state in states.items()
                                 if state != "success")
                return self._wait(n, WAIT_DEPLOY, f"no deployment verdict on {sha[:12]} after {self.deploy.timeout_s}s: "
                                                  f"{owed}", resume=DEPLOYING)
            if self.store.stop_requested():
                return   # it stays in `deploying`, and the next run polls the same commit
            self.sleep(self.caps.ci_poll_s)
            waited += max(self.caps.ci_poll_s, 1)

    def _deployed(self, n: int, sha: str, states: dict[str, str], descriptions: dict[str, str]) -> None:
        """Every context succeeded on `sha`. The verdict is recorded with the description each
        success carried when it was read: the evidence step compares that one, and reads no status
        again (`_verified_discharged`)."""
        st = self.store.issue(n)
        st.deploy = {"sha": sha, "states": states, "descriptions": descriptions}
        self.store.put(st)
        self.store.event("deployed", issue=n, sha=sha, contexts=states)
        self._release_hold(n)
        self._set_phase(n, Phase.MERGED)

    def _deployment_failed(self, n: int, sha: str, failed: dict[str, str]) -> None:
        """A final state that is not success: the merged change broke the deployment. The
        fix-forward gets the verdict (the commit, the failed contexts and their states) and then
        the summary of what the pipeline's reporter said, from `_deployment_summary`. If the
        statuses cannot be read for it, nothing is decided yet: the issue waits, and the next run
        reads the same commit again, so that no fix-forward is spent without the summary."""
        try:
            summary = self._deployment_summary(sha)
        except GitHubError as e:
            return self._wait(n, WAIT_GITHUB, f"the commit statuses of {sha[:12]} could not be read: {str(e)[:300]}",
                              resume=DEPLOYING)
        self.store.event("deployment_failed", issue=n, sha=sha, contexts=failed)
        self._release_hold(n)
        self.claims.release(MERGE_QUEUE, self.owner)   # the verdict is in; a revert takes the queue itself
        said = ", ".join(f"`{c}` is `{state}`" for c, state in failed.items())
        self._fix_forward(n, [(f"the deployment failed after the merge: on the merge commit {sha[:12]}, the commit "
                               f"status {said}. Find what in this issue's change broke the deployment, and repair it."),
                              *summary])

    def _deployment_summary(self, sha: str) -> list[str]:
        """What the pipeline's reporter said of the failed deployment of the commit `sha`, as
        feedback for a fix-forward: one entry per status. It is built from that commit's statuses
        and from nothing else: the engine reads no commit comment and no other text about a
        deployment (ADR 0001, amendment 4). Only the statuses of a configured context, or of one
        followed by a slash and more, count. The first kind is a stage's own status, whatever its
        state. The second kind is a fact: the reporter posts each fact of a failure under the
        context of its stage (system design, section 8.4). Any other context is another reporter's
        (`measure/...`), or one the owner did not name. Of each context the newest status counts.
        The entries come in the configured order of the stages, each stage's own status before its
        facts, the facts sorted by context, each with the context, the state and the description."""
        newest: dict[str, dict] = {}
        for status in self.gh.commit_statuses(sha):   # the oldest first: the last of a context is its newest
            newest[str(status.get("context") or "")] = status
        summary = []
        for configured in self.deploy.contexts:
            for context in sorted(c for c in newest if c == configured or c.startswith(configured + "/")):
                status = newest.pop(context)   # popped: a context below two configured ones is told once
                description = " ".join(str(status.get("description") or "").split())
                summary.append(f"the deployment's report on the merge commit {sha[:12]}: the commit status `{context}` is "
                               f"`{status.get('state')}`" + (f", with the description `{description}`" if description else ""))
        return summary

    def _deployment_evidence(self, st: IssueState) -> list[dict]:
        """The deployment verdict as an evidence record of the issue: the commit and each context
        with its state. It is no ledger item's proof and ticks no box."""
        d = (st.deploy or {}) if self.deploy.contexts else {}
        if not d.get("states"):
            return []
        note = f"commit statuses of `{d['sha'][:12]}`: " + ", ".join(f"`{c}` {state}" for c, state in d["states"].items())
        record = Evidence(criterion="deployment", kind="deployment", command=None, cwd=".", head_sha=d["sha"],
                          exit_code=None, passed=True, duration_s=0.0, output_tail="", output_sha256="", note=note,
                          verifier="commit statuses")
        return [asdict(record) | {"contexts": dict(d["states"])}]

    # The landing claim, where a verdict outlasts the run (`DEPLOY_HOLDS`).
    def _hold_path(self, n: int) -> Path:
        return self._holds_dir / f"{self.owner}-issue-{n}.json"

    def _hold(self, n: int, sha: str) -> None:
        """Record that issue `n` waits for the deployment verdict of `sha`. Written with the merge
        queue held, so a run that takes the queue and then finds no record may land."""
        self._holds_dir.mkdir(parents=True, exist_ok=True)
        path = self._hold_path(n)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"owner": self.owner, "issue": n, "sha": sha, "since": time.time()}))
        os.replace(tmp, path)

    def _release_hold(self, n: int) -> None:
        self._hold_path(n).unlink(missing_ok=True)

    def _holds(self, but: int | None = None) -> list[dict]:
        """The verdicts awaited, by any run; without the one of this run's issue `but`."""
        out = []
        for path in sorted(self._holds_dir.glob("*.json")) if self._holds_dir.is_dir() else []:
            try:
                hold = json.loads(path.read_text())
            except (OSError, ValueError):
                continue   # released between the listing and the reading
            if not (hold.get("owner") == self.owner and hold.get("issue") == but):
                out.append(hold)
        return out

    def _drop_stale_holds(self) -> None:
        """At the start of a run: a record of this run for an issue that no longer waits for a
        verdict (its state was archived, or the contexts were emptied) would stop every landing
        for good."""
        for hold in self._holds():
            n = hold.get("issue")
            if hold.get("owner") == self.owner and isinstance(n, int) and (
                    n not in self.plan.work or not self._verdict_owed(self.store.issue(n))):
                self._release_hold(n)
                self.store.event("deploy_hold_dropped", issue=n, sha=hold.get("sha"))

    def _take_queue(self, n: int, lands: bool = True) -> bool:
        """The merge queue, for issue `n`. To land, no other issue of any run may wait for a
        deployment verdict: that is the landing claim held through the wait. Records are written
        under the claim, so they are read again once it is taken."""
        if lands and self._holds(but=n):
            return False
        if not self.claims.acquire(MERGE_QUEUE, self.owner):
            return False
        if lands and self._holds(but=n):
            self.claims.release(MERGE_QUEUE, self.owner)
            return False
        return True

    def _unreleasable_hold(self, n: int, phases: dict[int, Phase] | None = None) -> dict | None:
        """A verdict that stops issue `n` from landing and that this run will not read again: the
        issue of this run that waits for it waits for the supervisor. Polling the queue in this
        run would not see it end."""
        phases = phases or self._phases()
        for hold in self._holds(but=n):
            if hold.get("owner") == self.owner and phases.get(hold.get("issue")) not in (Phase.MERGED, DEPLOYING):
                return hold
        return None

    def _ci_failed(self, n: int, head: str, conclusion: str | None) -> None:
        if conclusion in CI_NO_VERDICT:
            return self._wait(n, WAIT_CI, f"the required check gave no verdict on {head[:7]} ({conclusion})",
                              resume=Phase.LAND)
        log = self._safe(self.gh.failed_log, head, skip=self.cfg.required_check) or "(no log available)"
        self.store.event("ci_failed", issue=n, head=head, conclusion=conclusion)
        self._round_failed(n, [(f"The required CI check is '{conclusion}' on {head[:7]}. Find and fix the cause; "
                                f"never weaken or skip a check to get green. Tail of the failed log:\n"
                                f"{log[-3000:]}")])

    def _wait_required(self, sha: str) -> str:
        """The conclusion of the required check on `sha`: `pending` when it ran and gave none in
        time, `missing` when it never ran. A failed check is never run again: it goes to the
        builder with its log (`_ci_failed`)."""
        waited = 0
        while True:
            run = self.gh.required_check_run(sha, self.cfg.required_check)
            if run is not None and run["status"] == "completed" and run["conclusion"] not in (None, "pending"):
                return run["conclusion"]
            if waited >= self.caps.ci_timeout_s:
                return "pending" if run is not None else "missing"
            self.sleep(self.caps.ci_poll_s)
            waited += self.caps.ci_poll_s

    # ------------------------------------------------------------------ accept
    def _step_accept(self, n: int) -> None:
        if (self._owner_record(self.store.issue(n)) or {}).get("revert"):
            return self._owner_revert(n)   # main is still broken: no evidence step before its revert is merged
        if self._verdict_owed(self.store.issue(n)):
            # Never the evidence step with a deployment verdict owed, whatever brought the issue
            # here (a run killed right after the merge, a revert that waited for the queue).
            return self._set_phase(n, DEPLOYING)
        with self._accept_lock:
            self._accept(n)

    def _accept(self, n: int) -> None:
        st = self.store.issue(n)
        edited = ["the issue was edited after its work was merged; deliver the edited version"]
        if not self._requirements_current(n):
            return self._owner_round(n, edited)
        self.ws.coord()   # the worktree read-only agents work in follows `main`
        # The evidence is the trusted verifier's, on the head that landed (the land step merges only
        # a verified and certified head). Nothing runs again on `main`: CI runs every test there.
        # Only the verifier's own records: a second pass of this step finds the ones added below.
        evidence = [e for e in st.evidence if e.get("kind") == "command"]
        head = st.verified_sha or next((e["head_sha"] for e in evidence), "")
        verdicts = {i: v for i, v in st.verdicts.items() if v.get("verdict") == "met"}
        deployed = self._deployment_evidence(st)   # the pipeline's verdict on the merge commit, when one was awaited
        checked = self._ci_discharged(n, st)       # owner items a CI job proved on the head that was merged
        verified = self._verified_discharged(n, st)   # owner items the deployment's verification proved on the merge commit
        discharged = checked | verified
        records = evidence + deployed + list(discharged.values())
        evidence_comment = (self._upsert_comment(st.pr, report.EVIDENCE_MARKER,
                                                 self._render_evidence(n, records, head, verdicts))
                            if st.pr else None)
        link = (evidence_comment or {}).get("html_url", "")
        proven = {e["criterion"] for e in evidence if e["passed"]} | set(verdicts) | set(discharged)
        issue = self.gh.get_issue(n)
        if issue_parser.body_hash(issue.get("body") or "") != st.body_hash:
            return self._owner_round(n, edited)
        owed = self._owner_items(st) - set(discharged)   # never ticked by the loop
        exempt = owed - self._agent_part_items(st)       # an owner item's agent part must still be proven
        unproven = [i.id for i in issue_parser.ledger(issue.get("body") or "") if i.id not in proven and i.id not in exempt]
        if unproven:
            return self._fix_forward(n, [f"ledger items without evidence on the head that landed: {unproven}"])
        links = {cid: link for cid in proven if cid not in owed} | {i: e["address"] for i, e in checked.items()}
        ticked = issue_parser.tick(issue.get("body") or "", links)
        if ticked != (issue.get("body") or ""):
            self._safe(self.gh.edit_issue, n, body=ticked)
        st = self.store.issue(n)
        st.evidence = records
        st.restarts = 0
        st.obligations = [o for o in st.obligations if not (o["kind"] == "capability" and o["item"] in discharged)]
        st.human_tasks = [self._obligation_text(o) for o in st.obligations]
        # Each publication below ends this step when the hygiene scanner cannot mask it (`_safe`):
        # the issue waits in `merged` and this step runs again. So the follow-up, which is created
        # and not updated, comes last of the two and is recorded at once; and the comment that says
        # how the issue ends is written before anything that cannot be taken back.
        if discharged and st.pr:   # the pull request listed them as left for later
            self._safe(self.gh.update_pr, st.pr, self._pr_body(n, st, head))
        self._publish_deferred(n, st)
        self._file_followup(n, st)
        self.store.put(st)
        if checked:
            self.store.event("obligations_discharged", issue=n, head=next(iter(checked.values()))["head_sha"],
                             items={i: e["address"] for i, e in checked.items()})
        if verified:
            self.store.event("obligations_discharged", issue=n, head=next(iter(verified.values()))["head_sha"],
                             items={i: e["context"] for i, e in verified.items()})
        self._announce(n, st, Phase.DELIVERED if st.obligations else Phase.DONE)
        if st.worktree:
            self.ws.remove(Path(st.worktree))
        if st.obligations:
            return self._set_phase(n, Phase.DELIVERED)
        self._safe(self.gh.edit_issue, n, state="closed", state_reason="completed")
        self._set_phase(n, Phase.DONE)
        self._close_trackers(n)

    def _merged_pull(self, st: IssueState) -> dict:
        """The pull request this round merged, as GitHub records it. Empty when the round merged
        none (nothing was left to land) or GitHub does not show it merged."""
        if not st.pr or not st.branch:
            return {}
        pr = self._safe(self.gh.find_pr, st.branch) or {}
        return pr if pr.get("number") == st.pr and (pr.get("merged") or pr.get("merged_at")) else {}

    def _ci_discharged(self, n: int, st: IssueState) -> dict[str, dict]:
        """The owner items a CI job proves on the head that was merged (ADR 0001, amendment 5), each
        with its evidence record. An item is one when all of this holds: the plan gives it to the
        owner for a capability that `[host] ci_jobs` maps to a job; its text names a test under
        `tests/container/`; and the check run of that job's name on the merged pull request's head
        (the commit the required check was read on) is completed with the conclusion `success`.
        The record carries the check run's address (`address`): the page of that check run in the
        repository, `runs/<id>` beside the pull request's own page, since the client returns a
        check run's id and not its address. Anything less discharges nothing and the obligation
        stays the owner's: no mapping, no such test, no merged pull request with a head and an
        address, no check run, one that is not completed, was skipped or failed, one without an
        id, or a read GitHub did not answer.

        The plan is read, not `obligations`: this step runs again when a publication ends it, and
        the second pass, which finds the obligation already removed, records the same evidence."""
        texts = self._item_text(n)
        jobs = {i["id"]: self.ci_jobs[i["capability"]] for i in self._items(st)
                if i["proof"] == "owner" and i.get("capability") in self.ci_jobs
                and CONTAINER_TEST.search(texts.get(i["id"], ""))}
        pr = self._merged_pull(st) if jobs else {}
        head = (pr.get("head") or {}).get("sha")
        home = PULL_ADDRESS.match(pr.get("html_url") or "")
        if not head or not home:
            return {}
        runs = {job: self._safe(self.gh.required_check_run, head, job) or {} for job in sorted(set(jobs.values()))}
        out = {}
        for item, job in jobs.items():
            run = runs[job]
            if run.get("status") != "completed" or run.get("conclusion") != "success" or not run.get("id"):
                continue
            address = f"{home.group(1)}/runs/{run['id']}"
            record = Evidence(criterion=item, kind=CHECK_RUN_EVIDENCE, command=None, cwd=".", head_sha=head,
                              exit_code=None, passed=True, duration_s=0.0, output_tail="", output_sha256="",
                              note=f"the check run `{job}` succeeded on the merged head `{head[:12]}`: {address}",
                              verifier="check run")
            out[item] = asdict(record) | {"address": address, "check": job}
        return out

    def _verified_discharged(self, n: int, st: IssueState) -> dict[str, dict]:
        """The owner items a verified deployment proves (ADR 0001, decision 6, Consequences), each
        with its evidence record. The pipeline's verification runs the smoke tests of the commit it
        deployed against the deployed endpoint, all but those `[deploy] unverified_tests` names, so
        its success proves an item when all of this holds: `[deploy] verified_description` is set,
        and `unverified_tests` says which test files the verification leaves a test out of; a
        configured context of the form `verify/<stage>` succeeded on the issue's merge commit with
        exactly that description, as the deploying phase read it (`_deployed`); the plan gives the
        item to the owner for `aws:deployed-environment`, and its text ends with no other
        capability; and its text names a smoke test (`SMOKE_TEST`), every one of which is a file of
        that commit and none of which is among `unverified_tests`. The record names the commit, the
        context and the description; the item's box is ticked with the evidence comment, which
        carries that row. Anything less discharges nothing and the obligation stays the owner's:
        no description configured, a table that does not say what the verification leaves out, no
        verification context, a success with another description (a verification that ran no
        test, a parked system), a verdict recorded without descriptions, another capability, no
        smoke test named, one the commit does not hold, or one the verification leaves a test out
        of. No status is read here, and this step gives the same answer when it runs again."""
        # An older config.py reads neither key into its table (this engine.py beside the `implement_loop` of
        # the engine that runs the loop, as the verifier loads it): nothing is said, so nothing is discharged.
        said = getattr(self.deploy, "verified_description", "")
        left_out = getattr(self.deploy, "unverified_tests", None)
        if left_out is None:
            return {}
        d = (st.deploy or {}) if self.deploy.contexts and said else {}
        states, descriptions = d.get("states") or {}, d.get("descriptions") or {}
        context = next((c for c in self.deploy.contexts if VERIFY_CONTEXT.fullmatch(c) and states.get(c) == "success"
                        and descriptions.get(c) == said), None)
        if not context:
            return {}
        sha = d["sha"]
        return {item: self._status_record(item, tests, sha, context, said, f"the merge commit `{sha[:12]}`")
                for item, tests in self._smoke_items(n, st, left_out).items() if self._holds_tests(sha, tests)}

    def _smoke_items(self, n: int, st: IssueState, left_out: tuple[str, ...]) -> dict[str, list[str]]:
        """The items of issue `n` a verification's success can prove, whatever the commit, each with
        the smoke tests its text names: the plan gives it to the owner for `aws:deployed-environment`,
        its text ends with no other capability, and it names a smoke test (`SMOKE_TEST`), none of
        which is among `left_out` (`[deploy] unverified_tests`)."""
        texts, out = self._item_text(n), {}
        unverified = {SMOKE_TEST.sub(r"\1", t) for t in left_out}   # as an item's text is read: no leading `./`
        for i in self._items(st):
            text = texts.get(i["id"], "")
            needs = NEEDS_END.search(text)
            tests = sorted({m.group(1) for m in SMOKE_TEST.finditer(text)})
            if (i["proof"] == "owner" and i.get("capability") == DEPLOYED_ENVIRONMENT and tests
                    and not (needs and needs.group(1) != DEPLOYED_ENVIRONMENT) and not unverified & set(tests)):
                out[i["id"]] = tests
        return out

    def _holds_tests(self, sha: str, tests: list[str]) -> bool:
        """Whether each of `tests` is a regular file of the commit `sha`: not a directory of that
        name, which `ls-tree --name-only` would list as well, and not a symbolic link, which git
        lists as a blob too and which may point at nothing. The entry is read NUL-terminated
        (`-z`), so a path is compared as it is and not as git quotes it for display (`core.quotePath`)."""
        for t in tests:
            entries = self.ws.git("ls-tree", "-z", sha, "--", t, check=False, strip=False).split("\0")
            meta, _, path = entries[0].partition("\t")
            if path != t or meta.split(" ")[:2] not in (["100644", "blob"], ["100755", "blob"]):
                return False
        return True

    @staticmethod
    def _status_record(item: str, tests: list[str], sha: str, context: str, said: str, where: str) -> dict:
        """The evidence record of an item the success of `context` on the commit `sha` proved."""
        record = Evidence(criterion=item, kind=COMMIT_STATUS_EVIDENCE, command=None, cwd=".", head_sha=sha,
                          exit_code=None, passed=True, duration_s=0.0, output_tail="", output_sha256="",
                          note=(f"the commit status `{context}` succeeded on {where} with the description "
                                f"`{said}`: the verification ran {', '.join(f'`{t}`' for t in tests)}"),
                          verifier="commit status")
        return asdict(record) | {"context": context, "description": said}

    def _discharge_table(self) -> tuple[tuple[str, ...], str, tuple[str, ...] | None]:
        """`[deploy] discharge_contexts`, `verified_description` and `unverified_tests`, read with
        their defaults, as `_verified_discharged` reads its keys: an older config.py discharges nothing."""
        return (tuple(getattr(self.deploy, "discharge_contexts", ()) or ()),
                getattr(self.deploy, "verified_description", ""), getattr(self.deploy, "unverified_tests", None))

    def _owed_smoke_items(self, n: int, st: IssueState) -> dict[str, list[str]]:
        """The items of issue `n` a later verification can discharge: those `_smoke_items` accepts
        that still hold an obligation for `aws:deployed-environment`. None when the table does not
        configure a discharge."""
        contexts, said, left_out = self._discharge_table()
        if not contexts or not said or left_out is None:
            return {}
        owed = {o["item"] for o in st.obligations
                if o.get("kind") == "capability" and o.get("detail") == DEPLOYED_ENVIRONMENT}
        return {i: tests for i, tests in self._smoke_items(n, st, left_out).items() if i in owed}

    @staticmethod
    def _unrecorded(st: IssueState) -> set[str]:
        """The items of which the issue's records hold a proof a discharge saved before writing it
        (`PUBLISHING`) and never recorded, while their obligation is still owed."""
        owed = {o["item"] for o in st.obligations if o.get("kind") == "capability"}
        return {e["criterion"] for e in st.evidence
                if e.get("kind") == COMMIT_STATUS_EVIDENCE and e.get(PUBLISHING) and e.get("criterion") in owed}

    def _kept_proofs(self, st: IssueState) -> dict[str, dict]:
        """The proofs a discharge saved for the issue's pull request and did not record: a write
        GitHub did not answer, or applied without its reply. Until the next discharge settles them,
        whatever the items it can still discharge, the issue is not closed (`_reconcile_delivered`):
        one GitHub published, its row in the evidence comment and its box ticked with the link to
        it (`_published`), is recorded, whatever the statuses and the table then say; any other is
        dropped, its row withdrawn, and the statuses of then are read again."""
        unpublished = self._unrecorded(st)
        return {e["criterion"]: e for e in st.evidence
                if e.get("kind") == COMMIT_STATUS_EVIDENCE and e.get("passed") and e.get("criterion") in unpublished
                and e.get(PUBLISHING) and st.pr and e.get("pr") == st.pr}

    def _discharge_delivered(self) -> None:
        """The owner items a later verification proves (ADR 0001, decision 6), after the fact: on
        each run, for each delivered issue of the plan that holds an obligation for
        `aws:deployed-environment` on an item `_smoke_items` accepts, the status of each context of
        `[deploy] discharge_contexts` is read on the issue's merge commit (its pull request's, as
        GitHub records it), and, for each context that is not a success there with exactly
        `verified_description`, on the head of `main` this run fetched, if that commit's history
        holds the merge commit. An item whose smoke tests are all regular files of a commit that
        carries such a success is proven: its evidence record (kind `commit_status`) joins the
        issue's, its row the evidence comment of the pull request, its box is ticked with a link to
        that comment, its obligation is removed, and the event `obligations_discharged` journals it;
        `_reconcile_delivered` then closes the issue once every box is ticked. Nothing here enters
        the deploying phase, holds the merge queue, or waits: what is not there yet is read again by
        the next run. Anything less discharges nothing and the obligation stays the owner's: no
        context, no description, a table that does not say what the verification leaves out (an
        older config.py reads none of the three keys), no merged pull request with a merge commit,
        an issue edited since it was delivered (its items are not the ones proven), or a read
        GitHub does not answer, which the next run tries again. A proof read before a write GitHub
        did not answer is recorded by the next run only if GitHub ticked its box; otherwise it is
        read again (`_kept_proofs`). Such a proof is settled even once its issue is no longer this
        run's work (another root's run closed it meanwhile): a write of the evidence comment GitHub
        applied late, without its reply, may have erased the rows that run published, which the
        settling writes again; nothing new is proven for such an issue."""
        contexts, said, _ = self._discharge_table()
        head: list[str] = []   # the head of `main`, fetched once, when an issue needs it
        for n in [*self.plan.work, *(m for m in self.store.known() if m not in self.plan.work)]:
            st = self.store.issue(n)
            if st.phase != Phase.DELIVERED:
                continue
            if n not in self.plan.work:
                items = {}   # only what this run saved and left unsettled
            elif self.plan.specs[n].body_hash != st.body_hash:
                continue   # an edited issue is not discharged: its items are no longer the ones delivered
            else:
                items = self._owed_smoke_items(n, st)
            if not items and not self._kept_proofs(st):   # a proof saved and not recorded is settled all the same
                continue
            try:
                self._discharge_later(n, items, contexts, said, head)
            except (GitHubError, GitError, OSError) as e:   # with RedactionUnavailable: nothing of it was published
                self.store.event("discharge_unread", issue=n, error=str(e)[:300])

    def _discharge_later(self, n: int, items: dict[str, list[str]], contexts: tuple[str, ...], said: str,
                         head: list[str]) -> None:
        """`_discharge_delivered` for issue `n`, whose `items` it found. A read or a write GitHub
        does not answer is raised. The proofs are saved before anything is written (`PUBLISHING`),
        and marked failed when a write is not answered. A proof GitHub published without the reply
        of its tick, its own row in the evidence comment and its box ticked with the link to that
        comment (`_published`), is recorded by the next run, whatever the statuses then say. One
        whose box is so ticked while the comment's row of the item is another proof's was
        discharged by another run: it is dropped and that run's record adopted, never written over
        it, nor proven again. Any other was never published (an owner's tick carries no such link):
        the next run drops it, even if the item is no longer one it can discharge, writes the
        evidence comment again without it, in case its row was written (it stays saved, marked
        failed, until that write is answered, so the run after tries the withdrawal again), and reads the statuses of
        the contexts then configured again. The issue is read before and after the statuses: an
        issue edited meanwhile, whose items may no longer be the ones proven, is left as it is."""
        st = self.store.issue(n)
        body = self._as_delivered(n, st) if st.pr else None
        if body is None:
            return
        kept = self._kept_proofs(st)
        comment = self._evidence_comment(st.pr or 0) if kept else {}
        proven = {i: e for i, e in kept.items() if self._published(body, comment, e)}
        taken = {i for i in kept if i not in proven and self._linked(body, comment.get("html_url") or "", {i})}
        stale = self._unrecorded(st) - set(proven)

        def is_stale(e: dict) -> bool:
            return e.get("kind") == COMMIT_STATUS_EVIDENCE and bool(e.get(PUBLISHING)) and e.get("criterion") in stale
        # Never published by this run: proven again, by the statuses of now, or not at all. Each is
        # kept, marked failed, until a comment without its row is written: a write GitHub does not
        # answer leaves it for the next run, which withdraws its row again.
        keep = [e for e in st.evidence if not is_stale(e)]
        withdrawn = [{**e, PUBLISHING: "failed"} for e in st.evidence if is_stale(e)]
        todo = {i: tests for i, tests in items.items() if i not in proven and i not in taken}
        if todo:
            proven |= self._read_proofs(st, todo, contexts, said, head)
        if not proven and not stale:
            return
        if proven and self._as_delivered(n, st) is None:
            return
        # The verifier's records of an item's agent part stay: only a status record of the item is replaced.
        mine = {e.get("criterion") for e in keep if e.get("kind") == COMMIT_STATUS_EVIDENCE}
        base = [e for e in keep if not (e.get("kind") == COMMIT_STATUS_EVIDENCE and e.get("criterion") in proven)]
        writer = self.store.lock().holder()   # this invocation, as its run lock names it
        base += [{**e, PUBLISHING: "writing", WRITER: writer} for e in proven.values()]
        # Saved before the comment and the body are written, with the obligations still owed: a run of
        # another root that writes them meanwhile then finds the records (`_status_elsewhere`) once it
        # has written, and writes their rows and their ticks again.
        st.evidence = [*base, *(e for e in withdrawn if e["criterion"] not in proven)]
        self.store.put(st)
        recorded, done, adopted = False, set(), {}
        try:
            adopted, link = self._publish_status_rows(n, st, base, proven, mine)
            if not link:
                return
            done = {*proven, *adopted}
            st = self.store.issue(n)
            st.evidence = [{k: v for k, v in e.items() if k not in (PUBLISHING, WRITER)}
                           for e in [*base, *adopted.values()]]
            st.obligations = [o for o in st.obligations if not (o["kind"] == "capability" and o["item"] in done)]
            st.human_tasks = [self._obligation_text(o) for o in st.obligations]
            self.store.put(st)
            recorded = True
        finally:
            if not recorded:   # published in part or not at all: no other run adopts it unless GitHub published it
                st = self.store.issue(n)
                st.evidence = [{**e, PUBLISHING: "failed"} if e.get(PUBLISHING) == "writing" else e for e in st.evidence]
                self.store.put(st)
        if not done:   # a withdrawal alone
            return
        records = {**adopted, **proven}
        self.store.event("obligations_discharged", issue=n, head=next(iter(records.values()))["head_sha"],
                         items={i: e["context"] for i, e in proven.items()},
                         heads={i: e["head_sha"] for i, e in records.items()}, adopted=sorted(adopted))
        if st.obligations:   # it stays delivered: its progress comment no longer lists the items
            try:
                self._announce(n, st, Phase.DELIVERED)
            except RedactionUnavailable as e:
                self.store.event("progress_not_published", issue=n, phase=Phase.DELIVERED.value, error=str(e)[:300])

    def _read_proofs(self, st: IssueState, items: dict[str, list[str]], contexts: tuple[str, ...], said: str,
                     head: list[str]) -> dict[str, dict]:
        """The proofs of `items` the statuses of `contexts` give: on the merge commit of the issue's
        pull request, then, for each context not verified there, on the head of `main` (fetched
        once into `head`) if its history holds the merge commit."""
        get_pr = getattr(self.gh, "get_pr", None)   # by its number: a later pull request may reuse its branch
        pr = get_pr(st.pr) if get_pr else (self.gh.find_pr(st.branch) if st.branch else None)
        merge = (pr or {}).get("merge_commit_sha")
        if not pr or pr.get("number") != st.pr or not (pr.get("merged") or pr.get("merged_at")) or not merge:
            return {}

        def verified(sha: str, context: str) -> bool:
            s = self.gh.deploy_status(sha, context) or {}
            return VERIFY_CONTEXT.fullmatch(context) is not None and s.get("state") == "success" \
                and s.get("description") == said
        on_merge = [c for c in contexts if verified(merge, c)]
        candidates = [(merge, c, f"the merge commit `{merge[:12]}`") for c in on_merge]
        proven = self._first_proof(st.pr, items, candidates, said)
        rest = [c for c in contexts if c not in on_merge]
        if rest and len(proven) < len(items):   # each context not verified on the merge commit, on main
            if not head:   # the head of `main`, as this run fetched it
                self.ws.fetch()
                head.append(self.ws.git("rev-parse", self.ws.base_ref()))
            if head[0] != merge and self.ws.is_ancestor(self.ws.repo, merge, head[0]):
                where = f"`main` at `{head[0][:12]}`, whose history holds the merge commit `{merge[:12]}`"
                candidates += [(head[0], c, where) for c in rest if verified(head[0], c)]
                proven = self._first_proof(st.pr, items, candidates, said)
        return proven

    def _publish_status_rows(self, n: int, st: IssueState, base: list[dict], proven: dict[str, dict],
                             mine: set) -> tuple[dict[str, dict], str]:
        """Write the evidence comment of issue `n`'s pull request with the records `base` and the
        status records another root's run holds for it (`_status_elsewhere`), which are kept, never
        erased, then tick the boxes of `proven` and of those records: the records adopted, and the
        link of the comment, or no link when the issue was edited (nothing is then ticked). The other roots' records
        are read again once the body is written; one saved meanwhile, whose row or tick these
        writes may have erased, is adopted and both are written again. As each run saves its
        records before it writes, the last write of the comment and of the body holds every record
        saved before it, whichever run finishes last (`DISCHARGE_WRITES` bounds the rounds; past
        them, the next run tries again). A record another run saved and did not publish, its run
        having given up or ended, is adopted only if GitHub published it: its row in the evidence
        comment as read before the first write, and its box ticked with the link to that comment
        (`_published`), or, failing that, its box ticked with that link, which only a run that
        wrote its row in that comment does. So the first write keeps the row of a box already
        ticked so, even if its run never heard GitHub's reply, and a write in flight while another
        run published and ticked a row is written again with it; what that run never published is
        not proven by this one, nor by an owner's tick, which carries no link."""
        before = self._evidence_comment(st.pr or 0)   # what readers of the linked comment see now

        def elsewhere(body: str, link: str) -> dict[str, dict]:
            # Best first: a record published or being published; one whose row and linked tick GitHub
            # holds; one whose box alone is ticked with the link, which only a run that wrote its row
            # in this comment does, though a write of this run that was in flight may have erased it.
            found = self._status_elsewhere(n, st.pr or 0)
            tests = (lambda e, published: published,
                     lambda e, _: self._published(body, {**before, "html_url": link}, e),
                     lambda e, _: bool(self._linked(body, link, {e["criterion"]})))
            out: dict[str, dict] = {}
            for test in tests:
                for e, published in found:
                    c = e["criterion"]
                    if c not in proven and c not in mine and c not in out and test(e, published):
                        out[c] = e
            return out
        evidence_head = st.verified_sha or next((e["head_sha"] for e in st.evidence if e.get("kind") == "command"), "")
        verdicts = {i: v for i, v in st.verdicts.items() if v.get("verdict") == "met"}
        adopted = elsewhere(self.gh.get_issue(n).get("body") or "", before.get("html_url") or "")
        for _ in range(DISCHARGE_WRITES):
            comment = self._upsert_comment_answered(st.pr or 0, report.EVIDENCE_MARKER, self._render_evidence(
                n, [*base, *adopted.values()], evidence_head, verdicts))
            link = (comment or {}).get("html_url", "")
            body = self.gh.get_issue(n).get("body") or ""
            if not link or issue_parser.body_hash(body) != st.body_hash:
                return adopted, ""
            ticked = issue_parser.tick(body, dict.fromkeys({*proven, *adopted}, link))
            if ticked != body:
                self.gh.edit_issue(n, body=ticked)
            missing = {c: e for c, e in elsewhere(ticked, link).items() if c not in adopted}
            if not missing:
                return adopted, link
            adopted |= missing
        raise GitHubError(f"the evidence comment of #{n} was written again by another run {DISCHARGE_WRITES} times")

    def _first_proof(self, pr: int | None, items: dict[str, list[str]], candidates: list[tuple[str, str, str]],
                     said: str) -> dict[str, dict]:
        """For each of `items`, the record of the first of `candidates` (commit, context, where)
        whose commit holds each of its smoke tests as a regular file, with the pull request `pr`
        whose merge commit it proves and the tests, by which a kept proof is found again."""
        out = {}
        for item, tests in items.items():
            for sha, context, where in candidates:
                if self._holds_tests(sha, tests):
                    out[item] = self._status_record(item, tests, sha, context, said, where) | {"pr": pr, "smoke_tests": tests}
                    break
        return out

    def _as_delivered(self, n: int, st: IssueState) -> str | None:
        """The body of issue `n` on GitHub, if it still says what was delivered: an edit made while
        the statuses were read changes the items, which are then not the ones proven (None)."""
        body = self.gh.get_issue(n).get("body") or ""
        if issue_parser.body_hash(body) == st.body_hash:
            return body
        self.store.event("discharge_edited", issue=n)
        return None

    def _status_elsewhere(self, n: int, pr: int) -> list[tuple[dict, bool]]:
        """The commit status records another root's run holds for issue `n` and its pull request
        `pr`, which that run wrote in the pull request's evidence comment (`_delivered_elsewhere`),
        each with whether it is published or being published: recorded by that run, or saved while
        it writes it (`PUBLISHING`) by a run that is still alive. One that run gave up on, or left
        when it ended, is not."""
        out = []
        for d in sorted(runs_root(self.repo_root).glob("issue-*")):
            if d.resolve() == self.store.dir.resolve():
                continue
            try:
                raw = json.loads((d / "state.json").read_text())["issues"].get(str(n)) or {}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if isinstance(raw, dict) and raw.get("pr") == pr:
                alive: list[dict] = []   # who holds that run's lock, if an engine that is alive does: asked once
                for e in raw.get("evidence") or []:
                    if not (isinstance(e, dict) and e.get("passed") and e.get("kind") == COMMIT_STATUS_EVIDENCE
                            and isinstance(e.get("criterion"), str)):
                        continue
                    state = e.get(PUBLISHING)
                    if state == "writing" and not alive:
                        lock = RunLock(d / "lock")
                        alive.append(lock.holder() if lock.held() else {})
                    # being written only by the invocation that saved it: a run of the same root
                    # started since, which holds the lock now, did not write it
                    out.append((e, not state or (state == "writing" and bool(alive[0]) and e.get(WRITER) == alive[0])))
        return out

    @staticmethod
    def _linked(body: str, link: str, items) -> set[str]:
        """The `items` whose box `body` ticks with `link`, the evidence comment, as the engine
        ticks it: a box an owner ticked by hand carries no such link, and proves no record."""
        return {i for i in items if link and issue_parser.tick(body, {i: link}) == body}

    def _evidence_comment(self, pr: int) -> dict:
        """Pull request `pr`'s evidence comment, empty when it has none; a list GitHub does not
        answer is raised."""
        return next((c for c in self.gh.list_comments(pr) if report.EVIDENCE_MARKER in (c.get("body") or "")), {})

    def _published(self, body: str, comment: dict, record: dict) -> bool:
        """Whether GitHub published the status `record`: the evidence `comment` holds its own row,
        not another proof's of the same item, and `body` ticks its box with the comment's link."""
        row = next(line for line in report.render_evidence(0, [record], "").splitlines()
                   if line.startswith(f"| {record['criterion']} |"))
        return row in (comment.get("body") or "").splitlines() and \
            bool(self._linked(body, comment.get("html_url") or "", {record["criterion"]}))

    def _upsert_comment_answered(self, n: int, marker: str, body: str) -> dict:
        """`_upsert_comment`, where a read or a write GitHub does not answer is raised: a list of
        comments that could not be read is not an empty one, which would add a second comment."""
        for c in self.gh.list_comments(n):
            if marker in (c.get("body") or ""):
                return self.gh.update_comment(c["id"], body)
        return self.gh.create_comment(n, body)

    def _obligation_text(self, o: dict) -> str:
        if o["kind"] == "deferred":
            return f"{o['item']}: waits for {o['detail']}; it reopens by itself when that issue lands"
        part = f" (agents proved: {o['agent_part']})" if o.get("agent_part") and o["agent_part"] != "none" else ""
        return f"{o['item']}: needs {o['detail']}, which this host lacks{part}"

    def _file_followup(self, n: int, st: IssueState) -> None:
        """Reproduced findings that tie to no item or invariant: one follow-up issue per PR."""
        follow = [f for f in st.findings if f.get("status") == "follow_up"]
        if not follow or st.followup:
            return
        lines = [f"- **{f['title']}**" + (f" (on {f['item']})" if f.get("item") else "")
                 + f" — observed: {f.get('observed')}; expected: {f.get('expected')}; reproduce: "
                 f"`{f.get('reproduction') or f.get('probe')}`" for f in follow]
        body = (f"Follow-up to #{n}" + (f" (PR #{st.pr})" if st.pr else "") + ". The other vendor's examination "
                "reproduced these findings. They did not block the merge: no ledger item or safety invariant requires "
                f"them, or they were raised from examination {self.caps.examiner_bound} on, when only a safety "
                "invariant blocks:\n\n" + "\n".join(lines) + "\n")
        created = self._safe(self.gh.create_issue, f"Follow-up to #{n}: {len(follow)} non-blocking finding(s)", body,
                             [FOLLOWUP_LABEL[0]])
        if created:
            st.followup = created.get("number")
            self.store.event("followup_filed", issue=n, followup=st.followup, findings=[f["id"] for f in follow])

    def _publish_deferred(self, n: int, st: IssueState) -> None:
        deferred = [o for o in st.obligations if o["kind"] == "deferred"]
        if not deferred:
            return
        self._safe(self.gh.add_labels, n, [DEFERRED_LABEL[0]])
        body = (f"{report.DEFERRED_MARKER}\n**implement loop**: delivered without these items, each waiting for another "
                f"issue. Each reopens by itself (under the supervisor) once that issue is delivered or done:\n\n"
                + "\n".join(f"- {o['item']}: waits for {o['detail']}" for o in deferred) + "\n")
        self._upsert_comment(n, report.DEFERRED_MARKER, body)

    def _owner_round(self, n: int, feedback: list[str]) -> None:
        self._requirements_current(n)
        st = self.store.issue(n)
        self._new_round(st, feedback, owner_edit=True)
        st.body_hash = self.plan.specs[n].body_hash
        self.store.put(st)
        self._set_phase(n, Phase.PENDING)

    def _fix_forward(self, n: int, feedback: list[str]) -> None:
        """A merged change broke `main` (its deployment failed, or a ledger item has no evidence):
        one fix-forward round, then a revert PR through the same gates; `main` is never left red."""
        st = self.store.issue(n)
        if st.fix_forwards >= self.caps.fix_forwards:
            return self._revert(n, feedback)
        st.fix_forwards += 1
        self._new_round(st, feedback, owner_edit=False)
        st.phase = Phase.PLANNED if st.plan else Phase.PENDING
        self.store.put(st)
        self.store.event("fix_forward", issue=n, round=st.round)
        self._sync(n)

    def _revert(self, n: int, feedback: list[str]) -> None:
        """Revert what this issue merged since it broke `main`: the breaking round and its
        fix-forward rounds (one round, after an earlier revert). `main` is reverted even past the
        revert cap; past it, the issue fails instead of being delivered once more."""
        st = self.store.issue(n)
        base = self.plan.specs[n].branch or f"loop/issue-{n}"
        # rounds abandoned unmerged (a refused history) lie between the merged ones: look past them
        first = st.round if st.reverts else max(st.round - st.fix_forwards - st.restarts, 1)
        merged = []
        for r in range(st.round, first - 1, -1):   # newest first
            pr = self._safe(self.gh.find_pr, base if r == 1 else f"{base}-r{r}") or {}
            if (pr.get("merged") or pr.get("merged_at")) and pr.get("merge_commit_sha"):
                merged.append(pr)
        if not merged:
            return self._fail(n, "main_broken", "main fails after the merge and the fix-forward; the revert could not be "
                                                "made: " + "; ".join(feedback)[:500])
        if st.worktree:   # the revert's worktree takes the issue worktree's place, as the next round does
            self.ws.remove(Path(st.worktree))
            st.worktree = None
        # A revert lands through the same gates as any change: the merge queue, the standing and
        # history checks, the other vendor's certificate for the exact head, and the required check
        # on it.
        waited = 0
        while not self._take_queue(n):
            if waited >= self.caps.queue_wait_s or self._unreleasable_hold(n):
                return self._wait(n, WAIT_MERGE_QUEUE, "the revert waits for the merge queue", resume=Phase.MERGED)
            self.sleep(self.caps.queue_poll_s)
            waited += max(self.caps.queue_poll_s, 1)
        wt = None
        try:
            st.reverts += 1
            self.store.put(st)
            branch = f"{st.branch}-revert"
            wt = self.ws.issue_worktree(n, branch)
            self.ws.fetch()
            main_sha = self.ws.git("rev-parse", self.ws.base_ref(), cwd=wt)
            self.ws.git("reset", "--hard", "--quiet", main_sha, cwd=wt)
            self.ws.git("revert", "--no-edit", *[pr["merge_commit_sha"] for pr in merged], cwd=wt)
            head = self.ws.head(wt)
            with self.ws.disposable(wt) as scratch:   # like any head that is pushed: the standing checks first
                _, standing = verify.standing_checks(scratch, standing_checks(), head, self.store.issue_dir(n) / "revert",
                                                     self.caps.check_timeout_s, self.check_env, self._mounts(scratch))
            if standing:
                return self._fail(n, "main_broken", "the revert's head fails the standing checks: "
                                                    + "; ".join(standing)[:500])
            findings, unscanned = self._history_scan(n, wt, main_sha, head, "revert")
            if findings or unscanned:   # the revert commits themselves: their messages and their author
                return self._fail(n, "main_broken", "the revert's commits fail the history checks, so it is not pushed: "
                                                    + ("; ".join(history_places(findings)) if findings
                                                       else "; ".join(unscanned))[:500])
            with self.ws.disposable(wt) as scratch:
                exam = self.agents.examine(self.other, self._req(n, [
                    (f"Certify a revert: the loop reverts this issue's merged change because main fails after it and a "
                     f"fix-forward did not repair it. Check that `{main_sha[:12]}..{head[:12]}` only undoes that "
                     f"change and leaves main working.")]), {"items": []}, scratch, main_sha, head, [], [], [],
                    None)
            serious = []
            for f in exam.findings:   # the same rule as any examination: reproduced, witnessed, tied to an invariant
                probe = next((p for p in exam.probes if f.probe and p.file == f.probe and not self._probe_problem(p)),
                             None)
                reproduced = (self._reproduces(wt, probe.command, probe.cwd, (probe.file, probe.content)) if probe
                              else self._reproduces(wt, f.reproduction, f.cwd or ".", None))
                if gate.classify(f, {}, self.invariants, reproduced, set(), set()).blocking:
                    serious.append(f)
            if not exam.certify or serious:
                why = exam.summary[:300] if not exam.certify else "; ".join(f.title for f in serious)[:300]
                return self._fail(n, "main_broken", f"the other vendor did not certify the revert: {why}")
            local = self._local_scan(wt, main_sha, head)   # the owner's own look, as before any push
            if local and (local[0] or local[1]):
                return self._fail(n, "main_broken", "the revert fails the scan with the owner's local values, so it is not "
                                                    "pushed: " + "; ".join(local[0] or local[1])[:500])
            # Before any push, as in the land step: a revert of a change to a workflow file changes
            # that file again, and the engine pushes no such commit, whatever identity it runs as.
            # The certified revert is the owner's to push and to merge (`_owner_revert`).
            workflows = self._workflow_paths(wt, main_sha, head)
            if workflows:
                st = self.store.issue(n)
                st.owner_landing = {"branch": branch, "head": head, "paths": workflows, "announced": None,
                                    "published": None, "revert": True, "of": [pr["number"] for pr in merged],
                                    "feedback": [f[:1500] for f in feedback]}
                self.store.put(st)
                self.store.event("owner_landing", issue=n, branch=branch, head=head, paths=workflows, revert=True)
                return self._owner_revert(n, made=True)
            if not self._push(wt, head if local is not None else None) or self.ws.head(wt) != head:
                return self._fail(n, "main_broken", "main moved while the revert was being certified")
            rpr = self._revert_pull(n, branch, [pr["number"] for pr in merged], feedback)
            conclusion = self._wait_required(head)
            if conclusion != "success":
                return self._fail(n, "main_broken", f"the revert PR #{rpr['number']} did not pass the required check "
                                                    f"({conclusion})")
            self.gh.merge_pr(rpr["number"], head)
            self.store.event("reverted", issue=n, pr=rpr["number"], of=[pr["number"] for pr in merged])
        finally:
            if wt is not None:
                self.ws.remove(wt)
            self.claims.release(MERGE_QUEUE, self.owner)
        self._reverted(n, rpr["number"], feedback)

    def _revert_pull(self, n: int, branch: str, of: list[int], feedback: list[str]) -> dict:
        """Open the pull request of a revert of the pull requests `of`, from `branch`."""
        prs = ", ".join(f"#{number}" for number in of)
        body = (f"The implement loop reverts PR {prs} for #{n}: after its merge, `main` fails and one fix-forward "
                f"did not repair it. Scanned and certified by {self.other} on this exact head.\n\n"
                + "\n".join(f"- {f}" for f in feedback)[:3000])
        return self.gh.create_pr(branch, self.cfg.base_branch, f"Revert #{n}: it broke main", body)

    def _reverted(self, n: int, pr: int, feedback: list[str]) -> None:
        """The revert is merged: the issue is delivered again in a new round, unless it was reverted
        once too often."""
        st = self.store.issue(n)
        if st.reverts > self.caps.reverts:
            return self._fail(n, "main_broken", f"its delivery broke main again and was reverted in PR #{pr}: "
                                                + "; ".join(feedback)[:500])
        self._new_round(st, feedback + [(f"Your earlier delivery was reverted in PR #{pr} because it broke "
                                         f"main; deliver it again without breaking it.")], owner_edit=False)
        st.phase = Phase.PLANNED if st.plan else Phase.PENDING
        self.store.put(st)
        self._sync(n)

    def _owner_revert(self, n: int, made: bool = False) -> None:
        """A certified revert that changes a workflow file is landed by the owner, like the change
        it undoes: the engine pushes nothing and merges nothing. `made`: the revert was made and
        certified by the calling step, which holds the merge queue and releases it; this posts the
        comment that names the revert's branch and head, with the command that pushes it. Each
        resumed step (`_step_accept`) then looks again. The remote branch at the certified head:
        the pull request is opened, once. That pull request merged with that head, or with one
        that descends from it: the issue starts its next round, as after any revert. The revert
        no longer merges into a main that moved: the issue ends `failed` (`main_broken`), as when
        main moves while a revert is being certified. Otherwise it keeps waiting
        (`WAIT_OWNER_LANDING`), and no landing claim is held."""
        st = self.store.issue(n)
        record = self._owner_record(st)
        assert record is not None
        branch, head, of = record["branch"], record["head"], list(record.get("of") or [])
        feedback = list(record.get("feedback") or [])
        pr = None if made else self.gh.find_pr(branch)
        if pr and (pr.get("merged") or pr.get("merged_at")):
            merged_head = (pr.get("head") or {}).get("sha") or head
            if self._delivery(st, merged_head, reviewed=head)[0] != "stale":
                self.store.event("reverted", issue=n, pr=pr["number"], of=of, head=merged_head, by="owner")
                st.owner_landing = None
                self.store.put(st)
                return self._reverted(n, pr["number"], feedback)
        if not made:
            self.ws.fetch()
            if not self.ws.is_ancestor(self.ws.repo, self.ws.base_ref(), head):
                wt = self.ws.issue_worktree(n, branch)   # the revert's branch: the issue's own worktree is gone
                try:
                    moved = self.ws.head(wt) != head
                    files = [] if moved else self._conflicts_with_main(wt, pr, head)
                finally:
                    self.ws.remove(wt)
                if moved or files:
                    st.owner_landing = None
                    self.store.put(st)
                    why = (f"the branch `{branch}` is no longer at the certified revert `{head[:12]}`" if moved else
                           f"main moved, and the certified revert `{head[:12]}` on `{branch}` no longer merges into it "
                           f"without a conflict ({', '.join(files)[:300]})")
                    return self._fail(n, "main_broken", (
                        f"{why}; it was the owner's to land, because it changes a workflow file, and the loop pushed "
                        f"nothing of it: " + "; ".join(feedback)[:400]))
        if record.get("announced") != head:
            self._hand_over(n, record, (
                f"`main` fails after the merge of {', '.join(f'#{number}' for number in of)}, and one fix-forward did "
                f"not repair it, so the loop made a revert, and {self.other} certified it."))
            self.store.put(st)
        detail = self._owner_pull(n, record, lambda: self._revert_pull(n, branch, of, feedback))
        self.store.put(st)
        self._wait(n, WAIT_OWNER_LANDING, f"a revert: {detail}", resume=Phase.MERGED)

    def _reconcile_delivered(self) -> None:
        """A delivered issue whose owner has since ticked every remaining box is done. A deferred
        item whose issue has since been delivered or closed reopens: a new round delivers it."""
        for n in self.plan.work:
            st = self.store.issue(n)
            if st.phase != Phase.DELIVERED or self._kept_proofs(st):
                continue   # a discharge that may have ticked boxes it did not record ends first (`_kept_proofs`)
            landed = [o for o in st.obligations if o.get("kind") == "deferred" and self._delivered(o.get("issue"))]
            for o in st.obligations:
                if o.get("kind") == "deferred" and not o.get("unresolved") and o not in landed:
                    data = self._safe(self.gh.get_issue, o.get("issue")) or {}
                    if data.get("state") == "closed":   # closed without delivering: reported, never reopened
                        o["unresolved"] = True
                        self.store.put(st)
                        self.store.event("deferred_unresolved", issue=n, item=o["item"], on=o.get("issue"))
            if landed:
                st.obligations = [o for o in st.obligations if o not in landed]
                st.plan = {**(st.plan or {}), "items": [
                    {**i, "proof": "review" if i["proof"] == "deferred" else i["proof"]}
                    if any(o["item"] == i["id"] for o in landed) else i for i in (st.plan or {}).get("items", [])]}
                self.store.event("deferred_reopened", issue=n, items=[o["item"] for o in landed])
                self._new_round(st, [f"{o['item']} was deferred to {o['detail']}, which has landed; deliver it now."
                                     for o in landed], owner_edit=False)
                st.phase = Phase.PLAN
                self.store.put(st)
                self._sync(n)
                continue
            if any(o.get("kind") == "deferred" for o in st.obligations):
                continue   # it closes once its deferred items are delivered, not on ticked boxes
            body = (self._safe(self.gh.get_issue, n) or {}).get("body") or ""
            items = issue_parser.ledger(body)
            if items and all(i.checked for i in items):
                try:
                    self._announce(n, replace(st, human_tasks=[]), Phase.DONE)
                except RedactionUnavailable as e:
                    # It stays open and waits, to be delivered again and closed by a later run: left
                    # delivered it would be settled for the supervisor, which would stop there.
                    self.store.event("progress_not_published", issue=n, phase=Phase.DONE.value, error=str(e)[:300])
                    self._wait(n, WAIT_GITHUB, str(e)[:300], resume=Phase.DELIVERED)
                    continue
                try:   # done only once GitHub closed it
                    self.gh.edit_issue(n, state="closed", state_reason="completed")
                except (GitHubError, OSError) as e:
                    self.store.event("github_call_failed", call="edit_issue", issue=n, error=str(e)[:300])
                    if (self._safe(self.gh.get_issue, n) or {}).get("state") != "closed":
                        # Not known to be closed: it waits to be delivered again, and closed, by the next
                        # run, or, closed after all and so no longer that run's work, is settled as done
                        # there (`_settle_closed_elsewhere`); its progress comment no longer says done.
                        self._wait(n, WAIT_GITHUB, f"the issue could not be closed: {str(e)[:300]}",
                                   resume=Phase.DELIVERED)
                        continue
                self.store.event("owner_evidence_complete", issue=n)
                self._set_phase(n, Phase.DONE, human_tasks=[])
                self._close_trackers(n)

    def _delivered(self, m: int | None) -> bool:
        """Has issue `m` landed? Delivered or done in this run, or on GitHub closed as completed or
        labelled delivered. A closure as not planned is not a delivery."""
        if not m:
            return False
        if m in self.plan.work and self.store.issue(m).phase in SATISFIES_DEPENDENTS:
            return True
        data = self._safe(self.gh.get_issue, m) or {}
        labels = {l["name"] if isinstance(l, dict) else l for l in data.get("labels", [])}
        if data.get("state") == "closed":
            return (data.get("state_reason") or "completed") == "completed"
        return "loop:delivered" in labels

    def _mark_blocked(self) -> None:
        """Issues still pending behind a prerequisite that waits or failed wait with it, so the
        report says why; they resume when it is delivered."""
        phases = self._phases()
        for n in self.plan.work:
            if phases[n] != Phase.PENDING:
                continue
            stuck = [d for d in sorted(self.plan.deps.get(n, ())) if phases.get(d) in (Phase.WAITING, Phase.FAILED)]
            if stuck:
                self._wait(n, "prerequisite", f"waits for #{stuck[0]}, which is {phases[stuck[0]].value}",
                           resume=Phase.PENDING)

    def _close_trackers(self, n: int) -> None:
        parent = self.plan.parent.get(n)
        while parent is not None:
            children = self.plan.tracking.get(parent, [])
            phases = self._phases()
            done = all((c in self.plan.closed_work) or phases.get(c) == Phase.DONE or
                       (c in self.plan.tracking and self.plan.issues[c].state == "closed") for c in children)
            body = (self._safe(self.gh.get_issue, parent) or {}).get("body") or ""
            if not done or any(not item.checked for item in issue_parser.ledger(body)):
                return
            self._safe(self.gh.edit_issue, parent, state="closed", state_reason="completed")
            self.plan.issues[parent].state = "closed"
            self.store.event("tracker_closed", issue=parent)
            parent = self.plan.parent.get(parent)

    # ------------------------------------------------------------------ end
    def _summary(self, stopped: bool) -> dict:
        phases = self._phases()
        counts: dict[str, int] = {}
        for ph in phases.values():
            counts[ph.value] = counts.get(ph.value, 0) + 1
        return {"stopped": stopped, "phases": counts,
                "waiting": {n: f"[{(self.store.issue(n).wait or {}).get('kind')}] {(self.store.issue(n).wait or {}).get('detail')}"
                            for n, ph in phases.items() if ph == Phase.WAITING},
                "failed": {n: f"[{(self.store.issue(n).fail or {}).get('cause')}] {(self.store.issue(n).fail or {}).get('detail')}"
                           for n, ph in phases.items() if ph == Phase.FAILED},
                "owner_obligations": {n: self.store.issue(n).human_tasks for n, ph in phases.items()
                                      if ph == Phase.DELIVERED}}

    def _post_run_summary(self) -> None:
        states = {n: self.store.issue(n) for n in self.plan.work}
        try:
            self._upsert_comment(self.plan.root, report.RUN_MARKER, report.render_run_summary(self.plan.root, states))
        except RedactionUnavailable as e:   # no issue's state depends on it; the next run writes its own
            self.store.event("run_summary_not_published", error=str(e)[:300])


ACCEPTANCE_CHECKS = "^Loop-Phase: acceptance-checks$"   # the trailer of a commit CI's loop guard reads


def locked_on_branch(ws: Workspace, wt: Path) -> list[str]:
    """The files CI's loop guard (`scripts/check-loop-guard.sh`) holds locked at the head of the
    branch in `wt`, read as it reads them: what each acceptance-checks commit since main wrote or
    named in `Loop-Locks`, less what that commit or a later one named in `Loop-Unlocks`. Merge
    commits count for nothing."""
    locked: set[str] = set()
    commits = ws.git("rev-list", "--reverse", "--no-merges", f"--grep={ACCEPTANCE_CHECKS}", f"{ws.base_ref()}..HEAD",
                     cwd=wt).split()
    for sha in commits:
        lines = ws.git("log", "-1", "--format=%B", sha, cwd=wt).splitlines()
        unlocks = {line.removeprefix("Loop-Unlocks: ") for line in lines if line.startswith("Loop-Unlocks: ")}
        locks = {line.removeprefix("Loop-Locks: ") for line in lines if line.startswith("Loop-Locks: ")}
        written = set(ws.git("diff-tree", "--no-commit-id", "--name-only", "--diff-filter=d", "-r", sha,
                             cwd=wt).splitlines())
        locked = (locked - unlocks) | ({f for f in written | locks if f} - unlocks)
    return sorted(locked)


def has_conflict_markers(path: Path) -> bool:
    return path.is_file() and bool(CONFLICT_MARKER.search(path.read_text(errors="replace")))


def conflict_feedback(files: list[str]) -> str:
    return ("Main moved on, and merging it into this branch conflicts. The merge is in progress in your worktree. "
            f"Resolve the conflict markers in {', '.join(files)}, keeping the intent of both sides: main's side is "
            f"work already merged, this branch's side is this issue's work. Do not run git merge, commit or abort: "
            f"the engine concludes the merge.")


def criterion_item(criterion: str | None, items: set[str]) -> str | None:
    """The plan item a command proves: its criterion, or the item id it leads with ("D2, D3: the
    probes" proves D2). None when it names no item."""
    if criterion in items:
        return criterion
    m = CRITERION_LEAD.match(criterion or "")
    return m.group(1) if m and m.group(1) in items else None


def _names_probe(command: dict, probe: str) -> bool:
    """Whether an examiner's command runs a probe file: one of its path words, resolved from the
    command's cwd, is the probe (`tests/x.py` run in `services/api/` names `services/api/tests/x.py`;
    `archive/tests/x.py` there does not)."""
    cwd = command.get("cwd") or "."
    target = posixpath.normpath(probe)
    for word in re.findall(r"[^\s'\"=;&|()<>]+", command.get("command") or ""):
        path = word.split("::", 1)[0]
        if path and posixpath.normpath(posixpath.join(cwd, path)) == target:
            return True
    return False


VERIFIER_RUN = "verifier_run"   # on an open finding: the engine's last run of its reproduction (`run_record`)
PROBE_TEXT = "probe_text"       # on a finding a probe witnesses: the probe's content, never committed
RUN_OUTPUT = 1500               # the characters of a run's output a builder is given, as for a standing check
FAILED_RUNS = 4                 # the failed commands of one verification whose output a builder is given


def run_verdict(ran: Evidence | None) -> bool | None:
    """What a run of a reproduction says of its finding: True when it failed (the finding holds),
    False when it passed, None when there was no run or it could not start."""
    if ran is None or (ran.exit_code is None and "timed out" not in (ran.output_tail or "")):
        return None
    return not ran.passed


def run_record(ran: Evidence) -> dict:
    """What is kept of a verifier's run for the builder: where it ran, how it ended, and the end
    of what it printed."""
    return {"head_sha": ran.head_sha, "exit_code": ran.exit_code, "note": ran.note,
            "output_tail": (ran.output_tail or "")[-RUN_OUTPUT:]}


def run_said(ran: Mapping) -> str:
    """How a run ended and what it printed last, from its evidence record or its `run_record`."""
    ended = ran.get("note") or f"exit {ran.get('exit_code')}"
    printed = (ran.get("output_tail") or "")[-RUN_OUTPUT:].strip()
    return f"it ended with {ended}" + (f", and its output ends with:\n{printed}" if printed else ", and printed nothing")


def failed_output(evidence: list[dict]) -> list[str]:
    """For the builder: what each command that failed a verification printed where the verifier
    ran it. The verifier's problem is `<item> failed: exit 1` and no more, and a command that
    passes in the builder's worktree gives it nothing to act on (#32: three such rounds). A
    command that proves several items is given once, with the items. A standing check is left
    out: its problem carries its output already."""
    failed: dict[tuple[str, str], dict] = {}
    items: dict[tuple[str, str], list[str]] = {}
    for e in evidence:
        if e.get("passed") or not e.get("command") or str(e.get("criterion") or "").startswith(verify.STANDING):
            continue
        run = (e["command"], e.get("cwd") or ".")
        failed.setdefault(run, e)
        items.setdefault(run, []).append(str(e.get("criterion")))
    out = [(f"The verifier's run of `{command}` for {', '.join(items[command, cwd])}, in `{cwd}` of a copy of "
            f"`{str(e.get('head_sha'))[:12]}` and in its own environment, is the one that counts: {run_said(e)}")
           for (command, cwd), e in list(failed.items())[:FAILED_RUNS]]
    if len(failed) > FAILED_RUNS:
        out.append(f"{len(failed) - FAILED_RUNS} more commands failed; their output is not repeated here.")
    return out


def history_findings(evidence: list[dict]) -> list[str]:
    """From evidence records, the output of each history check that reported findings (its exit
    status says so). One that could not run is not among them: it is an ordinary failed check."""
    names = {f"{verify.STANDING}{name}" for name, _ in history_checks("")}
    return [e.get("output_tail") or "" for e in evidence
            if e.get("criterion") in names and e.get("exit_code") == HISTORY_FINDINGS_EXIT]


HISTORY_PLACE = re.compile(r"^(\S[^\n]*?:\d+: [A-Za-z][\w-]*): ", re.MULTILINE)
OBJECT_ID = re.compile(r"@([0-9a-f]{12})[0-9a-f]{28,52}\b")


def history_places(outputs: list[str]) -> list[str]:
    """Where a history check found something and which rule, without the excerpt it printed:
    `path@<blob>:<line>: <rule>`, object IDs shortened. The engine publishes this (a fault issue,
    feedback to the builder), and an excerpt shows the text around a value."""
    places = [OBJECT_ID.sub(r"@\1", m) for out in outputs for m in HISTORY_PLACE.findall(out)]
    return list(dict.fromkeys(places)) or ["see the `history_refused` event in the run's `events.jsonl`"]


# One finding as the scanner prints it: `path:line: rule: excerpt`, the path with `@<blob id>` after
# it in a history scan (`<commit>@<commit id>` for a commit's own text).
LOCAL_FINDING = re.compile(r"^(.+?)(?:@[0-9a-f]{12})?:\d+: ([a-z][a-z0-9-]*): ", re.MULTILINE)


def local_places(output: str) -> list[str]:
    """What the engine keeps of the findings of a scan with the owner's local values:
    `<path>: <rule>`, each once. Not the line, not the object and not the excerpt: the excerpt
    shows the text around the owner's value, and the rest says where in it to look. The path is as
    the scanner printed it, which masks a value in a file's name."""
    return list(dict.fromkeys(f"{path[:200]}: {rule}" for path, rule in LOCAL_FINDING.findall(output)))


# The `pre-push` hook of a scanned push (`Engine._push`). git gives it, on standard input, one line
# for each ref it is about to update: `<local ref> <local commit> <remote ref> <remote commit>`.
PUSH_GUARD = """#!/bin/sh
# Written by the implement loop's engine for one push, and removed after it. git may publish the
# commit the engine scanned with the owner's local values, and no other.
while read -r ref commit rest; do
  if [ "$commit" != "{only}" ]; then
    echo "[loop] not pushed: $commit is not the commit that was scanned ({only})" >&2
    exit 1
  fi
done
exit 0
"""


def environment_problem(evidence: list[dict]) -> str | None:
    for ev in evidence:
        m = ENVIRONMENT_FAILURE.search(ev.get("output_tail") or "")
        if m:
            line = next((l for l in (ev.get("output_tail") or "").splitlines() if m.group(0) in l), m.group(0))
            return f"{ev.get('criterion')}: {line.strip()[:300]}"
    return None


def fault_fingerprint(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "implement_loop" in (f.filename or "")]
    where = f"{Path(frames[-1].filename).name}:{frames[-1].name}" if frames else "unknown"
    return hashlib.sha256(f"{type(exc).__name__}@{where}".encode()).hexdigest()[:10]


def engine_frames(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "implement_loop" in (f.filename or "")]
    return "\n".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in frames) or "(no engine frame)"


def redact(text: str, repo_root: Path) -> str:
    home = str(Path.home())
    return text.replace(str(repo_root), "<repo>").replace(home, "~")
