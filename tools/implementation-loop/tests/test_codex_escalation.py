"""Escalated Codex calls (ADR 0001, amendment 12): deciding a `protected_change` request, any
arbitration, and examining a change to a protected path run on the role's `codex_escalated_model`,
and on its `codex_fallback_model` while that model refuses calls. Driven against a fake `codex`
that refuses one model the way `codex exec --json` reports it (an `error` and a `turn.failed`
event, exit 1, no answer file). No tokens spent."""

import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from helpers import git, identify, load_fixture
from implement_loop import live
from implement_loop.agents import IssueRequest
from implement_loop.issue_parser import parse
from implement_loop.model import Issue

FAKE = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
if argv[:2] == ["mcp", "list"]:
    print("[]")
    sys.exit(0)
model = argv[argv.index("-m") + 1] if "-m" in argv else ""
sys.stdin.read()
with open(os.environ["ESCALATION_LOG"], "a") as f:
    f.write(json.dumps({{"model": model}}) + "\n")
if (model or "DEFAULT") in os.environ.get("ESCALATION_REFUSED", "").split(","):
    message = os.environ.get("ESCALATION_MESSAGE", "")
    print(json.dumps({{"type": "thread.started", "thread_id": "t"}}))
    print(json.dumps({{"type": "turn.started"}}))
    print(json.dumps({{"type": "error", "message": message}}))
    print(json.dumps({{"type": "turn.failed", "error": {{"message": message}}}}))
    sys.exit(1)
