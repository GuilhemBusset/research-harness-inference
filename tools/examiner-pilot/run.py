"""The examiner pilot (ADR 0001, decision 2): how often the examiner's findings name a defect seeded
in a change to the CDK app, per class of defect.

The corpus is `defects/`: patches written against one commit of `main`, which `manifest.toml`
records. For each defect the runner builds a scratch worktree of that commit outside the
repository, applies the patch there and leaves it uncommitted, and asks the examiner role for its
findings through the engine's agent interface (`Agents.examine`), the call the implement loop makes
before it certifies a head. A defect is caught when one finding points at the patched file and
names the defect: the safety invariant of its class, or one of the defect's `match` phrases, each a
statement of the defect itself, never an incidental word of the file or of the task. The report,
with the catch rate per class, is written outside the repository.

Nothing here commits, branches, tags or pushes. A scratch worktree is a fresh repository with no
branch and no remote, which borrows the objects of this one read-only and is deleted after its
examination. Before any examination, every patch is applied to the recorded commit: one that does
not apply, or that changes another file than its entry names, refuses the whole run.

    uv run --locked python tools/examiner-pilot/run.py --check             # the patches, no examiner
    uv run --locked python tools/examiner-pilot/run.py --examiner codex    # the pilot (the owner's)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ENGINE = REPO / "tools" / "implementation-loop"
MANIFEST = HERE / "defects" / "manifest.toml"
sys.path.insert(0, str(ENGINE))

from implement_loop import issue_parser, sandbox
from implement_loop.agents import AgentFailure, Agents, ExamResult, IssueRequest
from implement_loop.config import load_policy
from implement_loop.live import DIFF_LIMIT, LiveAgents
from implement_loop.model import Finding, Issue

VENDORS = ("codex", "claude")


class Refused(Exception):
    """The run cannot start: a patch does not apply to the recorded commit, or the commit is missing."""


# ---------------------------------------------------------------------------- the corpus


@dataclass(frozen=True)
class Defect:
    id: str
    cls: str
    file: str
    description: str
    task: str
    match: tuple[str, ...]
    caught_by_tests: bool
    patch: Path


@dataclass(frozen=True)
class Manifest:
    commit: str
    classes: dict[str, tuple[str, ...]]   # class -> the safety invariants a finding of it names
    defects: tuple[Defect, ...]


def load_manifest(path: Path = MANIFEST) -> Manifest:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    classes = {name: tuple(entry["invariants"]) for name, entry in data["classes"].items()}
    defects = tuple(
        Defect(
            id=entry["id"],
            cls=entry["class"],
            file=entry["file"],
            description=entry["description"],
            task=entry["task"],
            match=tuple(entry.get("match", ())),
            caught_by_tests=bool(entry["caught_by_tests"]),
            patch=path.parent / f"{entry['id']}.patch",
        )
        for entry in data["defect"]
    )
    unknown = sorted({d.cls for d in defects} - set(classes))
    if unknown:
        raise ValueError(f"{path}: defects of classes the manifest does not define: {', '.join(unknown)}")
    return Manifest(commit=data["commit"], classes=classes, defects=defects)


# ---------------------------------------------------------------------------- scoring


@dataclass
class Outcome:
    defect: str
    cls: str
    caught: bool
    certified: bool
    caught_by_tests: bool
    findings: list[dict] = field(default_factory=list)
    error: str | None = None


def _points_at(finding: Finding, file: str, text: str) -> bool:
    named = (finding.file or "").strip().removeprefix("./")
    if named and (named == file or named.endswith("/" + file) or file.endswith("/" + named)):
        return True
    return file.lower() in text


def names_defect(finding: Finding, defect: Defect, invariants: Sequence[str]) -> bool:
    """Whether `finding` points at the defect's file and names the defect: one of its class's
    safety invariants, or one of the defect's `match` phrases in what the finding says. The
    finding's `quote` is left out: it quotes the task, which never names the defect."""
    text = " ".join(part for part in (finding.title, finding.observed, finding.expected, finding.invariant) if part)
    text = text.lower()
    if not _points_at(finding, defect.file, text):
        return False
    invariant = (finding.invariant or "").strip().lower()
    return invariant in {i.lower() for i in invariants} or any(word.lower() in text for word in defect.match)


def score(defect: Defect, result: ExamResult, invariants: Sequence[str]) -> Outcome:
    return Outcome(
        defect=defect.id,
        cls=defect.cls,
        caught=any(names_defect(f, defect, invariants) for f in result.findings),
        certified=result.certify,
        caught_by_tests=defect.caught_by_tests,
        findings=[
            {k: getattr(f, k) for k in ("title", "item", "invariant", "file", "line", "observed", "expected")}
            for f in result.findings
        ],
    )


