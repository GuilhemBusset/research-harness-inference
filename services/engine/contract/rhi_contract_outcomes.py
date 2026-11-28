"""The pytest plugin `run.sh` loads: the outcome of each of the four contract tests, a failed test's
reason, and nothing else.

`run.sh` runs the contract suite on the node with pytest's own output sent nowhere: a failure's
report carries the engine's answers and the test's arguments, the pool's secret among them, so no
report is printed and none is written to a file (no JUnit file, no cache). This plugin keeps, in
memory, one outcome for each test of `TESTS` and, when the session ends, writes them to the file
descriptor `OUTCOMES` as lines `<test> <outcome>`, the outcome one of `passed`, `failed`, `error`
or `skipped`. A test that did not run has no line.

Then, for each of those tests whose call failed, one more line `<test> reason <request> <check>
<finish>`. The suite begins every failure's message with the line `<request> <check> <finish>`
(tests/contract/test_engine_contract.py, `Exchange`); that line is kept only when each of its three
words is one of the vocabulary below (system design, section 4.1), and `none unexplained none` is
written otherwise. Only the names and words of this module are written, so the lines hold nothing
that the run met.
"""

from __future__ import annotations

import os
import re

import pytest

from rhi_common.engine_metrics import Quantity

# The four rows an engine at an address answers (tests/contract/test_engine_contract.py).
TESTS = ("test_inference", "test_health", "test_metrics", "test_bearer_secret")
# The file descriptor the outcomes go to, which `run.sh` opens on the container's standard output.
OUTCOMES = 3

# The words of a reason.
_FORMATS = ("chat", "responses", "messages")
REQUESTS = frozenset(
    {
        "health",
        "metrics",
        "count",
        "none",
        *(f"{f}-{way}{calling}" for f in _FORMATS for way in ("whole", "streamed") for calling in ("", "-calling")),
        *(f"{route}-{way}-secret" for route in (*_FORMATS, "count") for way in ("without", "wrong", "with")),
    }
)
CHECKS = frozenset(
    {
        *("not-a-stream", "no-end-event", "no-usage", "no-text", "a-call", "no-call", "arguments-not-an-object"),
        *("no-input-tokens", "metrics-elsewhere", "timeout", "no-connection", "unreadable", "unexplained"),
        *(f"metric-{kind}-{quantity.value}" for kind in ("missing", "kind") for quantity in Quantity),
    }
)
STATUS = re.compile(r"status-[0-9]{3}", re.ASCII)
FINISHES = frozenset(
    {"stop", "length", "tool_calls", "completed", "incomplete"}
    | {"end_turn", "max_tokens", "tool_use", "stop_sequence", "other", "none"}
)
UNEXPLAINED = "none unexplained none"

_outcomes: dict[str, str] = {}
_reasons: dict[str, str] = {}


def outcome_of(report: pytest.TestReport) -> str | None:
    """What one phase of a test says of the test: a failed call is `failed`, a failed setup or
    teardown is `error`, a skip is `skipped`, a passed call is `passed`; a passed setup or teardown
    says nothing."""
    if report.failed:
        return "failed" if report.when == "call" else "error"
    if report.skipped:
        return "skipped"
    return "passed" if report.when == "call" else None


def reason_of(error: BaseException) -> str:
    """The first line of the error's message, when it is three words of the vocabulary; `none
    unexplained none` otherwise."""
    try:
        lines = str(error).splitlines()
    except Exception:  # noqa: BLE001 - an error whose message cannot be read is unexplained
        return UNEXPLAINED
    words = lines[0].split(" ") if lines else []
    if len(words) != 3:
        return UNEXPLAINED
    request, check, finish = words
    if request in REQUESTS and (check in CHECKS or STATUS.fullmatch(check)) and finish in FINISHES:
        return lines[0]
    return UNEXPLAINED


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:
    """Keep the reason of a failed call. Returns nothing, so pytest makes its report as ever."""
    name = item.nodeid.rpartition("::")[2]
    if name in TESTS and call.when == "call" and call.excinfo is not None:
        _reasons[name] = reason_of(call.excinfo.value)


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
    failed = [name for name in TESTS if _outcomes.get(name) == "failed"]
    lines += "".join(f"{name} reason {_reasons.get(name, UNEXPLAINED)}\n" for name in failed)
    try:
        os.write(OUTCOMES, lines.encode("ascii"))
    except OSError:
        # No descriptor to write to: the run reports every test as missing.
        pass
