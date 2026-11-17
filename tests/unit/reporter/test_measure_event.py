"""The measurement event a job sends the reporter, built and read by the measure module (system
design, sections 8.4 and 12). No number here is a measurement, and the commit is made here.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from rhi_reporter.measure import BUS_VARIABLE, SOURCE, Verdict, decode, encode, measurement_event, read_event

BUS = "rhi-measurements"
NAMES = ["output_speed_median", "time_to_first_token_p95", "preemptions"]
NUMBERS: dict[str, int | float] = {"output_speed_median": 19.5, "time_to_first_token_p95": 3.25, "preemptions": 0}


def delivered(entry: dict[str, str]) -> dict[str, Any]:
    """The event EventBridge delivers for a PutEvents entry: the detail parsed, around it fields
    the reporter does not read."""
    return {
        "version": "0",
        "id": "an event",
        "detail-type": entry["DetailType"],
        "source": entry["Source"],
        "account": "123456789012",
        "time": "2026-11-02T09:31:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail": json.loads(entry["Detail"]),
    }


@pytest.fixture
def commit() -> str:
    return secrets.token_hex(20)


@pytest.mark.parametrize("verdict", list(Verdict))
def test_an_event_reads_back_as_the_statuses_of_its_run(commit: str, verdict: Verdict) -> None:
    entry = measurement_event("S5", "8bit", verdict, NUMBERS, names=NAMES, commit=commit, bus=BUS)
    assert (entry["Source"], entry["EventBusName"]) == (SOURCE, BUS) == ("rhi.measurement", BUS)
    assert BUS_VARIABLE == "RHI_MEASUREMENT_BUS"
    detail = json.loads(entry["Detail"])
    assert detail == {
        "commit": commit,
        "measurement": "S5",
        "label": "8bit",
        "verdict": {Verdict.PASS: "pass", Verdict.FAIL: "fail", Verdict.NOT_MEASURED: "error"}[verdict],
        "names": NAMES,
        "numbers": NUMBERS,
    }
    statuses = read_event(delivered(entry))
    assert statuses == encode("S5", "8bit", verdict, NUMBERS, names=NAMES, commit=commit)
    (run,) = decode(statuses)
    assert (run.verdict, dict(run.numbers)) == (verdict, NUMBERS)


def test_a_job_cannot_build_an_event_the_reporter_would_refuse(commit: str) -> None:
    with pytest.raises(ValueError, match="enumeration"):
        measurement_event("S5", "8bit", Verdict.PASS, {"other": 1}, names=NAMES, commit=commit, bus=BUS)


def test_only_the_detail_is_read(commit: str) -> None:
    event = delivered(measurement_event("S5", "8bit", Verdict.PASS, NUMBERS, names=NAMES, commit=commit, bus=BUS))
    expected = read_event(event)
    assert read_event({"detail": event["detail"]}) == expected
    assert read_event(event | {"source": "another", "account": "111122223333", "detail-type": "x"}) == expected


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"detail": "not an object"}, id="a detail that is not an object"),
        pytest.param({"numbers": None}, id="no numbers"),
        pytest.param({"numbers": [["preemptions", 0]]}, id="numbers that are not named"),
        pytest.param({"numbers": {"preemptions": True}}, id="a truth value for a number"),
        pytest.param({"numbers": {"preemptions": "0"}}, id="text for a number"),
        pytest.param({"names": "preemptions"}, id="names that are not a list"),
        pytest.param({"names": [*NAMES, "invalid/name"]}, id="a declared name of another shape"),
        pytest.param({"verdict": "success"}, id="a verdict that is not pass, fail or error"),
        pytest.param({"label": 8}, id="a run label that is not text"),
        pytest.param({"commit": "0" * 39}, id="a commit that is not a full SHA"),
        pytest.param({"measurement": "s5"}, id="an id that is not a measurement's"),
    ],
)
def test_refuses_a_detail_of_another_shape(commit: str, change: dict[str, Any]) -> None:
    event = delivered(measurement_event("S5", "8bit", Verdict.PASS, NUMBERS, names=NAMES, commit=commit, bus=BUS))
    if "detail" in change:
        event |= change
    else:
        event["detail"] |= change
    with pytest.raises(ValueError):
        read_event(event)


def test_refuses_a_detail_that_lacks_a_part(commit: str) -> None:
    event = delivered(measurement_event("S5", "8bit", Verdict.PASS, NUMBERS, names=NAMES, commit=commit, bus=BUS))
    del event["detail"]["names"]
    with pytest.raises(ValueError, match="not a measurement's"):
        read_event(event)