def catch_rates(outcomes: Sequence[Outcome], classes: Sequence[str] = ()) -> dict[str, dict]:
    """Per class: the defects examined, those caught, the rate, and the examinations that failed
    (counted apart: a call that failed is no answer)."""
    rates: dict[str, dict] = {name: {"examined": 0, "caught": 0, "failed": 0} for name in classes}
    for outcome in outcomes:
        row = rates.setdefault(outcome.cls, {"examined": 0, "caught": 0, "failed": 0})
        if outcome.error is not None:
            row["failed"] += 1
            continue
        row["examined"] += 1
        row["caught"] += int(outcome.caught)
    for row in rates.values():
        row["rate"] = row["caught"] / row["examined"] if row["examined"] else None
    return rates


# ---------------------------------------------------------------------------- the scratch worktree


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


@contextmanager
def scratch_worktree(repo: Path, commit: str) -> Generator[Path]:
    """A throwaway working tree at `commit`, in a new temporary directory: a fresh repository with
    no branch, no tag and no remote, whose detached HEAD is `commit`, reading `repo`'s objects
    through an alternate. Nothing is written into `repo`. Deleted on exit."""
    if git("cat-file", "-e", f"{commit}^{{commit}}", cwd=repo, check=False).returncode != 0:
        raise Refused(f"the recorded commit {commit} is not in this repository: fetch main")
    objects = git("rev-parse", "--path-format=absolute", "--git-path", "objects", cwd=repo).stdout.strip()
    root = Path(tempfile.mkdtemp(prefix="examiner-pilot-"))
    tree = root / "worktree"
    try:
        git("init", "--quiet", str(tree), cwd=root)
        (tree / ".git" / "objects" / "info" / "alternates").write_text(objects + "\n", encoding="utf-8")
        git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit, cwd=tree)
        yield tree
    finally:
        shutil.rmtree(root, ignore_errors=True)


def patch_problems(defect: Defect, tree: Path) -> list[str]:
    """Why `defect`'s patch cannot be applied in `tree` as its entry says, if it cannot."""
    if not defect.patch.is_file():
        return [f"{defect.id}: no patch file {defect.patch.name}"]
    applied = git("apply", "--check", str(defect.patch), cwd=tree, check=False)
    if applied.returncode != 0:
        return [f"{defect.id}: does not apply to the recorded commit: {applied.stderr.strip()[:300]}"]
    numstat = git("apply", "--numstat", str(defect.patch), cwd=tree).stdout.splitlines()
    files = [line.split("\t")[-1] for line in numstat]
    if files != [defect.file]:
        return [f"{defect.id}: changes {', '.join(files) or 'nothing'}, not only {defect.file}"]
    return []


def check(repo: Path, manifest: Manifest) -> None:
    """Every patch applies to the recorded commit and changes only its entry's file, or `Refused`."""
    with scratch_worktree(repo, manifest.commit) as tree:
        problems = [problem for defect in manifest.defects for problem in patch_problems(defect, tree)]
    if problems:
        raise Refused("\n".join(problems))


# ---------------------------------------------------------------------------- the examination


def request(number: int, defect: Defect, commit: str, facts: Mapping) -> IssueRequest:
    """What the examiner is told: an issue whose one deliverable is the defect's task. Neither the
    defect nor the pilot is named."""
    body = f"## Deliverables\n\n- [ ] {defect.task}\n"
    issue = Issue(number=number, title=defect.task, state="open", body=body, labels=[], db_id=0)
    context = (
        f"The change is not committed: it is in your working directory, on top of commit {commit}, "
        f"and `git diff {commit}` shows it."
    )
    return IssueRequest(issue=issue, spec=issue_parser.parse(issue), context=context, facts=dict(facts))


def plan_for(defect: Defect) -> dict:
    item = {"id": "D1", "reading": defect.task, "quote": defect.task, "proof": "review", "capability": None,
            "agent_part": None, "depends_on": None, "depends_quote": None}
    return {"items": [item], "zones": [defect.file], "notes": ""}


AgentsFor = Callable[[Path], Agents]


