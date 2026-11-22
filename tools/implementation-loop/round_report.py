#!/usr/bin/env python3
"""What sent merged issues back for another build: failed rounds by trigger, from a run's journal.

    python3 tools/implementation-loop/round_report.py --journal <run dir>/events.jsonl --from 531 --to 559

An issue is selected when it merged a pull request numbered within the inclusive range (`merged`,
`merge_found_on_resume` or `merged_externally`). Its failed rounds (`round_failed`) count up to
its first merge in the range, each under the trigger the engine journalled for that issue just
before it (see `TRIGGERS`). A round whose trigger is missing, or lacks the fields its class needs
(an engine older than the field), is `unknown`.

The journal is opened read-only and nothing is written. It is local because it holds text agents
wrote (a finding's title, a round's reason): the report prints only the fixed labels in `LABELS`
and counts, never a value read from the journal, so its output can be quoted and diffed across
runs. Standard library only; Python 3.11 or newer, like the engine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

# ADR 0001, amendment 12: from an issue's fourth examination since its latest plan only a finding
# that names a safety invariant blocks (`Caps.examiner_bound`). The `examined` event carries the
# ordinal the engine counted, so the split needs no counting here.
FOURTH = 4

MERGED_ISSUES = "merged issues"
ISSUES_WITH_FAILED_ROUNDS = "issues with failed rounds"
FAILED_ROUNDS = "failed rounds"
EXAMINATION = "examination"
EXAM_BEFORE_UNMET = "examination before the fourth, ledger verdict unmet"
EXAM_BEFORE_MET = "examination before the fourth, no ledger verdict unmet"
EXAM_FROM_UNMET = "examination from the fourth, ledger verdict unmet"
EXAM_FROM_MET = "examination from the fourth, no ledger verdict unmet"
OPEN_FINDING = "verification, open finding reproduced"
PROTECTED = "protected-path refusal"
REJECTED = "rejected request"
UNKNOWN = "unknown"
UNREADABLE = "unreadable lines"

EXAM_BUCKETS = (EXAM_BEFORE_UNMET, EXAM_BEFORE_MET, EXAM_FROM_UNMET, EXAM_FROM_MET)
TRIGGERS = (EXAMINATION, OPEN_FINDING, PROTECTED, REJECTED, UNKNOWN)
LABELS = (MERGED_ISSUES, ISSUES_WITH_FAILED_ROUNDS, FAILED_ROUNDS, *TRIGGERS[:1], *EXAM_BUCKETS,
          *TRIGGERS[1:], UNREADABLE)

MERGE_KINDS = frozenset({"merged", "merge_found_on_resume", "merged_externally"})
# Every event the engine journals right before a `round_failed` (`_round_failed`'s callers in
# `implement_loop/engine.py`). Each one starts its issue's next round's cause, and a failed round
# uses it up: a build that fails a standing check after a refused request (`build_not_committed`)
# is a round of its own, not a second refusal.
CAUSE_KINDS = frozenset({"built", "build_not_committed", "requests_decided", "verified", "examined",
                         "push_withheld", "ci_failed"})
# The verifier's own wording (`gate.protected_problems`, `engine._step_verify`); a finding's title
# follows the prefix and is never read.
PROTECTED_PREFIXES = ("protected paths changed without approval", "a fault-repair run may not change the safety surface")
OPEN_FINDING_RE = re.compile(r"open finding \S+ still reproduces")


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def trigger(event: dict | None) -> str:
    """The trigger of a failed round, from the cause event its issue journalled before it."""
    if event is None:
        return UNKNOWN
    kind = event.get("kind")
    if kind == "examined":
        exam, unmet = _number(event.get("examination")), event.get("unmet")
        if exam is None or not isinstance(unmet, list):
            return UNKNOWN
        if exam < FOURTH:
            return EXAM_BEFORE_UNMET if unmet else EXAM_BEFORE_MET
        return EXAM_FROM_UNMET if unmet else EXAM_FROM_MET
    if kind == "verified":
        problems = event.get("problems")
        if event.get("ok") is not False or not isinstance(problems, list):
            return UNKNOWN
        texts = [p for p in problems if isinstance(p, str)]
        if any(p.startswith(PROTECTED_PREFIXES) for p in texts):
            return PROTECTED
        if any(OPEN_FINDING_RE.match(p) for p in texts):
            return OPEN_FINDING
        return UNKNOWN
    if kind == "requests_decided":
        decided = event.get("decided")
        if isinstance(decided, dict) and "refused" in decided.values():
            return REJECTED
    return UNKNOWN


def report(lines: Iterable[str], first: int, last: int) -> dict[str, int]:
    """The counts for the merges numbered `first` to `last`, both included, every label present."""
    cause: dict[int, dict | None] = {}
    rounds: dict[int, list[str]] = {}   # an issue's failed rounds so far, by trigger, in order
    counted: dict[int, list[str]] = {}  # selected issues: their rounds through their first merge in range
    unreadable = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(event, dict):
            unreadable += 1
            continue
        n = _number(event.get("issue"))
        if n is None or n in counted:
            continue
        kind = event.get("kind")
        if kind in CAUSE_KINDS:
            cause[n] = event
        elif kind == "round_failed":
            rounds.setdefault(n, []).append(trigger(cause.pop(n, None)))
        elif kind in MERGE_KINDS:
            pr = _number(event.get("pr"))
            if pr is not None and first <= pr <= last:
                counted[n] = rounds.get(n, [])
    out = Counter({label: 0 for label in LABELS})
    out[MERGED_ISSUES] = len(counted)
    out[ISSUES_WITH_FAILED_ROUNDS] = sum(1 for r in counted.values() if r)
    for triggers in counted.values():
        out.update(triggers)
    out[EXAMINATION] = sum(out[b] for b in EXAM_BUCKETS)
    out[FAILED_ROUNDS] = sum(out[t] for t in TRIGGERS)
    out[UNREADABLE] = unreadable
    return {label: out[label] for label in LABELS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="round_report", description="Failed rounds of merged issues, by trigger.")
    parser.add_argument("--journal", required=True, type=Path, help="a run's events.jsonl (read only)")
    parser.add_argument("--from", dest="first", required=True, type=int, help="first merge PR number, included")
    parser.add_argument("--to", dest="last", required=True, type=int, help="last merge PR number, included")
    args = parser.parse_args(argv)
    if args.first > args.last:
        parser.error("--from is after --to")
    try:
        with args.journal.open(encoding="utf-8", errors="replace") as f:
            counts = report(f, args.first, args.last)
    except OSError:
        print("round_report: the journal cannot be read", file=sys.stderr)
        return 2
    for label, count in counts.items():
        print(f"{label}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
