"""Protected paths (ADR 0001, decision 10): a builder changes one only when the issue names it
or the examiner approves a typed request. Any other file is the builder's to change: nothing
restricts an issue to the paths its plan declared.

A protected path is granted when the issue's own text names it: that path, or a protected
directory above it. An issue that names a wider directory (`scripts/`, `tools/`) has not named
the gates inside it."""

import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import present
from implement_loop import config as loaded
from implement_loop import gate, issue_parser
from implement_loop.agents import ItemPlan, PlanResult
from implement_loop.config import Caps, is_protected, is_safety_surface
from implement_loop.engine import Engine
from implement_loop.model import IssueState, Phase
from implement_loop.state import RunStore
from test_engine import EngineCase

TEXT = {"D1": "the gate script gains a step", "A1": "the new step passes on the change"}
DEPLOYMENT = ["src/rhi/pipeline/stack.py", "setup/bootstrap/policies.yaml"]   # ADR 0001, amendment 1
LISTS = ("PROTECTED_PREFIXES", "PROTECTED_FILES", "SAFETY_SURFACE_PREFIXES", "SAFETY_SURFACE_FILES")


def body(*named: str) -> str:
    lines = ["## Deliverables", *[f"- [ ] `{path}` is changed as described." for path in named],
             "", "## Acceptance criteria", "- [ ] `scripts/check.sh fast` passes; see `docs/factory/operating.md`."]
    return "\n".join(lines) + "\n"


def problems(changed: list[str], st: IssueState, repair: bool = False) -> list[str]:
    """What the verify step says of these changed files: every one is in the builder's lane."""
    return gate.zone_problems(changed, st.grants, changed, repair)


class Agents:
    def plan(self, vendor, req, wt):
        items = [ItemPlan(id=k, reading="r", quote=v, proof="review") for k, v in TEXT.items()]
        return PlanResult(items=items, zones=[".github/workflows/ci.yml", "scripts/check.sh"], decisions=[])

    def triage(self, vendor, req, plan):
        return []


class PlanGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-protected-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plan(self, issue_body: str, repair=False, grants=()) -> IssueState:
        engine = Engine.__new__(Engine)
        engine.store = RunStore(self.tmp / "run")
        engine.caps, engine.repair, engine.other, engine.op = Caps(), repair, "codex", "claude"
        engine.lacks, engine._lookup_issue = [], lambda n: None   # ty: ignore[invalid-assignment]  # test double
        ledger = [SimpleNamespace(id=k, text=v) for k, v in TEXT.items()]
        engine.plan = SimpleNamespace(specs={7: SimpleNamespace(ledger=ledger, declared_paths=[])},  # ty: ignore[invalid-assignment]  # test double
                                      issues={7: SimpleNamespace(body=issue_body, labels=[])}, work=[7])
        engine.agents = Agents()  # ty: ignore[invalid-assignment]  # test double
        engine.store.put(IssueState(number=7, phase=Phase.PLAN, grants=list(grants)))
        with mock.patch.object(Engine, "_req"), mock.patch.object(Engine, "_set_phase"), \
                mock.patch("implement_loop.engine.scheduler.prerequisites_met", return_value=True):
            engine._step_plan(7)
        return engine.store.issue(7)

    @staticmethod
    def granted(st: IssueState) -> dict[str, bool]:
        return {g["path"]: g["protected"] for g in st.grants}

    def judge_by_this_trees_lists(self):
        """The lists a test of a change to them judges are those of the `config.py` beside these
        tests, read from the file. The verifier's pytest plugin belongs to the engine that runs the
        loop, and loading it imports that engine's `implement_loop`. It drops the package again
        only in the pytest that records, not in an xdist worker, so the tests of
        `scripts/check.sh engine-tests` would find there the lists of the running engine."""
        mine = runpy.run_path(str(Path(__file__).resolve().parent.parent / "implement_loop" / "config.py"))
        patch = mock.patch.multiple(loaded, **{name: mine[name] for name in LISTS})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_protected_path_the_issue_does_not_name_is_not_granted(self):
        st = self.plan(body("infra/stack.py", "docs/a.md"))
        self.assertEqual(self.granted(st), {}, "the plan cannot reach a gate, and other files need no grant")
        self.assertFalse([d for d in st.decisions if d["id"] == "protected-paths"])
        self.assertEqual(problems(["infra/stack.py", "docs/b.md", "scripts/check.sh"], st),
                         [("protected paths changed without approval: ['scripts/check.sh'] "
                           "(ask with a `protected_change` request)")])

    def test_a_protected_path_the_issue_names_is_granted_as_protected(self):
        st = self.plan(body("scripts/check.sh", "tools/hygiene/", "docs/a.md"))
        self.assertEqual(self.granted(st), {"scripts/check.sh": True, "tools/hygiene": True},
                         "the file by its name, and the directory the issue names")
        [decision] = [d for d in st.decisions if d["id"] == "protected-paths"]
        self.assertIn("protected paths the issue names are granted: scripts/check.sh, tools/hygiene", decision["choice"])
        self.assertEqual(problems(["scripts/check.sh", "tools/hygiene/scan.py", "docs/a.md", "src/x.py"], st), [])

    def test_naming_one_file_does_not_grant_the_directory_around_it(self):
        st = self.plan(body("tools/hygiene/scan.py"))
        self.assertEqual(self.granted(st), {"tools/hygiene/scan.py": True})
        self.assertIn("['tools/hygiene/hygiene.toml']", "\n".join(problems(["tools/hygiene/hygiene.toml"], st)))

    def test_naming_a_wider_directory_does_not_name_the_protected_paths_inside_it(self):
        st = self.plan(body("scripts/", "tools/", ".github-notes/a.md"))
        self.assertEqual(self.granted(st), {}, "`scripts/` is not `scripts/check.sh`, and `tools/` is not `tools/hygiene/`")
        self.assertEqual(problems(["scripts/check.sh", "scripts/new.sh", "tools/backlog/x.py", "tools/hygiene/scan.py"], st),
                         [("protected paths changed without approval: ['scripts/check.sh', 'tools/hygiene/scan.py'] "
                           "(ask with a `protected_change` request)")])

    def test_a_directory_inside_a_protected_one_names_what_lies_under_it(self):
        st = self.plan(body(".github/workflows/"))
        self.assertEqual(self.granted(st), {".github/workflows": True})
        self.assertIn("['.github/dependabot.yml']",
                      "\n".join(problems([".github/workflows/ci.yml", ".github/dependabot.yml"], st)))

    def test_the_agents_own_configuration_is_protected_like_the_gates(self):
        # ADR 0001, amendment 6: a hook or a setting added there runs in the owner's own session
        config = [".claude/settings.json", ".claude/skills/implement/SKILL.md", ".agents/skills/ship/SKILL.md",
                  ".codex/config.toml"]
        for path in config:
            self.assertTrue(is_protected(path), path)
            self.assertTrue(is_safety_surface(path), path)
        self.assertFalse(is_protected(".claudeignore") or is_protected("docs/.claude/notes.md"), "the directories, at the root")
        st = self.plan(body("docs/a.md"))
        self.assertEqual(self.granted(st), {})
        self.assertIn(f"protected paths changed without approval: {config}", "\n".join(problems(["docs/a.md", *config], st)))

    def test_an_issue_can_name_a_path_in_each_of_those_directories(self):
        named = body(".claude/skills/backlog/", ".agents/skills/backlog/SKILL.md", ".codex/config.toml")
        self.assertEqual(issue_parser.declared_paths(named),
                         [".claude/skills/backlog/", ".agents/skills/backlog/SKILL.md", ".codex/config.toml"])
        st = self.plan(named)
        self.assertEqual(self.granted(st), {".claude/skills/backlog": True, ".agents/skills/backlog/SKILL.md": True,
                                            ".codex/config.toml": True})
        self.assertIn("['.claude/settings.json']",
                      "\n".join(problems([".claude/skills/backlog/SKILL.md", ".claude/settings.json"], st)),
                      "what is named, and not the settings beside it")

    def test_a_repair_run_never_gets_the_agents_configuration(self):
        st = self.plan(body(".claude/settings.json", "docs/a.md"), repair=True)
        self.assertEqual(self.granted(st), {})
        self.assertIn("a fault-repair run may not change the safety surface",
                      "\n".join(problems([".claude/settings.json"], st, repair=True)))

    def test_the_pipeline_stack_and_the_bootstrap_policies_are_protected(self):
        # ADR 0001, amendment 1: a merge to `main` is deployment rights in the account
        self.judge_by_this_trees_lists()
        for path in DEPLOYMENT:
            self.assertTrue(is_protected(path), path)
        for path in ["src/rhi/stage.py", "src/rhi/pipeline.py", "setup/README.md", "setup/bootstrap.sh",
                     "docs/src/rhi/pipeline/notes.md"]:
            self.assertFalse(is_protected(path), f"{path}: the two folders, at the root, and nothing beside them")

    def test_they_are_granted_only_when_the_issue_names_them(self):
        self.judge_by_this_trees_lists()
        st = self.plan(body("src/rhi/stage.py"))
        self.assertEqual(self.granted(st), {})
        self.assertIn(f"protected paths changed without approval: {DEPLOYMENT}", "\n".join(problems(DEPLOYMENT, st)))
        st = self.plan(body("src/rhi/pipeline/", "setup/bootstrap/policies.yaml"))
        self.assertEqual(self.granted(st), {"setup/bootstrap/policies.yaml": True, "src/rhi/pipeline": True})
        self.assertEqual(problems(DEPLOYMENT, st), [])
        self.assertIn("['setup/bootstrap/README.md']", "\n".join(problems(["setup/bootstrap/README.md"], st)),
                      "the folder the issue names, the file it names, and not the file beside that one")

    def test_neither_folder_is_on_the_safety_surface(self):
        self.judge_by_this_trees_lists()
        for path in DEPLOYMENT:
            self.assertFalse(is_safety_surface(path), path)
        st = self.plan(body("src/rhi/pipeline/", "setup/bootstrap/", "scripts/check.sh"), repair=True)
        self.assertEqual(self.granted(st), {"setup/bootstrap": True, "src/rhi/pipeline": True},
                         "a repair run whose issue names them gets them, and not the gate script")
        self.assertEqual(problems(DEPLOYMENT, st, repair=True), [])

    def test_a_path_named_outside_the_deliverables_is_not_a_grant(self):
        st = self.plan(body("docs/a.md"))
        self.assertEqual(self.granted(st), {},
                         "the acceptance criteria mention scripts/check.sh as a command, not as a thing to change")

    def test_a_repair_run_is_never_granted_the_safety_surface_even_when_the_issue_names_it(self):
        st = self.plan(body("scripts/check.sh", "tools/implementation-loop/implement_loop/engine.py"), repair=True)
        self.assertEqual(self.granted(st), {"tools/implementation-loop/implement_loop/engine.py": True})

    def test_a_replan_keeps_what_was_granted_before(self):
        earlier = [{"path": "scripts/check.sh", "source": "approved R7-1", "protected": True},
                   {"path": "docs/a.md", "source": "plan", "protected": False}]   # an earlier engine's zone
        st = self.plan(body("docs/a.md", ".githooks/pre-push"), grants=earlier)
        self.assertEqual(st.grants, [*earlier, {"path": ".githooks/pre-push", "source": "issue", "protected": True}])


