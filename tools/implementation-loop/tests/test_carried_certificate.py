"""A certified head that the land step brings up to date with main without a conflict keeps the
certificate of the head it merged (ADR 0001, amendment 14): the merged head is verified again and
lands with no new examination. A merged head whose verification fails goes back to the builder,
and the head that resolves a conflict with main is examined, as before.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are fakes. Main moves once the examiner has certified, so the land step finds it moved."""

from contextlib import contextmanager
from dataclasses import replace

from helpers import SHIP, TempRepo, git, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

FakeAgents, Script = L.agents.FakeAgents, L.agents.Script
Caps, Config = L.config.Caps, L.config.Config
Engine = L.engine.Engine
FakeGitHub = L.github.FakeGitHub
build_plan = L.graph.build_plan
Phase = L.model.Phase
RunStore = L.state.RunStore
Workspace = L.workspace.Workspace

EPIC, ISSUE = 1, 2


class Agents(FakeAgents):
    """The fake agents, with the heads the examiner saw, a hook run once after the first
    examination, a check added to every builder command, and a fix the builder makes."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.examined: list[str] = []
        self.after_certificate = lambda: None
        self.extra_check = ""
        self.fix = lambda worktree: None

    def build(self, vendor, req, plan, worktree, feedback):
        self.fix(worktree)
        result = super().build(vendor, req, plan, worktree, feedback)
        result.commands = [replace(c, command=f"{c.command}{self.extra_check}") for c in result.commands]
        return result

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        self.examined.append(head_sha)
        result = super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)
        hook, self.after_certificate = self.after_certificate, lambda: None
        hook()
        return result


class MovingWorkspace(Workspace):
    """Each update from main first runs the next queued move of main, if any."""

    moves: list

    def update(self, path):
        if self.moves:
            self.moves.pop(0)()
        return super().update(path)


class Loop:
    def __init__(self, script):
        self.repo = TempRepo()
        self.fake = FakeGitHub(on_merge=self.repo.merge)
        body = issue_body(f"feat/issue-{ISSUE}", [f"`docs/f{ISSUE}.txt` contains `text {ISSUE}`."], ["The test passes."])
        self.fake.add_issue(ISSUE, f"Issue {ISSUE}", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))
        self.agents = Agents({ISSUE: script})
        self.ws = MovingWorkspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.tmp / "loop")
        self.ws.moves = []
        self.store = RunStore.for_root(self.repo.work, EPIC)

    def run(self):
        Engine(build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake, self.ws, self.agents,
               Config(repo="test/test", caps=Caps(ci_poll_s=0, queue_poll_s=0)), repo_root=self.repo.work,
               log=lambda m: None, sleep=lambda s: None, policy={"lacks": []}).run()

    def main_moves(self, path, text):
        clone = self.repo.merger
        git("fetch", "-q", "origin", cwd=clone)
        git("reset", "-q", "--hard", "origin/main", cwd=clone)
        (clone / path).parent.mkdir(parents=True, exist_ok=True)
        (clone / path).write_text(text)
        git("add", "--all", cwd=clone)
        git("commit", "-q", "-m", f"main moves: {path}", cwd=clone)
        git("push", "-q", "origin", "HEAD:main", cwd=clone)
        return git("rev-parse", "HEAD", cwd=clone)

    def then_main_moves(self, *moves):
        """Once the examiner has certified, each next update from main finds main moved by one of `moves`."""
        self.agents.after_certificate = lambda: self.ws.moves.extend(lambda m=m: self.main_moves(*m) for m in moves)

    def events(self, kind):
        return [e for e in self.store.events() if e.get("issue") == ISSUE and e["kind"] == kind]

    def merged(self):
        return [c[2] for c in self.fake.calls if c[0] == "merge_pr"]

    def parents(self, sha):
        return git("rev-list", "--parents", "-n1", sha, cwd=self.repo.work).split()[1:]


@contextmanager
def loop(script=None):
    run = Loop(script or Script(files={f"docs/f{ISSUE}.txt": f"text {ISSUE}"}))
    try:
        yield run
    finally:
        run.repo.cleanup()


def test_a_clean_update_from_main_lands_on_the_certificate_of_the_head_it_merged():
    with loop() as run:
        run.then_main_moves(("docs/one.txt", "one\n"), ("docs/two.txt", "two\n"))   # main moves twice
        run.run()

        assert run.store.issue(ISSUE).phase == Phase.DONE
        [certified] = run.agents.examined   # the examiner is not called again
        updates = [e for e in run.events("updated_from_main") if e["phase"] == "land"]
        carries = run.events("certificate_carried")
        assert [(e["certified"], e["head"], e["main"]) for e in carries] == [
            (certified, updates[0]["to"], updates[0]["main"]), (updates[0]["to"], updates[1]["to"], updates[1]["main"])]
        for e in carries:   # each merged head merges main into the head whose certificate it carries
            assert run.parents(e["head"]) == [e["certified"], e["main"]]
            assert any(v["head"] == e["head"] and v["ok"] for v in run.events("verified")), "verified again"
        assert run.merged() == [carries[-1]["head"]]
        assert run.repo.main_file("docs/one.txt") == "one" and run.repo.main_file(f"docs/f{ISSUE}.txt") == f"text {ISSUE}"
        [pr] = run.fake.prs
        evidence = [c["body"] for c in run.fake.comments[pr] if "Evidence for" in c["body"]]
        assert evidence and certified in evidence[-1], "the evidence comment names the head the examiner certified"


def test_a_merged_head_that_fails_verification_goes_back_to_the_builder():
    with loop() as run:
        run.agents.extra_check = " && test ! -e docs/breaks.txt"   # main's change breaks the issue's check
        run.then_main_moves(("docs/breaks.txt", "broken\n"))

        def fix(worktree):
            (worktree / "docs/breaks.txt").unlink(missing_ok=True)
        run.agents.fix = fix
        run.run()

        [update] = [e for e in run.events("updated_from_main") if e["phase"] == "land"]
        merged_head = update["to"]
        assert [v["ok"] for v in run.events("verified") if v["head"] == merged_head] == [False]
        assert not run.events("certificate_carried")
        assert any(e["kind"] == "round_failed" or e.get("to") == "build" for e in run.store.events()
                   if e.get("issue") == ISSUE and e["seq"] > update["seq"]), "a failed round, back to the builder"
        assert merged_head not in run.merged() and merged_head not in [p["head"]["sha"] for p in run.fake.prs.values()]
        # the builder's next head is examined before it lands
        assert len(run.agents.examined) == 2 and run.merged() == [run.agents.examined[1]] != [merged_head]


def test_a_conflicting_update_goes_to_the_builder_and_its_resolution_is_examined():
    shared = "shared/suites.txt"
    with loop(Script(files={f"docs/f{ISSUE}.txt": f"text {ISSUE}"}, lines={shared: f"suite-for-{ISSUE}"})) as run:
        run.main_moves(shared, "base-suite\n")
        run.then_main_moves((shared, "base-suite\nmain-suite\n"))
        run.run()

        assert [e["phase"] for e in run.events("conflict")][:1] == ["land"]
        assert not run.events("certificate_carried")
        certified, resolved = run.agents.examined
        main_moved = git("rev-parse", "origin/main~1", cwd=run.repo.merger)   # main before the squash merge
        assert git("merge-base", "--is-ancestor", main_moved, resolved, cwd=run.repo.work) == ""
        assert resolved != certified and run.merged() == [resolved]
        assert run.store.issue(ISSUE).phase == Phase.DONE
