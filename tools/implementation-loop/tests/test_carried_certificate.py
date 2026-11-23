"""A certified head that the land step brings up to date with main without a conflict keeps the
certificate of the head it merged (ADR 0001, amendment 14): the merged head is verified again and
lands with no new examination. A merged head whose verification fails goes back to the builder,
and the head that resolves a conflict with main is examined, as before.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are fakes. Main moves once the examiner has certified, so the land step finds it moved."""

from contextlib import contextmanager
from dataclasses import replace
from shlex import quote

import pytest

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
    def __init__(self, script, caps):
        self.caps = caps
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
               Config(repo="test/test", caps=Caps(ci_poll_s=0, queue_poll_s=0, **self.caps)), repo_root=self.repo.work,
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
def loop(script=None, **caps):
    run = Loop(script or Script(files={f"docs/f{ISSUE}.txt": f"text {ISSUE}"}), caps)
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


def test_a_merged_head_that_failed_verification_does_not_carry_after_its_command_is_repaired():
    with loop() as run:
        path = "docs/format.txt"
        run.main_moves(path, "first")
        run.then_main_moves((path, "second"))
        run.agents.extra_check = " && grep -qx first docs/format.txt"   # true of the certified head only

        def use_current_format(worktree):   # the builder repairs its command, not the commit
            if (worktree / path).read_text() == "second":
                run.agents.extra_check = " && grep -qx second docs/format.txt"
        run.agents.fix = use_current_format
        run.run()

        [failed] = [e for e in run.events("verified") if not e["ok"]]
        assert run.events("round_failed") and not run.events("certificate_carried")
        assert failed["head"] not in run.merged() or failed["head"] in run.agents.examined


def test_a_run_stopped_once_a_merged_head_failure_is_saved_does_not_carry_it(tmp_path, monkeypatch):
    with loop() as run:
        available = tmp_path / "available"   # what the check reads changes; the head does not
        run.then_main_moves(("docs/later.txt", "later\n"))
        run.agents.extra_check = f" && (test ! -f docs/later.txt || test -f {quote(str(available))})"
        put, failed = run.store.put, []

        def stop_once_failure_saved(st):   # the run stops before the failure reaches the journal
            put(st)
            if not failed and any(e.get("passed") is False for e in st.evidence):
                failed.append(run.events("updated_from_main")[-1]["to"])
                (run.store.dir / "STOP").touch()
                raise L.agents.AgentStopped("stopped once the failed evidence was saved")

        with monkeypatch.context() as patch:
            patch.setattr(run.store, "put", stop_once_failure_saved)
            run.run()
        [head] = failed
        available.touch()   # the same check passes when the run resumes
        run.run()

        assert run.store.issue(ISSUE).phase == Phase.DONE
        assert head not in run.merged() or head in run.agents.examined


@pytest.mark.parametrize("case", ["certified", "withheld", "main moves"])
def test_a_carried_head_that_fails_a_later_verification_lands_only_on_a_new_certificate(case, tmp_path, monkeypatch):
    with loop() as run:
        run.then_main_moves(("docs/later.txt", "later\n"))
        outside = tmp_path / "format"   # what the check reads changes; the head does not
        outside.write_text("first\n")
        run.agents.extra_check = f" && grep -qx first {quote(str(outside))}"
        merge, examine = run.fake.merge_pr, run.agents.examine

        def refuse_once(*args):   # GitHub refuses the carried head's merge once: it is verified again
            monkeypatch.setattr(run.fake, "merge_pr", merge)
            outside.write_text("second\n")
            if case == "main moves":   # and main moves while the builder repairs
                run.main_moves("docs/second.txt", "second\n")
            raise L.github.GitHubError("merge refused")

        def repair_command(worktree):   # the builder repairs its command, not the commit
            if outside.read_text() == "second\n":
                run.agents.extra_check = f" && grep -qx second {quote(str(outside))}"

        def withhold_once(*args):   # the examination after the failure withholds its certificate
            result = examine(*args)
            if case == "withheld" and len(run.agents.examined) == 2:
                result.certify = False
            return result
        monkeypatch.setattr(run.fake, "merge_pr", refuse_once)
        monkeypatch.setattr(run.agents, "examine", withhold_once)
        run.agents.fix = repair_command
        run.run()

        [carry, *_] = run.events("certificate_carried")
        [failed] = [v for v in run.events("verified") if not v["ok"]]
        assert failed["head"] == carry["head"] and run.store.issue(ISSUE).phase == Phase.DONE
        later = [e for e in run.store.events() if e.get("issue") == ISSUE and e["seq"] > failed["seq"]]
        certified = [e["head"] for e in later if e["kind"] == "examined" and e["certify"]]
        [merged] = run.merged()   # the one merge request that reached GitHub after the refusal
        assert certified, "a new examination certified after the failure"
        assert merged == certified[-1] or any(
            e["kind"] == "certificate_carried" and e["head"] == merged and e["certified"] == certified[-1] for e in later)


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


def test_a_clean_merge_shipping_makes_at_the_push_carries_the_certificate(monkeypatch):
    with loop() as run:
        run.then_main_moves(("docs/first.txt", "first\n"))
        push = run.ws.push

        def move_and_push(path):   # main moves again between the land step's update and its push
            monkeypatch.setattr(run.ws, "push", push)
            run.main_moves("docs/second.txt", "second\n")
            return push(path)

        monkeypatch.setattr(run.ws, "push", move_and_push)
        run.run()

        [merged] = run.merged()
        certified, shipped_main = run.parents(merged)
        assert any(v["ok"] and v["head"] == merged for v in run.events("verified"))
        assert len(run.agents.examined) == 1
        assert [(e["certified"], e["main"]) for e in run.events("certificate_carried") if e["head"] == merged] == [
            (certified, shipped_main)]