class EndToEndTests(EngineCase):
    def test_a_builder_cannot_reach_a_gate_the_issue_does_not_name(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S")
        self.scripts[2].files[".github/workflows/extra.yml"] = "on: push"   # the builder writes it, the issue does not name it
        self.epic(2)
        self.run_engine()
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
        self.assertNotIn(".github/workflows/extra.yml", {g["path"] for g in st.grants})
        found = "\n".join(p for e in self.events("verified", 2) for p in e["problems"])
        self.assertIn("protected paths changed without approval: ['.github/workflows/extra.yml']", found)
        self.assertFalse([c for c in self.agents.calls if c[0] == "examine"])
        self.assertEqual(self.gh.prs, {})
        self.assertIsNone(self.repo.main_file(".github/workflows/extra.yml"))

    def test_a_builder_cannot_reach_the_agents_configuration_either(self):
        hooks = {2: ".claude/settings.json", 3: ".agents/skills/extra/SKILL.md", 4: ".codex/config.toml"}
        for n, path in hooks.items():
            self.add_work(n, f"docs/{n}.txt", "alpha", size="S")
            self.scripts[n].files[path] = "a hook for the owner's next session"   # not in the issue
        self.epic(*hooks)
        self.run_engine()
        for n, path in hooks.items():
            with self.subTest(path=path):
                st = self.store.issue(n)
                self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
                self.assertNotIn(path, {g["path"] for g in st.grants})
                found = "\n".join(p for e in self.events("verified", n) for p in e["problems"])
                self.assertIn(f"protected paths changed without approval: ['{path}']", found)
                self.assertIsNone(self.repo.main_file(path))
        self.assertFalse([c for c in self.agents.calls if c[0] == "examine"])
        self.assertEqual(self.gh.prs, {})

    def test_a_typed_request_is_the_way_into_the_agents_configuration(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S", blocked_once=True, extra_path=".claude/registry.txt")
        self.epic(2)
        summary = self.run_engine()
        self.assertEqual(summary["phases"], {"done": 1})
        st = self.store.issue(2)
        [request] = [r for r in st.requests if r.get("paths") == [".claude/registry.txt"]]
        self.assertEqual(request["status"], "approved")
        self.assertIn({"path": ".claude/registry.txt", "source": f"approved {request['id']}", "protected": True}, st.grants)
        self.assertTrue([c for c in self.agents.calls if c[0] == "decide"], "the examiner decided it")

    def test_an_issue_that_names_the_protected_path_delivers_it(self):
        self.add_work(2, ".github/extra.txt", "alpha", size="S")
        self.epic(2)
        summary = self.run_engine()
        self.assertEqual(summary["phases"], {"done": 1})
        st = self.store.issue(2)
        self.assertIn({"path": ".github/extra.txt", "source": "issue", "protected": True}, st.grants)
        [decision] = [d for d in st.decisions if d["id"] == "protected-paths"]
        self.assertIn("protected paths the issue names are granted: .github/extra.txt", decision["choice"])
        self.assertEqual(self.repo.main_file(".github/extra.txt"), "alpha")
        pr = self.gh.prs[present(st.pr)]
        self.assertIn("protected paths the issue names are granted", pr["body"], "published with the pull request")

    def test_a_typed_request_the_examiner_approves_is_the_other_way_in(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S", blocked_once=True, extra_path=".github/registry.txt")
        self.epic(2)
        summary = self.run_engine()
        self.assertEqual(summary["phases"], {"done": 1})
        st = self.store.issue(2)
        [request] = [r for r in st.requests if r.get("paths") == [".github/registry.txt"]]
        self.assertEqual(request["status"], "approved")
        self.assertIn({"path": ".github/registry.txt", "source": f"approved {request['id']}", "protected": True}, st.grants)
        self.assertTrue([c for c in self.agents.calls if c[0] == "decide"], "the examiner decided it")


if __name__ == "__main__":
    unittest.main()
