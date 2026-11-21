"""#92's path, replayed: a `build` call on Claude refused for its usage limit, attempt after attempt.

Claude Code 2.1 answers such a call with exit 1, an empty stderr and the reason only in its JSON
result (429, `rate_limit_error`, `usage_limit_reached`), in under a second and for nothing. Engine
1.2.4 read only stderr, charged every refusal to the infrastructure budget as a crash, and failed
#92 with `ceiling` after ten of them. Here the live adapter, with the checked-in `build` settings,
is metered by the engine's own `before`/`after` and its failure handled by `_finished`: a refusal
is never charged, and a refused issue waits for the vendor instead of failing."""

import os
import stat
import sys
import tempfile
from pathlib import Path

from helpers import SHIP, present
from implement_loop import live
from implement_loop.agents import FakeAgents
from implement_loop.config import Caps, Config
from implement_loop.engine import Engine
from implement_loop.graph import build_plan
from implement_loop.model import Phase
from implement_loop.state import RunStore
from implement_loop.workspace import Workspace
from test_engine import EPIC, EngineCase

# The shape #92's call records hold, scrubbed: no session, no request id, no link.
FAKE = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
sys.stdin.read()
with open(os.environ["REFUSED_BUILD_LOG"], "a") as f:
    f.write(model + "\n")
if model in os.environ["REFUSED_BUILD_MODELS"].split(","):
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "duration_ms": 400,
                       "total_cost_usd": 0, "api_error_status": 429, "api_error_type": "rate_limit_error",
                       "api_error": "usage_limit_reached", "terminal_reason": "api_error",
                       "result": "You have hit your limit"}}))
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": False, "total_cost_usd": 0.01,
                   "structured_output": {answer}}}))
'''

BUILT = {"status": "done", "note": "built", "tests": [], "commands": [], "decisions": [], "requests": []}


class StubWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def coord_path(self):
        return self.root


class RefusedBuildTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        caps = Caps(ci_poll_s=0, queue_poll_s=0)
        self.engine = Engine(build_plan(self.gh, EPIC, self.repo.work), RunStore.for_root(self.repo.work, EPIC),
                             self.gh, Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir),
                             FakeAgents(self.scripts), Config(repo="t/t", caps=caps), repo_root=self.repo.work,
                             log=lambda m: None, sleep=lambda s: None, policy=self.policy)
        self.engine._set_phase(2, Phase.BUILD)
        self.tmp = Path(tempfile.mkdtemp())
        claude = self.tmp / "claude"
        claude.write_text(FAKE.format(python=sys.executable, answer=repr(BUILT)))
        claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "models.txt"
        os.environ["REFUSED_BUILD_LOG"] = str(self.log)
        roles = live.load_roles()
        self.settings = roles["build"]   # the checked-in settings: Fable, then Opus
        self.live = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), roles=roles,
                                    claude_bin=str(claude), codex_bin=str(claude), sleep=lambda s: None)
        self.live.meter = self.engine

    def tearDown(self):
        for key in ("REFUSED_BUILD_LOG", "REFUSED_BUILD_MODELS"):
            os.environ.pop(key, None)
        super().tearDown()

    def build(self):
        try:
            return self.live.call("claude", "build", self.tmp, "build #2", issue=2)
        except live.AgentFailure as e:
            self.engine._finished(2, e)
            return None

    def models(self):
        return self.log.read_text().split()

    def test_a_refused_fable_build_is_answered_on_opus_and_charged_nothing(self):
        build = self.settings
        os.environ["REFUSED_BUILD_MODELS"] = present(build.claude_model)
        self.assertEqual(self.build(), BUILT)
        self.assertEqual(self.models(), [build.claude_model, build.claude_fallback_model])
        b = self.engine.store.budget(2)
        self.assertEqual((b["infra"], b["calls"], b.get("pending", 0)), (0, 1, 0))

    def test_refusals_past_the_infrastructure_budget_leave_the_issue_waiting_not_failed(self):
        build = self.settings
        os.environ["REFUSED_BUILD_MODELS"] = f"{build.claude_model},{build.claude_fallback_model}"
        rounds = 0
        while not self.log.exists() or len(self.models()) <= self.engine.caps.infra_budget:
            self.engine._set_phase(2, Phase.BUILD)   # the wait is over: the issue resumes its build
            self.assertIsNone(self.build())
            rounds += 1
        self.assertGreater(len(self.models()), self.engine.caps.infra_budget)
        st = self.engine.store.issue(2)
        self.assertEqual(self.engine.store.budget(2)["infra"], 0, "a refusal is charged to no budget")
        self.assertIsNone(st.fail, "a refused vendor is not a ceiling")
        wait = present(st.wait)
        self.assertEqual((st.phase, wait["kind"], wait["resume"], wait.get("vendor")),
                         (Phase.WAITING, "outage", "build", "claude"))
        self.assertGreater(rounds, 1)
