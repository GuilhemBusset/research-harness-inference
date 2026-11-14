"""Protected paths (ADR 0001, decision 10): a builder changes one only when the issue names it
or the examiner approves a typed request.

Upstream granted every protected path a plan listed among its zones, on the grounds that triage
saw the plan. Triage has no objection kind for a zone, so nothing was ever approved: a builder
could reach the gates by planning to. A plan's protected zone is granted now only when the
issue's own text names it: that path, or a protected directory above it. An issue that names a
wider directory (`scripts/`, `tools/`) has not named the gates inside it."""

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import present
from implement_loop import gate, issue_parser
from implement_loop.agents import ItemPlan, PlanResult
from implement_loop.config import Caps, is_protected, is_safety_surface
from implement_loop.engine import Engine
from implement_loop.model import IssueState, Phase
from implement_loop.state import RunStore
from test_engine import EngineCase

TEXT = {"D1": "the gate script gains a step", "A1": "the new step passes on the change"}
ZONES = [".github/workflows/ci.yml", "tools/hygiene", "tools/hygiene/scan.py", "scripts/check.sh", "tests/policy/",
         "tools", "docs/a.md", "infra/stack.py"]


def body(*named: str) -> str:
    lines = ["## Deliverables", *[f"- [ ] `{path}` is changed as described." for path in named],
             "", "## Acceptance criteria", "- [ ] `scripts/check.sh fast` passes; see `docs/factory/operating.md`."]
    return "\n".join(lines) + "\n"


class Agents:
    def __init__(self, zones):
        self.zones = zones

    def plan(self, vendor, req, wt):
        items = [ItemPlan(id=k, reading="r", quote=v, proof="review") for k, v in TEXT.items()]
        return PlanResult(items=items, zones=list(self.zones), decisions=[])

    def triage(self, vendor, req, plan):
        return []   # the examiner raises nothing: it has no objection kind for a zone


class PlanGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-zones-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plan(self, issue_body: str, zones=ZONES, repair=False, grants=()) -> IssueState:
        engine = Engine.__new__(Engine)
        engine.store = RunStore(self.tmp / "run")
        engine.caps, engine.repair, engine.other, engine.op = Caps(), repair, "codex", "claude"
        engine.lacks, engine._lookup_issue = [], lambda n: None   # ty: ignore[invalid-assignment]  # test double
        ledger = [SimpleNamespace(id=k, text=v) for k, v in TEXT.items()]
        engine.plan = SimpleNamespace(specs={7: SimpleNamespace(ledger=ledger, declared_paths=[])},  # ty: ignore[invalid-assignment]  # test double
                                      issues={7: SimpleNamespace(body=issue_body)}, work=[7])
        engine.agents = Agents(zones)  # ty: ignore[invalid-assignment]  # test double
        engine.store.put(IssueState(number=7, phase=Phase.PLAN, grants=list(grants)))
        with mock.patch.object(Engine, "_req"), mock.patch.object(Engine, "_set_phase"), \
                mock.patch("implement_loop.engine.scheduler.prerequisites_met", return_value=True):
            engine._step_plan(7)
        self.events = engine.store.events()
        return engine.store.issue(7)

    @staticmethod
    def granted(st: IssueState) -> dict[str, bool]:
        return {g["path"]: g["protected"] for g in st.grants}

    def test_a_protected_zone_the_issue_does_not_name_is_withheld(self):
        st = self.plan(body("infra/stack.py", "docs/a.md"))
        self.assertEqual(self.granted(st), {"docs/a.md": False, "infra/stack.py": False, "tools": False},
                         "the examiner's silence at triage approves nothing")
        withheld = [e for e in self.events if e["kind"] == "protected_zones_withheld"]
        self.assertEqual(withheld[0]["zones"], [".github/workflows/ci.yml", "scripts/check.sh", "tests/policy",
                                                "tools/hygiene", "tools/hygiene/scan.py"])
        [decision] = [d for d in st.decisions if d["id"] == "protected-zones"]
        self.assertIn("not granted, because the issue does not name them: .github/workflows/ci.yml", decision["choice"])
        self.assertNotIn("granted, because the issue names them", decision["choice"])
        [told] = [f for f in st.feedback if "protected paths the issue does not name" in f]
        self.assertIn("`protected_change` request", told)
        self.assertIn("scripts/check.sh", told)

    def test_a_protected_zone_the_issue_names_is_granted_as_protected(self):
        st = self.plan(body("scripts/check.sh", "tools/hygiene/", "docs/a.md"))
        granted = self.granted(st)
        self.assertEqual({p for p, protected in granted.items() if protected},
                         {"scripts/check.sh", "tools/hygiene", "tools/hygiene/scan.py"},
                         "the file by its name, and what lies under a directory the issue names")
        self.assertNotIn(".github/workflows/ci.yml", granted)
        self.assertNotIn("tests/policy", granted)
        [decision] = [d for d in st.decisions if d["id"] == "protected-zones"]
        self.assertIn("granted, because the issue names them: scripts/check.sh, tools/hygiene, tools/hygiene/scan.py",
                      decision["choice"])
        self.assertIn("not granted, because the issue does not name them: .github/workflows/ci.yml, tests/policy",
                      decision["choice"])

    def test_naming_one_file_does_not_grant_the_directory_around_it(self):
        st = self.plan(body("tools/hygiene/scan.py"))
        self.assertEqual({p for p, protected in self.granted(st).items() if protected}, {"tools/hygiene/scan.py"})

    def test_naming_a_wider_directory_does_not_name_the_protected_paths_inside_it(self):
        st = self.plan(body("scripts/", "tools/", ".github-notes/a.md"),
                       zones=["scripts/check.sh", "scripts/new.sh", "tools/hygiene", "tools/hygiene/scan.py", "tools/backlog/x.py"])
        self.assertEqual(self.granted(st), {"scripts/new.sh": False, "tools/backlog/x.py": False},
                         "`scripts/` is not `scripts/check.sh`, and `tools/` is not `tools/hygiene/`")
        withheld = [e for e in self.events if e["kind"] == "protected_zones_withheld"]
        self.assertEqual(withheld[0]["zones"], ["scripts/check.sh", "tools/hygiene", "tools/hygiene/scan.py"])
        problems = gate.zone_problems(["scripts/check.sh", "scripts/new.sh"], st.grants, [], False)
        self.assertIn("protected paths changed without approval: ['scripts/check.sh']", "\n".join(problems))

    def test_a_directory_inside_a_protected_one_names_what_lies_under_it(self):
        st = self.plan(body(".github/workflows/"), zones=[".github/workflows/ci.yml", ".github/dependabot.yml"])
        self.assertEqual(self.granted(st), {".github/workflows/ci.yml": True})

    def test_the_agents_own_configuration_is_protected_like_the_gates(self):
        # ADR 0001, amendment 6: a hook or a setting added there runs in the owner's own session
        config = [".claude/settings.json", ".claude/skills/implement/SKILL.md", ".agents/skills/ship/SKILL.md",
                  ".codex/config.toml"]
        for path in config:
            self.assertTrue(is_protected(path), path)
            self.assertTrue(is_safety_surface(path), path)
        self.assertFalse(is_protected(".claudeignore") or is_protected("docs/.claude/notes.md"), "the directories, at the root")
        st = self.plan(body("docs/a.md"), zones=["docs/a.md", *config])
        self.assertEqual(self.granted(st), {"docs/a.md": False}, "a plan cannot reach them by listing them")
        problems = gate.zone_problems([".claude/settings.json", "docs/a.md"], st.grants, [], False)
        self.assertIn("protected paths changed without approval: ['.claude/settings.json']", "\n".join(problems))

    def test_an_issue_can_name_a_path_in_each_of_those_directories(self):
        named = body(".claude/skills/backlog/", ".agents/skills/backlog/SKILL.md", ".codex/config.toml")
        self.assertEqual(issue_parser.declared_paths(named),
                         [".claude/skills/backlog/", ".agents/skills/backlog/SKILL.md", ".codex/config.toml"])
        st = self.plan(named, zones=[".claude/skills/backlog/SKILL.md", ".agents/skills/backlog/SKILL.md", ".codex/config.toml",
                                     ".claude/settings.json"])
        self.assertEqual(self.granted(st), {".claude/skills/backlog/SKILL.md": True, ".agents/skills/backlog/SKILL.md": True,
                                            ".codex/config.toml": True}, "what is named, and not the settings beside it")

    def test_a_repair_run_never_gets_the_agents_configuration(self):
        st = self.plan(body(".claude/settings.json", "docs/a.md"), zones=[".claude/settings.json", "docs/a.md"], repair=True)
        self.assertEqual(self.granted(st), {"docs/a.md": False})
        problems = gate.zone_problems([".claude/settings.json"], [{"path": ".claude/settings.json", "protected": True}], [], True)
        self.assertIn("a fault-repair run may not change the safety surface", "\n".join(problems))

    def test_a_path_named_outside_the_deliverables_is_not_a_grant(self):
        st = self.plan(body("docs/a.md"), zones=["docs/factory/operating.md", "scripts/check.sh"])
        self.assertEqual(self.granted(st), {"docs/factory/operating.md": False},
                         "the acceptance criteria mention scripts/check.sh as a command, not as a thing to change")

    def test_what_the_gate_then_lets_a_builder_change(self):
        st = self.plan(body("tools/hygiene/"))
        changed = ["tools/hygiene/scan.py", "tools/hygiene/hygiene.toml", "tools/backlog/x.py", "docs/a.md",
                   ".github/workflows/ci.yml", "scripts/check.sh"]
        problems = gate.zone_problems(changed, st.grants, [], False)
        self.assertEqual(len(problems), 2)
        self.assertIn("outside the plan's zones and approved paths: ['.github/workflows/ci.yml', 'scripts/check.sh']",
                      problems[0])
        self.assertIn("protected paths changed without approval: ['.github/workflows/ci.yml', 'scripts/check.sh']",
                      problems[1])

    def test_a_wide_zone_never_covers_the_protected_paths_inside_it(self):
        st = self.plan(body("tools/"), zones=["tools", "scripts"])
        self.assertEqual(self.granted(st), {"scripts": False, "tools": False})
        problems = gate.zone_problems(["tools/backlog/x.py", "tools/hygiene/scan.py", "scripts/check.sh"], st.grants, [], False)
        self.assertEqual(problems, [("protected paths changed without approval: ['tools/hygiene/scan.py', 'scripts/check.sh'] "
                                     "(ask with a `protected_change` request)")])

    def test_a_repair_run_is_never_granted_the_safety_surface_even_when_the_issue_names_it(self):
        st = self.plan(body("scripts/check.sh", "tools/implementation-loop/implement_loop/engine.py"),
                       zones=["scripts/check.sh", "tools/implementation-loop/implement_loop/engine.py"], repair=True)
        self.assertEqual(self.granted(st), {"tools/implementation-loop/implement_loop/engine.py": True})

    def test_a_replan_keeps_what_an_earlier_plan_or_request_was_granted(self):
        earlier = [{"path": "scripts/check.sh", "source": "approved R7-1", "protected": True},
                   {"path": "docs/a.md", "source": "plan", "protected": False}]
        st = self.plan(body("docs/a.md"), zones=["docs/a.md", "docs/b.md", ".githooks/pre-push"], grants=earlier)
        self.assertEqual(st.grants, [{"path": "scripts/check.sh", "source": "approved R7-1", "protected": True},
                                     {"path": "docs/a.md", "source": "earlier plan", "protected": False},
                                     {"path": "docs/b.md", "source": "plan", "protected": False}])

    def test_a_plan_without_protected_zones_publishes_no_decision(self):
        st = self.plan(body("docs/a.md"), zones=["docs/a.md"])
        self.assertFalse([d for d in st.decisions if d["id"] == "protected-zones"])
        self.assertFalse([f for f in st.feedback if "protected" in f])


