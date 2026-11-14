"""Independent checks of issue 34's run schema; helpers for the other probes."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("measurement_validator", Path("tests/unit/test_measurements.py"))
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

HEAD = 'id = "S4"\npass_condition = "Under 100 s for the coursework model\'s 31 GB"\nverdict = "pass"\n'
RUN = '[[runs]]\nrun = "download"\ndate = 2026-01-15\ncommit = "' + "a" * 40 + '"\nverdict = "pass"\n'
ROW = '[[rows]]\nname = "load_time"\nvalue = 90\nunit = "s"\nlabels = { run = "download", build = "8bit" }\n'


def document(head=HEAD, runs=RUN, rows=ROW):
    return "+++\n" + "\n".join((head, runs, rows)) + "\n+++\nFree Markdown.\n"


@pytest.mark.parametrize("label", ["8bit", "4bit", "warm", "cold", "stream", "download"])
def test_valid_run_labels(label):
    assert VALIDATOR.file_problems(document(runs=RUN.replace("download", label), rows="")) == []


@pytest.mark.parametrize(
    "runs,head",
    [
        (RUN.replace("download", "Upper"), HEAD),
        (RUN.replace("download", "with_space "), HEAD),
        (RUN + RUN, HEAD),
        (RUN.replace("date = 2026-01-15\n", ""), HEAD),
        (RUN.replace("2026-01-15", '"2026-01-15"'), HEAD),
        (RUN.replace('commit = "' + "a" * 40 + '"\n', ""), HEAD),
        (RUN.replace('verdict = "pass"', 'verdict = "waived"'), HEAD),
        (RUN.replace('verdict = "pass"', 'verdict = "pending"'), HEAD.replace('"pass"', '"pending"')),
    ],
)
def test_invalid_runs_are_rejected(runs, head):
    assert VALIDATOR.file_problems(document(head=head, runs=runs, rows=""))
