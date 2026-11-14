import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))
from helpers import SHIP, TempRepo, git, issue_body

NAME = "exam_" + Path(__file__).stem
PACKAGE = ROOT / "tools/implementation-loop/implement_loop"
spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
package = importlib.util.module_from_spec(spec)
sys.modules[NAME] = package
spec.loader.exec_module(package)
L = SimpleNamespace(**{n: __import__(NAME + "." + n, fromlist=[n]) for n in
    ("redact", "github", "cli", "agents", "config", "sandbox", "graph", "model", "state", "workspace", "engine", "supervisor")})
L.config.STANDING_CHECKS = L.config.HISTORY_CHECKS = ()
L.sandbox.use(L.sandbox.PassThrough())
ACCOUNT = "".join(("9274", "6203", "8501"))
ARN = "arn:aws:iam::" + ACCOUNT + ":role/example"
TEXT = "target " + ACCOUNT + " " + ARN

from contextlib import contextmanager


class Agents(L.agents.FakeAgents):
    crash = False

    def plan(self, *args):
        result = super().plan(*args)
        result.decisions.append({"id": "target", "choice": TEXT, "rationale": "test output"})
        return result

    def _test_script(self, n):
        return super()._test_script(n) + "echo target " + "$(printf %s 9274 6203 8501)" + " arn:aws:iam::$(printf %s 9274 6203 8501):role/example\n"

    def build(self, vendor, req, *args):
        assert (args[1] / "tools/hygiene/scan.py").is_file()
        if self.crash and req.issue.number == 3:
            raise L.agents.AgentFailure(TEXT, vendor=vendor)
        return super().build(vendor, req, *args)

    def examine(self, *args):
        result = super().examine(*args)
        for finding in result.findings:
            finding.observed = TEXT
        return result

@contextmanager
def fixture(owner=False, fault=False):
    repo = TempRepo()
    fake = L.github.FakeGitHub(on_merge=repo.merge)
    items = ["`docs/value.txt` contains `ok`."]
    if owner:
        items.append("Demonstrated on special hardware.")
    fake.add_issue(2, "Example task", issue_body("feat/example", items, ["Checks pass."]), labels=("size:M",))
    scripts = {2: L.agents.Script(files={"docs/value.txt": "ok"}, owner_items=("D2",) if owner else (), unquoted_findings=1)}
    if fault:
        fake.add_issue(3, "Faulting task", issue_body("feat/fault", ["`docs/fault.txt` contains `ok`."], ["Checks pass."]), labels=("size:M",))
        scripts[3] = L.agents.Script(files={"docs/fault.txt": "ok"})
    fake.add_issue(1, "Example epic", "An epic.", subs=tuple(scripts))
    store = L.state.RunStore.for_root(repo.work, 1)

    def run(mask=None):
        gh = L.github.RedactingGitHub(fake) if mask is None else L.github.RedactingGitHub(fake, mask)
        agents = Agents(scripts)
        agents.crash = fault
        engine = L.engine.Engine(L.graph.build_plan(gh, 1, repo.work), store, gh,
            L.workspace.Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir), agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0, infra_budget=1)),
            repo_root=repo.work, log=lambda _: None, sleep=lambda _: None, policy={"lacks": ["hardware:laptop"]})
        return engine.run()
    try:
        yield repo, fake, store, run
    finally:
        repo.cleanup()

def test_builder_worktree_scanner_cannot_change_redaction():
    with fixture() as (repo, fake, store, run):
        trace = repo.tmp / "scanner-was-run"
        scanner = repo.merger / "tools/hygiene/scan.py"
        scanner.parent.mkdir(parents=True)
        scanner.write_text("import sys\nfrom pathlib import Path\nPath(" + repr(str(trace)) + ").touch()\nprint(sys.stdin.read())\n")
        git("add", "tools", cwd=repo.merger)
        git("commit", "-q", "-m", "test worktree scanner", cwd=repo.merger)
        git("push", "-q", "origin", "HEAD:main", cwd=repo.merger)
        git("pull", "-q", cwd=repo.work)
        run()
        assert store.issue(2).phase == L.model.Phase.DONE
        assert not trace.exists()
        bodies = [p["body"] for p in fake.prs.values()]
        assert len(bodies) == 1 and "[redacted " in bodies[0]
        assert ACCOUNT not in bodies[0] and ARN not in bodies[0]