def test_a_run_resumed_after_the_merged_head_passed_verification_carries_the_certificate(monkeypatch):
    with loop() as run:
        run.then_main_moves(("docs/later.txt", "later\n"))
        record = run.store.event

        def interrupt(kind, **fields):
            record(kind, **fields)
            if kind == "verified" and fields["ok"] and run.events("updated_from_main"):
                (run.store.dir / "STOP").touch()
                raise L.agents.AgentStopped("interrupted after verification")

        with monkeypatch.context() as patch:
            patch.setattr(run.store, "event", interrupt)
            run.run()
        run.run()

        assert run.store.issue(ISSUE).phase == Phase.DONE
        assert len(run.agents.examined) == 1 and len(run.events("certificate_carried")) == 1


def test_the_pull_request_attributes_the_examination_to_the_head_examined():
    with loop() as run:
        run.then_main_moves(("docs/later.txt", "later\n"))
        run.run()

        [pr] = run.fake.prs.values()
        [examined] = run.agents.examined
        assert pr["head"]["sha"] != examined and examined in pr["body"]
        assert "certified on this exact head" not in pr["body"]


def test_a_certificate_of_the_examiner_bound_is_not_attributed_to_the_examiner():
    # past the bound, the engine certifies the first examination although the examiner withholds
    with loop(Script(files={f"docs/f{ISSUE}.txt": f"text {ISSUE}"}, no_certify=1), examiner_bound=1) as run:
        run.then_main_moves(("docs/later.txt", "later\n"))
        run.run()

        [record] = run.events("certified_by_bound")
        [pr] = run.fake.prs
        [evidence] = [c["body"] for c in run.fake.comments[pr] if L.report.EVIDENCE_MARKER in c["body"]]
        assert record["head"] in evidence and run.events("certificate_carried")
        assert "codex certified it" not in evidence.lower() and "examiner bound" in evidence


def test_a_run_that_adopts_the_delivery_keeps_the_examined_head_in_the_evidence(monkeypatch):
    from test_deploying import DEPLOYED, SIGNS_IN, SMOKE, VERIFIED
    from test_deploying import Agents as DeployingAgents
    from test_deploying import loop as deploying_loop
    from test_discharge import RHI, push_main

    with deploying_loop(contexts=(), verified=VERIFIED, lacks=(DEPLOYED,), unverified=()) as run:
        run.deploy = replace(run.deploy, discharge_contexts=(RHI,))
        run.add_work(2, owner=((SIGNS_IN, DEPLOYED),), files=(SMOKE,))
        examine = DeployingAgents.examine

        def move_main(*args, **kwargs):   # main moves once the examiner has certified
            result = examine(*args, **kwargs)
            push_main(run)
            return result

        monkeypatch.setattr(DeployingAgents, "examine", move_main)
        run.run()
        [examination] = run.events("examined", 2)
        head, pr = examination["head"], run.issue(2).pr
        assert run.events("certificate_carried", 2) and head in run.comment(pr, L.report.EVIDENCE_MARKER)
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        init = DeployingAgents.__init__

        def other_operator(self, *args, **kwargs):   # the adopting run's operator is the other vendor
            init(self, *args, **kwargs)
            self.operator, self.other = "codex", "claude"

        monkeypatch.setattr(DeployingAgents, "__init__", other_operator)
        run.run(root=2)   # another run, rooted at the issue, discharges the owner's item

        assert run.issue(2, root=2).phase == Phase.DONE
        evidence = run.comment(pr, L.report.EVIDENCE_MARKER)
        assert head in evidence
        assert "claude examined" not in evidence.lower() and "claude certified" not in evidence.lower()


@pytest.mark.parametrize("stop_after", ["verified", "certificate_revoked"])
def test_a_run_resumed_after_a_carried_head_failed_verification_examines_it_before_it_lands(
        stop_after, tmp_path, monkeypatch):
    with loop() as run:
        run.then_main_moves(("docs/later.txt", "later\n"))
        available = tmp_path / "available"   # what the check reads changes; the head does not
        available.touch()
        run.agents.extra_check = f" && test -f {quote(str(available))}"
        merge, record = run.fake.merge_pr, run.store.event

        def refuse_once(*args):   # GitHub refuses the carried head's merge once: it is verified again, and fails
            monkeypatch.setattr(run.fake, "merge_pr", merge)
            available.unlink()
            raise L.github.GitHubError("merge refused")

        def interrupt(kind, **fields):   # the run stops right after the failure is journaled
            record(kind, **fields)
            if kind == stop_after and not fields.get("ok", False):
                (run.store.dir / "STOP").touch()
                raise L.agents.AgentStopped("interrupted after the failed verification")

        with monkeypatch.context() as patch:
            patch.setattr(run.fake, "merge_pr", refuse_once)
            patch.setattr(run.store, "event", interrupt)
            run.run()
        [failed] = [e for e in run.events("verified") if not e["ok"]]
        [carried] = run.events("certificate_carried")
        assert failed["head"] == carried["head"]

        available.touch()   # the same check passes when the run resumes
        run.run()

        assert run.store.issue(ISSUE).phase == Phase.DONE
        [merged] = run.merged()
        later = [e for e in run.store.events() if e.get("issue") == ISSUE and e["seq"] > failed["seq"]]
        certified = [e["head"] for e in later if e["kind"] == "examined" and e["certify"]]
        assert certified, "a new examination certified after the failure"
        assert merged == certified[-1] or any(
            e["kind"] == "certificate_carried" and e["head"] == merged and e["certified"] == certified[-1] for e in later)
