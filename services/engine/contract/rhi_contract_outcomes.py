"""The pytest plugin `run.sh` loads: the outcome of each of the four contract tests, and nothing else.

`run.sh` runs the contract suite on the node with pytest's own output sent nowhere: a failure's
report carries the engine's answers and the test's arguments, the pool's secret among them, so no
report is printed and none is written to a file (no JUnit file, no cache). This plugin keeps, in
memory, one outcome for each test of `TESTS` and, when the session ends, writes them to the file
descriptor `OUTCOMES` as lines `<test> <outcome>`, the outcome one of `passed`, `failed`, `error`
or `skipped`. A test that did not run has no line. Only the names and words of this module are
written, so the lines hold nothing that the run met.
"""

from __future__ import annotations

import os

import pytest

# The four rows an engine at an address answers (tests/contract/test_engine_contract.py).
TESTS = ("test_inference", "test_health", "test_metrics", "test_bearer_secret")
# The file descriptor the outcomes go to, which `run.sh` opens on the container's standard output.
OUTCOMES = 3

_outcomes: dict[str, str] = {}


def outcome_of(report: pytest.TestReport) -> str | None:
    """What one phase of a test says of the test: a failed call is `failed`, a failed setup or
    teardown is `error`, a skip is `skipped`, a passed call is `passed`; a passed setup or teardown
    says nothing."""
    if report.failed:
        return "failed" if report.when == "call" else "error"
    if report.skipped:
        return "skipped"
    return "passed" if report.when == "call" else None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    name = report.nodeid.rpartition("::")[2]
    outcome = outcome_of(report)
    if name not in TESTS or outcome is None:
        return
    # The first phase that did not pass decides: a call that failed stays failed whatever its teardown.
    if _outcomes.get(name, "passed") == "passed":
        _outcomes[name] = outcome


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    lines = "".join(f"{name} {_outcomes[name]}\n" for name in TESTS if name in _outcomes)
    try:
        os.write(OUTCOMES, lines.encode("ascii"))
    except OSError:
        # No descriptor to write to: the run reports every test as missing.
        pass
