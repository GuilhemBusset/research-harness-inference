"""The examiner bound (ADR 0001, amendment 12): from an issue's fourth examination since its latest
plan on, only a finding that names a safety invariant blocks. A finding that names only an item
becomes a follow-up issue, and a head whose items are all met lands without the examiner's
certificate. Fake agents, real git and the real verifier, as in test_engine.py."""

import unittest

from helpers import present
from implement_loop import live
from implement_loop.agents import FakeAgents
from implement_loop.config import Caps
from implement_loop.model import Phase
from test_engine import EngineCase


class Agents(FakeAgents):
    """FakeAgents that keep what each examination was told."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.told: list[list[str]] = []

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        self.told.append(list(req.feedback))
        return super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)


class ExaminerBoundTests(EngineCase):
    def run_bound(self, **script):
        self.add_work(2, "docs/a.txt", "alpha", **script)
        self.epic(2)
        self.examiner = Agents(self.scripts)
        return self.run_engine(agents=self.examiner)

    def examinations(self):
        return [e["examination"] for e in self.events("examined", 2)]

    def findings(self, status):
        return [f for f in self.store.issue(2).findings if f["status"] == status]

    def test_from_the_fourth_examination_a_finding_on_an_item_is_a_follow_up(self):
        # Every examination finds another corner case, and every round handles the one it was shown.
        summary = self.run_bound(new_findings=9)
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(self.examinations(), [1, 2, 3, 4])
        self.assertEqual(len([c for c in self.agents.calls if c[0] == "build"]), 4, "three rounds, then it lands")
        self.assertEqual([f["title"] for f in self.findings("closed")],
                         ["corner case 0 breaks the item", "corner case 1 breaks the item", "corner case 2 breaks the item"])
        [late] = self.findings("follow_up")
        self.assertEqual((late["title"], late["item"], late["blocking"]), ("corner case 3 breaks the item", "D1", False))
        self.assertIn("examiner bound", late["reason"])
        self.assertEqual(self.events("examined", 2)[-1]["bounded"], [late["id"]])
        self.assertIsNone(self.repo.main_file("corners_2/case_3.txt"), "the fourth corner case was not built")
        [followup] = [i for i in self.gh.issues.values() if {"name": "loop:follow-up"} in i["labels"]]
        self.assertIn("corner case 3 breaks the item** (on D1)", followup["body"])
        self.assertIn("from examination 4 on", followup["body"])

    def test_the_examiner_is_told_which_examination_it_is_and_what_blocks(self):
        self.run_bound(new_findings=9)
        notes = [told[0] for told in self.examiner.told]
        for k, note in enumerate(notes, 1):
            self.assertIn(f"This is examination {k} of #2 since its latest plan.", note)
        self.assertTrue(all("From examination 4 on, only a finding that names a safety invariant will block" in n
                            for n in notes[:3]))
        self.assertIn("a finding that names only an item becomes a follow-up issue", notes[3])
        self.assertIn("Certify this commit once every item an agent must prove is met", notes[3])

    def test_past_the_bound_a_finding_that_names_a_safety_invariant_still_blocks(self):
        summary = self.run_bound(new_findings=9, invariant_findings=5)
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(self.examinations(), [1, 2, 3, 4, 5, 6])
        guards = [f for f in self.store.issue(2).findings if f.get("invariant")]
        self.assertEqual([(f["status"], f["round"]) for f in guards], [("closed", r) for r in range(1, 6)],
                         "each one blocked until the builder handled it")
        self.assertEqual(present(self.repo.main_file("corners_2/guard_4.txt")), "handled")
        self.assertEqual(sorted(f["title"] for f in self.findings("follow_up")),
                         ["corner case 3 breaks the item", "corner case 4 breaks the item", "corner case 5 breaks the item"])

    def test_past_the_bound_a_withheld_certificate_does_not_hold_a_head_whose_items_are_met(self):
        summary = self.run_bound(new_findings=9, no_certify=99)
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(self.examinations(), [1, 2, 3, 4])
        [certified] = self.events("certified_by_bound", 2)
        self.assertEqual(certified["examination"], 4)
        decision = next(d for d in self.store.issue(2).decisions if d["id"] == "examiner-bound-4")
        self.assertIn("fake examination", decision["rationale"], "the examiner's summary is published with it")

    def test_an_unmet_item_still_withholds_the_certificate_and_a_replan_starts_the_count_again(self):
        # D1 is judged unmet three times: the same failure twice more brings a replan, after which
        # the examinations are counted from one again. No examination passed the bound.
        summary = self.run_bound(review_items=("D1",), unmet_review=3)
        self.assertEqual(summary["phases"], {"done": 1})
        self.assertEqual(len(self.events("replan", 2)), 1)
        self.assertEqual(self.examinations(), [1, 2, 3, 1])
        self.assertIn("This is examination 1 of #2", self.examiner.told[-1][0])

    def test_with_the_bound_already_passed_an_unmet_item_still_blocks(self):
        caps = Caps(ci_poll_s=0, queue_poll_s=0, examiner_bound=1)
        self.add_work(2, "docs/a.txt", "alpha", review_items=("D1",), unmet_review=1, no_certify=1)
        self.epic(2)
        self.examiner = Agents(self.scripts)
        summary = self.run_engine(agents=self.examiner, caps=caps)
        self.assertEqual(summary["phases"], {"done": 1})
        first = self.events("examined", 2)[0]
        self.assertEqual(first["unmet"], ["D1"])
        self.assertFalse(self.events("certified_by_bound", 2), "the unmet item withheld it, not the bound")
        self.assertEqual(len([c for c in self.agents.calls if c[0] == "build"]), 2)

    def test_a_state_from_an_earlier_engine_counts_its_examinations_from_the_journal(self):
        self.run_bound(new_findings=2)
        st = self.store.issue(2)
        self.assertEqual(st.examinations, 3)
        st.examinations = None
        self.store.put(st)
        self.assertEqual(self.engine._examinations(2, self.store.issue(2)), 3)
        self.store.event("planned", issue=2)
        self.store.event("examined", issue=2)
        self.store.event("examined", issue=3)
        self.assertEqual(self.engine._examinations(2, self.store.issue(2)), 1)
        self.assertEqual(self.store.issue(2).phase, Phase.DONE)


class ExaminePromptTests(unittest.TestCase):
    def test_the_examination_prompt_carries_what_the_engine_tells_the_examiner(self):
        text = live.render("examine", feedback="Feedback you must address:\n- This is examination 4 of #2")
        self.assertIn("- This is examination 4 of #2", text)
        self.assertIn("From the bound on, only a finding that names\na safety invariant blocks", text)


if __name__ == "__main__":
    unittest.main()
