"""The supervisor: what makes "waits resume by themselves" true (ADR 0012).

`implement supervise <root>` runs the engine for a root again and again until every issue is
terminal, within a fixed bound of runs (part of the safety surface: a repair run cannot raise it).
It has no deadline of its own: what sequences supervisions over time is outside the engine. A
caller may still give one (`max_hours`), and the supervision then stops there, asking a run in
progress to stop first:

- before each run it fast-forwards the main checkout when it is clean and on `main`, so a new
  engine version, or a prerequisite merged elsewhere, is picked up;
- an issue waiting on an outage is resumed only once that vendor answers the doctor's smoke call;
- an engine exception the engine filed as a loop fault gets **one repair run per fault
  fingerprint**, on the fault issue, in repair mode (the engine refuses any change to the safety
  surface); then the root resumes. A stall (an issue `failed` on `ceiling`, `no_progress` and the
  like) is no fault: it stays in the run's journal and gets no repair run; the operator reads it,
  and `retry` revives the issue;
- a delivered issue with a deferred item resumes once that item's issue has landed (the engine
  reopens the item);
- a crashed run is relaunched with backoff.

Each engine run is a fresh process, so it imports the engine as it is on disk after the pull.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .model import Phase
from .state import RunStore, runs_root

MAX_RUNS = 10
BACKOFF_S = (60, 300, 900, 1800, 3600)


@dataclass
class Outcome:
    finished: bool
    reason: str
    runs: int
    repairs: list[str] = field(default_factory=list)
    waiting: dict[int, str] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)


class Supervisor:
    def __init__(self, repo_root: Path, root: int, operator: str, run: Callable[[list[str]], int] | None = None,
                 reachable: Callable[[str], bool | None] | None = None, pull: Callable[[], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, max_runs: int = MAX_RUNS, max_hours: float | None = None):
        self.repo_root, self.root, self.operator = repo_root, root, operator
        self._run = run or self._run_engine
        self._reachable = reachable or self._vendor_reachable
        self._pull = pull or self._fast_forward
        self.sleep, self.clock, self.log = sleep, clock, log
        # the bound on runs can be lowered, never raised: it is what keeps a supervision finite.
        # A deadline is the caller's choice; with none, the supervision ends on its runs alone.
        self.max_runs, self.max_hours = min(max_runs, MAX_RUNS), max_hours
        self._deadline: float | None = None
        self.state_path = runs_root(repo_root) / f"supervisor-{root}.json"

    # ------------------------------------------------------------------ the loop
    def supervise(self) -> Outcome:
        state = self._load()
        started = self.clock()
        if self.max_hours is not None:   # one deadline for repair and root runs alike
            self._deadline = started + self.max_hours * 3600
        runs, quiet = 0, 0
        while True:
            if runs >= self.max_runs:
                return self._finish(state, False, f"stopped after {runs} runs (the bound is {self.max_runs})", runs)
            if self._expired():
                return self._finish(state, False, f"stopped after {self.max_hours} hours", runs)
            self.log(f"supervise #{self.root}: {self._pull()}")
            store = RunStore.for_root(self.repo_root, self.root)
            outages = self._outages(store)
            unreachable = [v for v in outages if self._reachable(v) is False]
            if outages and len(unreachable) == len(outages) and not self._other_work(store):
                delay = self._within_deadline(BACKOFF_S[min(quiet, len(BACKOFF_S) - 1)])
                self.log(f"supervise #{self.root}: {', '.join(unreachable)} still unreachable; waiting {delay}s")
                quiet += 1
                self.sleep(delay)
                continue
            for fingerprint, number in self._unrepaired_faults(state):
                if runs >= self.max_runs or self._expired():
                    break
                self.log(f"supervise #{self.root}: repair run on #{number} (fault {fingerprint})")
                code = self._run(["run", str(number), "--operator", self.operator, "--yes", "--repair"])
                runs += 1
                state.setdefault("repairs", {})[fingerprint] = {"issue": number, "exit": code, "at": self.clock()}
                self._save(state)
                self.log(f"supervise #{self.root}: {self._pull()}")
            if runs >= self.max_runs:
                return self._finish(state, False, f"stopped after {runs} runs (the bound is {self.max_runs})", runs)
            if self._expired():
                return self._finish(state, False, f"stopped after {self.max_hours} hours", runs)
            before = self._snapshot(RunStore.for_root(self.repo_root, self.root))
            code = self._run(["run", str(self.root), "--operator", self.operator, "--yes"])
            runs += 1
            store = RunStore.for_root(self.repo_root, self.root)
            after = self._snapshot(store)
            if self._all_settled(store) and not self._unrepaired_faults(state):
                return self._finish(state, True, "every issue is done, delivered or failed for good", runs)
            if code not in (0, 3):
                self.log(f"supervise #{self.root}: the run exited {code}; relaunching after a pause")
            quiet = quiet + 1 if after == before else 0
            delay = BACKOFF_S[min(quiet, len(BACKOFF_S) - 1)] if quiet else BACKOFF_S[0]
            self.sleep(self._within_deadline(delay))

    def _expired(self) -> bool:
        return self._deadline is not None and self.clock() >= self._deadline

    def _within_deadline(self, delay: float) -> float:
        """A pause, cut short at the caller's deadline when there is one."""
        return delay if self._deadline is None else min(delay, max(self._deadline - self.clock(), 0))

    # ------------------------------------------------------------------ observations
    def _snapshot(self, store: RunStore) -> str:
        return json.dumps({n: (store.issue(n).phase.value, store.budget(n)["calls"]) for n in store.known()}, sort_keys=True)

    def _outages(self, store: RunStore) -> list[str]:
        vendors = set()
        for n in store.known():
            st = store.issue(n)
            if st.phase == Phase.WAITING and (st.wait or {}).get("kind") == "outage":
                vendors.add((st.wait or {}).get("vendor") or "claude")
        return sorted(vendors)

    def _other_work(self, store: RunStore) -> bool:
        return any(store.issue(n).phase not in (Phase.DONE, Phase.DELIVERED, Phase.FAILED, Phase.WAITING)
                   for n in store.known())

    def _all_settled(self, store: RunStore) -> bool:
        from . import VERSION
        for n in store.known():
            st = store.issue(n)
            if st.phase == Phase.DONE:
                continue
            if st.phase == Phase.DELIVERED and not any(o.get("kind") == "deferred" and not o.get("unresolved")
                                                       for o in st.obligations):
                continue
            if st.phase == Phase.FAILED and ((st.fail or {}).get("resumed") or (st.fail or {}).get("version") == VERSION):
                continue   # failed for good on this engine version; a new version revives it once
            return False
        return bool(store.known())

    def _unrepaired_faults(self, state: dict) -> list[tuple[str, int]]:
        path = runs_root(self.repo_root) / "faults.json"
        try:
            faults = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        done = state.get("repairs", {})
        return [(fp, entry["filed"]) for fp, entry in sorted(faults.items())
                if isinstance(entry, dict) and entry.get("filed") and fp not in done
                and self.root in (entry.get("runs") or [self.root]) and not _stall(entry)]

    # ------------------------------------------------------------------ side effects
    def _run_engine(self, args: list[str]) -> int:
        """One engine run. With a deadline, past it the run is asked to stop at its next safe point,
        and killed if it does not within ten minutes; without one, it runs until it ends."""
        cmd = [sys.executable, str(self.repo_root / "tools" / "implementation-loop" / "implement.py"), *args]
        proc = subprocess.Popen(cmd, cwd=self.repo_root, start_new_session=True)
        remaining = None if self._deadline is None else max(self._deadline - self.clock(), 1)
        try:
            return proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            root = args[1] if len(args) > 1 else str(self.root)
            run_dir = RunStore.for_root(self.repo_root, int(root)).dir
            run_dir.joinpath("STOP").write_text("supervision deadline\n")
            for sig, grace in ((None, 120), (signal.SIGTERM, 60), (signal.SIGKILL, 30)):
                if sig is not None:
                    self._signal_group(proc.pid, sig)
                    self._reap_agents(run_dir, sig)
                try:
                    return proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    continue
            self._reap_agents(run_dir, signal.SIGKILL)
            return -9

    @staticmethod
    def _signal_group(pid: int, sig) -> None:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _reap_agents(self, run_dir: Path, sig) -> None:
        """Agents run in their own sessions: signal each recorded process group too."""
        try:
            pids = [int(x) for x in (run_dir / "agents.pids").read_text().split() if x.strip()]
        except (OSError, ValueError):
            pids = []
        for pid in pids:
            self._signal_group(pid, sig)

    def _vendor_reachable(self, vendor: str) -> bool | None:
        from .environment import vendor_reachable
        return vendor_reachable(vendor)

    def _fast_forward(self) -> str:
        def git(*a):
            return subprocess.run(["git", *a], cwd=self.repo_root, capture_output=True, text=True, check=False)
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "main":
            return f"main checkout is on {branch}; not updated"
        if git("status", "--porcelain").stdout.strip():
            return "main checkout has local changes; not updated"
        out = git("pull", "--ff-only", "--quiet")
        return "main checkout updated" if out.returncode == 0 else f"main checkout not updated: {out.stderr.strip()[:200]}"

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=1, sort_keys=True))

    def _finish(self, state: dict, finished: bool, reason: str, runs: int) -> Outcome:
        store = RunStore.for_root(self.repo_root, self.root)
        out = Outcome(finished=finished, reason=reason, runs=runs, repairs=sorted(state.get("repairs", {})))
        for n in store.known():
            st = store.issue(n)
            if st.phase == Phase.WAITING:
                out.waiting[n] = f"{(st.wait or {}).get('kind')}: {(st.wait or {}).get('detail', '')}"
            elif st.phase == Phase.FAILED:
                out.failed[n] = f"{(st.fail or {}).get('cause')}: {(st.fail or {}).get('detail', '')}"
            for o in st.obligations:
                if o.get("kind") == "deferred" and st.phase == Phase.DELIVERED:
                    out.waiting.setdefault(n, f"deferred {o['item']} to {o['detail']} (unresolved)")
        state["last"] = {"finished": finished, "reason": reason, "runs": runs, "at": self.clock()}
        self._save(state)
        self.log(f"supervise #{self.root}: {reason}")
        return out


def _stall(entry: dict) -> bool:
    """A fault an earlier engine filed for an issue it could not deliver (`Engine._fail`), not for
    an exception: its title says `#<n> not delivered (<cause>)`, and an exception's says `<type> in
    the engine (<fingerprint>)`. The engine no longer files one (a stall stays in the run's
    `events.jsonl`), but `faults.json` keeps the entries it filed, and they get no repair run."""
    return " not delivered (" in str(entry.get("title") or "")
