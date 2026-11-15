"""A Claude model that refuses calls (its usage limit reached, or rate limited): the live adapter,
driven against a fake `claude` that refuses one model the way Claude Code 2.1 does (exit 1, an
empty stderr, the reason only in its JSON result). No tokens spent."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401 - puts the engine on sys.path
from implement_loop import live

FAKE = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
sys.stdin.read()
with open(os.environ["FALLBACK_LOG"], "a") as f:
    f.write(json.dumps({{"model": model}}) + "\n")
if model in os.environ.get("FALLBACK_REFUSED", "").split(","):
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "total_cost_usd": 0,
                       "api_error_status": 429, "api_error_type": "rate_limit_error",
                       "api_error": "usage_limit_reached", "terminal_reason": "api_error",
                       "result": "You've hit your limit"}}))
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": False, "structured_output": {{"notes": "from " + model}},
                   "total_cost_usd": 0.01, "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
'''


class StubWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def coord_path(self):
        return self.root


class Meter:
    def __init__(self):
        self.outcomes = []

    def before(self, issue, role, vendor):
        pass

    def after(self, issue, role, vendor, outcome):
        self.outcomes.append(outcome)


class ClaudeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        claude = self.tmp / "claude"
        claude.write_text(FAKE.format(python=sys.executable))
        claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "models.jsonl"
        os.environ["FALLBACK_LOG"] = str(self.log)
        os.environ["FALLBACK_REFUSED"] = "primary"
        self.now = 1000.0
        self.sleeps = []
        roles = live.load_roles()
        roles["plan"].claude_model, roles["plan"].claude_fallback_model = "primary", "backup"
        roles["plan"].max_budget_usd = None
        self.agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), roles=roles,
                                      claude_bin=str(claude), codex_bin=str(claude), sleep=self.sleeps.append,
                                      clock=lambda: self.now)
        self.agents.meter = self.meter = Meter()

    def tearDown(self):
        for key in ("FALLBACK_LOG", "FALLBACK_REFUSED"):
            os.environ.pop(key, None)

    def call(self):
        # `plan` is a read-only Claude role; the schema of `explore` keeps the fake's answer small.
        self.agents.roles["explore"] = self.agents.roles["plan"]
        return self.agents.call("claude", "explore", self.tmp, "a question", issue=None)

    def models(self):
        return [json.loads(line)["model"] for line in self.log.read_text().splitlines()]

    def records(self):
        return [json.loads(line) for line in (self.tmp / "run" / "agents.jsonl").read_text().splitlines()]

    def test_a_refused_model_is_replaced_by_the_fallback_at_once_and_charged_nothing(self):
        self.assertEqual(self.call(), {"notes": "from backup"})
        self.assertEqual(self.models(), ["primary", "backup"])
        self.assertEqual(self.meter.outcomes, ["outage", "answered"], "the refused attempt is charged to nothing")
        self.assertEqual(self.sleeps, [], "the fallback is asked at once")
        first, second = self.records()
        self.assertEqual((first["model"], first["outcome"], first["ok"], first["refused"]),
                         ("primary", "outage", False, True))
        self.assertEqual((second["model"], second["outcome"], second["ok"]), ("backup", "answered", True))

    def test_the_role_keeps_the_fallback_then_asks_its_own_model_again(self):
        self.call()
        self.now += live.REFUSED_RETRY_S - 1
        self.call()
        self.assertEqual(self.models(), ["primary", "backup", "backup"], "no refused call while the window lasts")
        self.now += 2
        os.environ["FALLBACK_REFUSED"] = ""
        self.assertEqual(self.call(), {"notes": "from primary"})
        self.assertEqual(self.models()[-1], "primary")

    def test_with_both_models_refusing_the_call_is_an_outage_with_backoff(self):
        os.environ["FALLBACK_REFUSED"] = "primary,backup"
        with self.assertRaises(live.AgentFailure) as caught:
            self.call()
        self.assertEqual((caught.exception.kind, caught.exception.vendor), ("outage", "claude"))
        self.assertEqual(self.models(), ["primary", "backup", "backup", "backup"])
        self.assertEqual(set(self.meter.outcomes), {"outage"}, "nothing is charged to the issue's budgets")
        self.assertEqual(self.sleeps, [30, 90])

    def test_without_a_fallback_a_refused_model_is_an_outage_not_a_crash(self):
        self.agents.roles["plan"].claude_fallback_model = None
        with self.assertRaises(live.AgentFailure) as caught:
            self.call()
        self.assertEqual(caught.exception.kind, "outage")
        self.assertEqual(self.models(), ["primary"] * 3)
        self.assertEqual(self.meter.outcomes, ["outage"] * 3)

    def test_what_counts_as_a_refusal(self):
        refused = {"is_error": True, "api_error": "usage_limit_reached", "api_error_status": 429}
        self.assertTrue(live.claude_refused(refused))
        self.assertTrue(live.claude_refused({"is_error": True, "api_error_type": "rate_limit_error"}))
        self.assertFalse(live.claude_refused(refused | {"is_error": False}))
        self.assertFalse(live.claude_refused({"is_error": True, "api_error_status": 500}))
        self.assertFalse(live.claude_refused({"is_error": False, "structured_output": {}}))

    def test_the_checked_in_settings_fall_back_from_fable_to_opus(self):
        roles = live.load_roles()
        for role in ("plan", "build"):
            self.assertEqual((roles[role].claude_model, roles[role].claude_fallback_model),
                             ("claude-fable-5-2", "claude-opus-5-5"), role)
        self.assertIsNone(roles["explore"].claude_fallback_model)


if __name__ == "__main__":
    unittest.main()