open(argv[argv.index("-o") + 1], "w").write(os.environ["ESCALATION_ANSWER"])
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 12, "output_tokens": 3}}}}))
'''

USAGE_LIMIT = "You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM."
ANSWERS = {
    "arbitrate": {"upheld": False, "reason": "the item does not ask for it"},
    "decide": {"decisions": []},
    "examine": {"verdicts": [], "findings": [], "probes": [], "certify": True, "summary": "fine"},
    "triage": {"objections": []},
}


class StubWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def coord_path(self):
        return self.root

    @contextmanager
    def disposable(self, worktree):
        yield worktree


class Meter:
    def __init__(self):
        self.outcomes = []

    def before(self, issue, role, vendor):
        pass

    def after(self, issue, role, vendor, outcome):
        self.outcomes.append(outcome)


class CodexEscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        codex = self.tmp / "codex"
        codex.write_text(FAKE.format(python=sys.executable))
        codex.chmod(codex.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "models.jsonl"
        self.env = {"ESCALATION_LOG": str(self.log), "ESCALATION_REFUSED": "", "ESCALATION_MESSAGE": USAGE_LIMIT}
        os.environ.update(self.env)
        self.now = 1000.0
        self.sleeps = []
        self.agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), roles=live.load_roles(),
                                      claude_bin=str(codex), codex_bin=str(codex), sleep=self.sleeps.append,
                                      clock=lambda: self.now)
        self.agents.meter = self.meter = Meter()
        issue = Issue.from_api(load_fixture()["issues"]["28"])
        self.req = IssueRequest(issue=issue, spec=parse(issue), context="", facts={"invariants": ["secret exposure"]})
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        identify(self.repo)
        self.base = self.commit({"docs/notes.md": "notes\n", "tools/hygiene/hygiene.toml": "rules = []\n"})

    def tearDown(self):
        for key in (*self.env, "ESCALATION_ANSWER"):
            os.environ.pop(key, None)

    # ---------------------------------------------------------------- helpers
    def commit(self, files: dict[str, str | None]) -> str:
        for rel, text in files.items():
            path = self.repo / rel
            if text is None:
                git("rm", "-q", rel, cwd=self.repo)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git("add", rel, cwd=self.repo)
        git("commit", "-q", "-m", "change", cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def answer(self, role):
        os.environ["ESCALATION_ANSWER"] = json.dumps(ANSWERS[role])

    def models(self):
        return [json.loads(line)["model"] for line in self.log.read_text().splitlines()]

    def records(self):
        return [json.loads(line) for line in (self.tmp / "run" / "agents.jsonl").read_text().splitlines()]

    def arbitrate(self):
        self.answer("arbitrate")
        return self.agents.arbitrate("codex", self.req, {"kind": "finding", "argument": "not asked"})

    def decide(self, *kinds):
        self.answer("decide")
        return self.agents.decide("codex", self.req, self.tmp,
                                  [{"id": f"R28-{i}", "kind": k, "paths": []} for i, k in enumerate(kinds, 1)])

    def examine(self, head, since=None):
        self.answer("examine")
        return self.agents.examine("codex", self.req, {"items": []}, self.repo, self.base, head, [], [], [], since)

    # ---------------------------------------------------------------- which calls escalate
    def test_the_checked_in_settings_escalate_to_astra_and_fall_back_to_sol(self):
        roles = live.load_roles()
        for role in ("examine", "decide", "arbitrate"):
            self.assertEqual((roles[role].codex_escalated_model, roles[role].codex_fallback_model),
                             ("gpt-6.2-astra", "gpt-6.2-sol"), role)
            self.assertIsNone(roles[role].codex_model, f"{role}: every other call stays on the default model")
        for role in ("explore", "plan", "triage", "build", "resolve_locked"):
            self.assertIsNone(roles[role].codex_escalated_model, role)
            self.assertIsNone(roles[role].codex_fallback_model, role)

    def test_every_arbitration_is_escalated(self):
        self.assertEqual(self.arbitrate().reason, "the item does not ask for it")
        self.assertEqual(self.models(), ["gpt-6.2-astra"])
        self.assertTrue(self.records()[-1]["escalated"])

    def test_a_decision_is_escalated_only_when_a_protected_change_is_among_the_requests(self):
        self.decide("paths", "reclassify")
        self.decide("paths", "protected_change")
        self.assertEqual(self.models(), ["", "gpt-6.2-astra"])
        self.assertNotIn("escalated", self.records()[0])

    def test_an_examination_is_escalated_when_the_change_touches_a_protected_path(self):
        docs = self.commit({"docs/notes.md": "more notes\n"})
        self.examine(docs)
        guarded = self.commit({"tools/hygiene/hygiene.toml": "rules = ['x']\n"})
        self.examine(guarded, since=docs)   # a follow-up examination: the whole change is what counts
        self.assertEqual(self.models(), ["", "gpt-6.2-astra"])

    def test_moving_a_protected_file_out_is_escalated(self):
        git("mv", "tools/hygiene/hygiene.toml", "docs/hygiene.toml", cwd=self.repo)
        git("commit", "-q", "-m", "move", cwd=self.repo)
        self.examine(git("rev-parse", "HEAD", cwd=self.repo))
        self.assertEqual(self.models(), ["gpt-6.2-astra"])

    def test_removing_a_protected_file_is_escalated(self):
        self.examine(self.commit({"tools/hygiene/hygiene.toml": None}))
        self.assertEqual(self.models(), ["gpt-6.2-astra"])

    def test_a_change_whose_paths_cannot_be_listed_is_escalated(self):
        self.examine("f" * 40)
        self.assertEqual(self.models(), ["gpt-6.2-astra"])

    def test_other_codex_calls_stay_on_the_default_model(self):
        self.answer("triage")
        self.agents.triage("codex", self.req, {"items": []})
        self.assertEqual(self.models(), [""])

    # ---------------------------------------------------------------- refusals and the fallback
    def test_a_refused_escalated_model_is_replaced_by_the_fallback_at_once_and_charged_nothing(self):
        os.environ["ESCALATION_REFUSED"] = "gpt-6.2-astra"
        self.assertEqual(self.arbitrate().reason, "the item does not ask for it")
        self.assertEqual(self.models(), ["gpt-6.2-astra", "gpt-6.2-sol"])
        self.assertEqual(self.meter.outcomes, ["outage", "answered"], "the refused attempt is charged to nothing")
        self.assertEqual(self.sleeps, [], "the fallback is asked at once")
        refused, answered = self.records()
        self.assertEqual((refused["model"], refused["outcome"], refused["refused"]), ("gpt-6.2-astra", "outage", True))
        self.assertIn("usage limit", refused["errors"][0])
        self.assertEqual((answered["model"], answered["outcome"], answered["ok"]), ("gpt-6.2-sol", "answered", True))

    def test_the_role_keeps_the_fallback_for_thirty_minutes_then_asks_astra_again(self):
        os.environ["ESCALATION_REFUSED"] = "gpt-6.2-astra"
        self.arbitrate()
        self.now += live.REFUSED_RETRY_S - 1
        self.arbitrate()
        self.assertEqual(self.models(), ["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.2-sol"])
        self.now += 2
        os.environ["ESCALATION_REFUSED"] = ""
        self.arbitrate()
        self.assertEqual(self.models()[-1], "gpt-6.2-astra")

    def test_a_refused_model_is_passed_over_by_every_role_that_asks_for_it(self):
        os.environ["ESCALATION_REFUSED"] = "gpt-6.2-astra"
        self.arbitrate()
        os.environ["ESCALATION_REFUSED"] = ""
        self.decide("protected_change")
        self.assertEqual(self.models(), ["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.2-sol"],
                         "astra refused the account, whichever role asked")

    def test_with_both_models_refusing_the_call_is_an_outage_with_backoff(self):
        os.environ["ESCALATION_REFUSED"] = "gpt-6.2-astra,gpt-6.2-sol"
        with self.assertRaises(live.AgentFailure) as caught:
            self.arbitrate()
        self.assertEqual((caught.exception.kind, caught.exception.vendor), ("outage", "codex"))
        self.assertEqual(self.models(), ["gpt-6.2-astra"] + ["gpt-6.2-sol"] * 3)
        self.assertEqual(set(self.meter.outcomes), {"outage"})
        self.assertEqual(self.sleeps, [30, 90])

    def test_a_default_model_that_refuses_is_an_outage_not_a_crash(self):
        os.environ["ESCALATION_REFUSED"] = "DEFAULT"   # the fake's name for a call with no `-m`
        with self.assertRaises(live.AgentFailure) as caught:
            self.decide("paths")
        self.assertEqual(caught.exception.kind, "outage")
        self.assertEqual(self.models(), [""] * 3, "no fallback for a call on the default model")

    def test_a_crash_is_no_refusal(self):
        os.environ["ESCALATION_REFUSED"] = "gpt-6.2-astra"
        os.environ["ESCALATION_MESSAGE"] = "unexpected argument '--frobnicate' found"
        with self.assertRaises(live.AgentFailure) as caught:
            self.arbitrate()
        self.assertEqual(caught.exception.kind, "crash")
        self.assertEqual(self.models(), ["gpt-6.2-astra"] * 3, "no fallback for a call that was not refused")

    def test_what_counts_as_a_codex_refusal(self):
        for text in (USAGE_LIMIT, "exceeded retry limit, last status: 429 Too Many Requests",
                     "Rate limit reached for gpt-6.2-astra", "insufficient_quota: You exceeded your current quota",
                     "The 'gpt-6.2-astra' model is not supported when using Codex with a ChatGPT account.",
                     "model_not_found: The model `gpt-6.2-astra` does not exist or you do not have access to it."):
            with self.subTest(text=text):
                self.assertTrue(live.codex_refused([text], ""))
                self.assertTrue(live.codex_refused([], text))
        for text in ("unexpected argument '--frobnicate' found", "Error: stream disconnected before completion",
                     "failed to parse the output schema", ""):
            with self.subTest(text=text):
                self.assertFalse(live.codex_refused([text], text))


class ParseTests(unittest.TestCase):
    def test_the_error_messages_of_a_codex_stream_are_kept(self):
        out = Path(tempfile.mkdtemp()) / "missing.json"
        stream = "\n".join([json.dumps({"type": "turn.started"}), "not json", json.dumps([1, 2]),
                            json.dumps({"type": "error", "message": "first"}),
                            json.dumps({"type": "turn.failed", "error": {"message": "second"}}),
                            json.dumps({"type": "turn.failed", "error": None})])
        value, meta = live.LiveAgents._parse("codex", stream, out)
        self.assertIsNone(value)
        self.assertEqual(meta["errors"], ["first", "second", ""])


if __name__ == "__main__":
    unittest.main()
