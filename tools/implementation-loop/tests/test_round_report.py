"""`round_report.py`: failed rounds of the issues merged in a PR range, by trigger, from synthetic journals
written in the shape `RunStore.event` writes (`seq`, `ts`, `kind`, sorted keys)."""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "round_report.py"
# Loaded by path: the verifier puts its own engine's directory first on PYTHONPATH.
_spec = importlib.util.spec_from_file_location("round_report_of_this_tree", SCRIPT)
assert _spec is not None and _spec.loader is not None
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)


def journal(*events: tuple[str, dict]) -> list[str]:
    return [json.dumps({"seq": i, "ts": 1.0 + i, "kind": kind, **fields}, sort_keys=True)
            for i, (kind, fields) in enumerate(events, 1)]


def failed(n):
    return ("round_failed", {"issue": n, "unproductive": 0, "problems": ["x"]})


def examined(n, exam, unmet):
    return ("examined", {"issue": n, "head": "a" * 40, "findings": {}, "unmet": unmet, "missing": [],
                         "certify": False, "rejected": [], "examination": exam})


def verified(n, *problems):
    return ("verified", {"issue": n, "head": "b" * 40, "ok": False, "problems": list(problems), "closed": []})


def refused(n):
    return ("requests_decided", {"issue": n, "decided": {f"R{n}-1": "refused"}})


def merged(n, pr):
    return ("merged", {"issue": n, "pr": pr, "head": "c" * 40})


PROTECTED = "protected paths changed without approval: ['tools/hygiene/scan.py'] (ask with a `protected_change` request)"
OPEN = "open finding E7-1-1 still reproduces: a title"


def zero_except(counts, **expected):
    want = dict.fromkeys(rr.LABELS, 0) | {getattr(rr, k): v for k, v in expected.items()}
    assert counts == want


def test_interleaved_issues_keep_their_own_trigger_and_stop_at_their_first_merge_in_range():
    lines = journal(
        examined(10, 2, ["A1"]), verified(11, PROTECTED), refused(12),
        failed(11), failed(10), failed(12),
        merged(10, 100),                                    # the lower bound is included
        examined(10, 5, []), failed(10), merged(10, 101),   # a fix-forward after its first merge
        merged(11, 101),
        merged(12, 99),                                     # out of range: neither merged nor counted
        merged(13, 102),                                    # the upper bound is included, no failed round
        verified(14, OPEN), failed(14), merged(14, 103))
    zero_except(rr.report(lines, 100, 102), MERGED_ISSUES=3, ISSUES_WITH_FAILED_ROUNDS=2, FAILED_ROUNDS=2,
                EXAMINATION=1, EXAM_BEFORE_UNMET=1, PROTECTED=1)


def test_each_trigger_has_its_own_count():
    events = []
    for exam, unmet, times in ((1, ["A1"], 1), (3, [], 2), (4, ["A2"], 3), (6, [], 4)):
        events += [examined(20, exam, unmet), failed(20)] * times
    events += [verified(20, "a check failed", OPEN), failed(20)] * 5
    events += [verified(20, OPEN, PROTECTED), failed(20)] * 6
    events += [("built", {"issue": 20, "sha": "d" * 40, "status": "blocked", "requests": ["R20-1"]}),
               refused(20), failed(20)] * 7
    # After a refused request the next build fails a standing check before `built`: its own, unknown round.
    events += [refused(20), failed(20), ("build_not_committed", {"issue": 20, "problems": ["x"]}), failed(20)]
    events += [verified(20, "no command proves A1"), failed(20),
               ("push_withheld", {"issue": 20, "problems": ["x"]}), failed(20),
               ("ci_failed", {"issue": 20, "conclusion": "failure"}), failed(20),
               ("built", {"issue": 20, "status": "blocked", "requests": []}), failed(20)]
    events += [("requests_decided", {"issue": 20, "decided": {"R20-9": "approved"}}), failed(20)] * 4
    events.append(merged(20, 7))
    zero_except(rr.report(journal(*events), 7, 7), MERGED_ISSUES=1, ISSUES_WITH_FAILED_ROUNDS=1, FAILED_ROUNDS=38,
                EXAMINATION=10, EXAM_BEFORE_UNMET=1, EXAM_BEFORE_MET=2, EXAM_FROM_UNMET=3, EXAM_FROM_MET=4,
                OPEN_FINDING=5, PROTECTED=6, REJECTED=8, UNKNOWN=9)


def test_historical_rows_without_the_metadata_count_as_unknown():
    lines = journal(
        failed(30),                                                     # no cause journalled
        ("examined", {"issue": 30, "findings": {}, "unmet": ["A1"]}), failed(30),   # before the ordinal
        ("examined", {"issue": 30, "findings": {}, "examination": 2}), failed(30),  # before `unmet`
        ("verified", {"issue": 30, "ok": False}), failed(30),
        ("requests_decided", {"issue": 30}), failed(30),
        ("round_failed", {"problems": ["no issue"]}),
        merged(30, 5))
    zero_except(rr.report([*lines, "{not json", "[1]"], 5, 5), MERGED_ISSUES=1, ISSUES_WITH_FAILED_ROUNDS=1,
                FAILED_ROUNDS=5, UNKNOWN=5, UNREADABLE=2)


def test_the_command_leaves_the_journal_intact_and_prints_only_fixed_labels_and_counts(tmp_path):
    marker = "PLANTED-MARKER"
    lines = journal(
        ("new_round", {"issue": 40, "round": 2, "reason": marker}),
        ("examined", {"issue": 40, "findings": {f"{marker}-id": "open"}, "unmet": [marker], "examination": 1}),
        ("round_failed", {"issue": 40, "unproductive": 0, "problems": [f"blocking finding E40-1-1: {marker}"]}),
        verified(40, f"open finding E40-1-1 still reproduces: {marker}"), failed(40),
        ("failed", {"issue": 41, "cause": "ceiling", "detail": marker}),
        merged(40, 1))
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o444)
    before = (path.read_bytes(), os.stat(path).st_mtime_ns, sorted(tmp_path.iterdir()))
    run = subprocess.run([sys.executable, str(SCRIPT), "--journal", str(path), "--from", "1", "--to", "1"],
                         capture_output=True, text=True, check=False, cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert (path.read_bytes(), os.stat(path).st_mtime_ns, sorted(tmp_path.iterdir())) == before
    assert marker not in run.stdout + run.stderr
    printed = [re.fullmatch(r"(.+): (\d+)", line) for line in run.stdout.splitlines()]
    assert all(printed)
    counts = {m.group(1): int(m.group(2)) for m in printed if m}
    assert counts == rr.report(lines, 1, 1)
    assert list(counts) == list(rr.LABELS)
    assert counts[rr.EXAM_BEFORE_UNMET] == 1 and counts[rr.OPEN_FINDING] == 1