class EndToEndTests(EngineCase):
    def test_a_builder_cannot_reach_a_gate_by_listing_it_in_its_plan(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S")
        self.scripts[2].files[".github/workflows/extra.yml"] = "on: push"   # the plan lists it, the issue does not
        self.epic(2)
        self.run_engine()
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
        self.assertNotIn(".github/workflows/extra.yml", {g["path"] for g in st.grants})
        self.assertTrue(self.events("protected_zones_withheld", 2))
        problems = "\n".join(p for e in self.events("verified", 2) for p in e["problems"])
        self.assertIn("protected paths changed without approval: ['.github/workflows/extra.yml']", problems)
        first_build = [c for c in self.agents.calls if c[0] == "build"][0]
        self.assertTrue(any("protected paths the issue does not name" in f for f in first_build[3]),
                        "the builder was told before it wrote anything")
        self.assertFalse([c for c in self.agents.calls if c[0] == "examine"])
        self.assertEqual(self.gh.prs, {})
        self.assertIsNone(self.repo.main_file(".github/workflows/extra.yml"))

    def test_a_builder_cannot_reach_the_agents_configuration_by_listing_it_either(self):
        hooks = {2: ".claude/settings.json", 3: ".agents/skills/extra/SKILL.md", 4: ".codex/config.toml"}
        for n, path in hooks.items():
            self.add_work(n, f"docs/{n}.txt", "alpha", size="S")
            self.scripts[n].files[path] = "a hook for the owner's next session"   # in the plan, not in the issue
        self.epic(*hooks)
        self.run_engine()
        for n, path in hooks.items():
            with self.subTest(path=path):
                st = self.store.issue(n)
                self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "no_progress"))
                self.assertNotIn(path, {g["path"] for g in st.grants})
                self.assertEqual(self.events("protected_zones_withheld", n)[0]["zones"], [path])
                problems = "\n".join(p for e in self.events("verified", n) for p in e["problems"])
                self.assertIn(f"protected paths changed without approval: ['{path}']", problems)
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
        self.assertIn({"path": ".github/extra.txt", "source": "plan", "protected": True}, st.grants)
        [decision] = [d for d in st.decisions if d["id"] == "protected-zones"]
        self.assertIn("granted, because the issue names them: .github/extra.txt", decision["choice"])
        self.assertEqual(self.repo.main_file(".github/extra.txt"), "alpha")
        pr = self.gh.prs[present(st.pr)]
        self.assertIn("protected paths among the plan's zones", pr["body"], "published with the pull request")

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