def run(repo: Path, manifest: Manifest, agents_for: AgentsFor, examiner: str, facts: Mapping) -> list[Outcome]:
    """Examine every defect, in a scratch worktree of its own. Refuses before any examination when a
    patch does not apply (`check`)."""
    check(repo, manifest)
    outcomes = []
    for number, defect in enumerate(manifest.defects, 1):
        with scratch_worktree(repo, manifest.commit) as tree:
            git("apply", str(defect.patch), cwd=tree)
            try:
                result = agents_for(tree).examine(examiner, request(number, defect, manifest.commit, facts),
                                                  plan_for(defect), tree, manifest.commit, manifest.commit,
                                                  [], [], [], None)
            except AgentFailure as e:
                outcomes.append(Outcome(defect.id, defect.cls, caught=False, certified=False,
                                        caught_by_tests=defect.caught_by_tests, error=str(e)))
                continue
        outcomes.append(score(defect, result, manifest.classes[defect.cls]))
    return outcomes


# ---------------------------------------------------------------------------- the live examiner


class ScratchWorkspace:
    """What the engine's live agents ask of its `Workspace`, for one scratch worktree. The examiner
    works in the patched worktree itself, since a copy of a commit would not hold a change that is
    not committed; it may read this repository's git directory, and write none of it."""

    def __init__(self, tree: Path, repo: Path, run_dir: Path) -> None:
        self.tree = tree
        self.dir = run_dir
        self._common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo).stdout.strip())

    def common_dir(self) -> Path:
        return self._common

    def coord_path(self) -> Path:
        return self.tree

    @contextmanager
    def disposable(self, from_worktree: Path) -> Generator[Path]:
        yield from_worktree


class PilotAgents(LiveAgents):
    """The engine's live agents, shown the change in the working tree rather than between commits."""

    def _diff(self, worktree: Path, base: str, head: str) -> str:
        diff = subprocess.run(["git", "diff", base], cwd=worktree, capture_output=True, text=True,
                              check=False).stdout
        if len(diff) > DIFF_LIMIT:
            diff = diff[:DIFF_LIMIT] + f"\n[diff truncated at {DIFF_LIMIT} characters; read the files directly]"
        return diff or "(empty)"


def live_agents(repo: Path, run_dir: Path, examiner: str) -> Callable[[Path], PilotAgents]:
    """The engine's live agents with `examiner` as the other vendor, one per scratch worktree, with
    package caches of the run's own under `run_dir`, as the loop gives its agents."""
    operator = "claude" if examiner == "codex" else "codex"
    caches = run_dir / "cache"
    env = {"UV_CACHE_DIR": str(caches / "uv"), "npm_config_cache": str(caches / "npm"), "UV_LINK_MODE": "copy"}
    sandbox.cache_dirs(env)
    return lambda tree: PilotAgents(operator, run_dir, ScratchWorkspace(tree, repo, run_dir), extra_env=env)


# ---------------------------------------------------------------------------- the command


def report(manifest: Manifest, outcomes: Sequence[Outcome], examiner: str) -> dict:
    return {
        "commit": manifest.commit,
        "examiner": examiner,
        "rates": catch_rates(outcomes, list(manifest.classes)),
        "defects": [asdict(o) for o in outcomes],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the examiner on the seeded defects of defects/.")
    parser.add_argument("--examiner", choices=VENDORS, default="codex", help="the vendor that examines")
    parser.add_argument("--check", action="store_true",
                        help="apply every patch at the recorded commit and stop: no examination")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--out", type=Path,
                        help="where the report and the calls go, outside the repository (default: a new temporary "
                             "directory)")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    try:
        if args.check:
            check(REPO, manifest)
            per_class = {name: sum(d.cls == name for d in manifest.defects) for name in manifest.classes}
            print(f"{len(manifest.defects)} defects apply at {manifest.commit[:12]}: "
                  + ", ".join(f"{name} {n}" for name, n in per_class.items()))
            return 0
        out = (args.out or Path(tempfile.mkdtemp(prefix="examiner-pilot-run-"))).resolve()
        if out.is_relative_to(REPO):
            print(f"examiner-pilot: --out {out} is inside the repository", file=sys.stderr)
            return 2
        out.mkdir(parents=True, exist_ok=True)
        policy = load_policy(ENGINE / "loop.toml")
        facts = {"lacks": policy["lacks"], "invariants": policy["invariants"]}
        outcomes = run(REPO, manifest, live_agents(REPO, out, args.examiner), args.examiner, facts)
    except Refused as e:
        print(f"examiner-pilot: refused:\n{e}", file=sys.stderr)
        return 2
    data = report(manifest, outcomes, args.examiner)
    (out / "report.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    for name, row in data["rates"].items():
        rate = "n/a" if row["rate"] is None else f"{row['rate']:.0%}"
        print(f"{name:20} caught {row['caught']}/{row['examined']} ({rate}), failed calls {row['failed']}")
    print(f"report: {out / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
