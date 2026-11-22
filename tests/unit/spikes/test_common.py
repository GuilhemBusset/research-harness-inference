"""What the spikes share (`spikes/common.py`): timing, percentiles, reading the workspace, the canary
students' keys, and a run's numbers sent to the reporter. The workspace, the secret and the bus
are stubs. No number here is a measurement; the commit and every key are made while the tests run.
"""

from __future__ import annotations

import json
import math
import secrets
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import pytest
from botocore.credentials import Credentials

import common
from rhi_common.keys import generate_key
from rhi_common.spike_keys import SPIKE_KEYS_SECRET, SpikeKeys
from rhi_reporter.measure import BUS_VARIABLE, SOURCE, Run, Verdict, decode, read_event

if TYPE_CHECKING:
    from collections.abc import Mapping

BUS = "rhi-measurements"
WORKSPACE_URL = "https://aps-workspaces.example.com/workspaces/ws-example/"


class Start(StrEnum):
    """The names of the numbers of a spike made up for these tests."""

    WARM_START_P90 = "warm_start_seconds_p90"
    FAILED_STARTS = "failed_starts"


SPIKE = common.Spike("S1", Start, lambda numbers: numbers[Start.WARM_START_P90] < 300)
NUMBERS: dict[str, int | float] = {Start.WARM_START_P90: 212.5, Start.FAILED_STARTS: 0}


class Bus:
    """Stands for the EventBridge client: it keeps the entries it is given."""

    def __init__(self, failed: int = 0) -> None:
        self.entries: list[dict[str, str]] = []
        self.failed = failed

    def put_events(self, *, Entries: list[dict[str, str]]) -> dict[str, Any]:
        self.entries += Entries
        return {"FailedEntryCount": self.failed, "Entries": [{"EventId": "an event"} for _ in Entries]}


def runs(bus: Bus) -> list[tuple[str, Run]]:
    """Each entry the bus holds, read as the reporter reads the event EventBridge delivers for it:
    the commit of its statuses, and the run they decode into."""
    found = []
    for entry in bus.entries:
        assert (entry["Source"], entry["EventBusName"]) == (SOURCE, BUS)
        statuses = read_event(
            {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
        )
        (commit,) = {status.commit for status in statuses}
        (run,) = decode(statuses)
        found.append((commit, run))
    return found


@pytest.fixture
def commit() -> str:
    return secrets.token_hex(20)


@pytest.fixture
def environ(commit: str) -> dict[str, str]:
    """The job's environment: the bus and the commit, and no run label."""
    return {BUS_VARIABLE: BUS, common.COMMIT_VARIABLE: commit}


def test_timing_counts_seconds_on_the_clock_it_is_given() -> None:
    ticks = iter([100.0, 102.5, 110.0, 110.0, 113.0])
    watch = common.Stopwatch(clock=lambda: next(ticks))
    assert watch.seconds() == 2.5
    assert watch.seconds() == 10.0

    assert common.timed(lambda: "served", clock=lambda: next(ticks)) == ("served", 3.0)


def test_percentiles_of_known_samples() -> None:
    # 1 to 20, out of order: the 90th percentile is nine tenths of the way from the first to the last.
    samples = [float(n) for n in (7, 20, 3, 14, 1, 9, 18, 5, 12, 16, 2, 11, 19, 4, 8, 13, 6, 17, 10, 15)]
    assert common.percentile(samples, 90) == pytest.approx(18.1)
    assert common.percentile(samples, 95) == pytest.approx(19.05)
    assert common.median(samples) == pytest.approx(10.5)
    assert (common.percentile(samples, 0), common.percentile(samples, 100)) == (1.0, 20.0)
    # A percentile that falls on a sample is that sample.
    assert common.percentile([30.0, 10.0, 20.0, 50.0, 40.0], 75) == 40.0
    assert common.percentile([4.2], 95) == 4.2

    for refused, percent in (([], 50), (samples, 101), (samples, -1), ([1.0, math.nan], 50)):
        with pytest.raises(ValueError, match="percent"):
            common.percentile(refused, percent)


# An answer of the workspace to a range query, as the Prometheus HTTP API writes it.
RECORDED_ANSWER = json.dumps(
    {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "vllm:num_requests_running", "pool": "coursework", "engine": "0"},
                    "values": [[1800000000, "0"], [1800000015, "3"], [1800000030.5, "8"]],
                },
                {
                    "metric": {"__name__": "vllm:num_requests_running", "pool": "upgrade", "engine": "0"},
                    "values": [[1800000015, "NaN"]],
                },
            ],
        },
    }
).encode()


def test_a_recorded_answer_of_the_workspace_reads_back_as_its_series() -> None:
    sent: list[tuple[str, Mapping[str, str], bytes]] = []

    def send(url: str, headers: Mapping[str, str], body: bytes) -> bytes:
        sent.append((url, headers, body))
        return RECORDED_ANSWER

    workspace = common.Workspace(WORKSPACE_URL, "us-east-1", Credentials("stub", "stub"), send=send)

    coursework, upgrade = workspace.read_series(
        "vllm:num_requests_running", start=1_800_000_000, end=1_800_000_030.5, step=15
    )

    assert coursework.labels == {"__name__": "vllm:num_requests_running", "pool": "coursework", "engine": "0"}
    assert coursework.samples == ((1_800_000_000.0, 0.0), (1_800_000_015.0, 3.0), (1_800_000_030.5, 8.0))
    assert coursework.values() == [0.0, 3.0, 8.0]
    assert upgrade.labels["pool"] == "upgrade"
    assert math.isnan(upgrade.values()[0])

    # One request: a range query of the workspace, signed for Managed Prometheus in that region.
    ((url, headers, body),) = sent
    assert url == "https://aps-workspaces.example.com/workspaces/ws-example/api/v1/query_range"
    assert parse_qs(body.decode()) == {
        "query": ["vllm:num_requests_running"],
        "start": ["1800000000"],
        "end": ["1800000030.5"],
        "step": ["15"],
    }
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "/us-east-1/aps/aws4_request" in headers["Authorization"]

    # An answer that is not a result is refused, and the error repeats nothing of it.
    for answer in (b'{"status": "error", "error": "told by the workspace"}', b"told by the workspace"):
        with pytest.raises(ValueError, match="not the result") as refused:
            common.series_of(answer)
        assert "told" not in str(refused.value)


def test_the_canary_keys_come_from_the_spikes_secret() -> None:
    held = SpikeKeys(students=(generate_key(), generate_key()))
    asked: list[str] = []

    class Secrets:
        value = held.to_json()

        def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
            asked.append(SecretId)
            return {"SecretString": self.value}

    # The secret is written with the students alone.
    assert set(json.loads(held.to_json())) == {"students"}
    assert common.canary_keys(Secrets()) == held
    assert asked == [SPIKE_KEYS_SECRET]
    # A reading does not show its keys.
    assert not any(key in repr(held) for key in held.students)

    # A secret of the earlier shape, with a spent student's key, reads to the same students; that
    # key is ignored, and kept nowhere.
    spent = generate_key()
    Secrets.value = json.dumps({"students": list(held.students), "spent": spent})
    earlier = common.canary_keys(Secrets())
    assert earlier == held
    assert spent not in repr(vars(earlier))

    # A secret of another content is refused, and the error repeats nothing of it: not JSON, no
    # students in either shape, a key that is not valid, another set of fields.
    # The keys that are not valid are refused without being repeated either.
    stray = generate_key()
    truncated = stray[:-1]
    unlike = f"unlike-a-key-{secrets.token_hex(8)}"
    for value in (
        stray,
        json.dumps({"students": []}),
        json.dumps({"students": [], "spent": stray}),
        json.dumps({"students": [truncated]}),
        json.dumps({"students": [stray, unlike]}),
        json.dumps({"students": [unlike], "spent": stray}),
        json.dumps({"students": [stray], "other": stray}),
        json.dumps({"keys": [stray]}),
    ):
        Secrets.value = value
        with pytest.raises(ValueError, match="keys") as refused:
            common.canary_keys(Secrets())
        assert not any(held in str(refused.value) for held in (stray, truncated, unlike))


def test_numbers_sent_to_the_bus_decode_into_the_same_run(environ: dict[str, str], commit: str) -> None:
    bus = Bus()

    assert common.run_spike(SPIKE, "s1_warm_start", lambda: NUMBERS, environ=environ, events=bus) is Verdict.PASS

    ((on_commit, run),) = runs(bus)
    assert on_commit == commit
    assert (run.measurement, run.label, run.verdict) == ("S1", "s1-warm-start", Verdict.PASS)
    assert dict(run.numbers) == NUMBERS
    # The names the spike declares travel with its numbers.
    assert json.loads(bus.entries[0]["Detail"])["names"] == [name.value for name in Start]

    # The verdict is the pass condition's, of the numbers.
    slow = {**NUMBERS, Start.WARM_START_P90: 431.0}
    assert common.run_spike(SPIKE, "s1_warm_start", lambda: slow, environ=environ, events=bus) is Verdict.FAIL
    assert (runs(bus)[1][1].verdict, dict(runs(bus)[1][1].numbers)) == (Verdict.FAIL, slow)

    # A bus that does not take the event fails the job.
    with pytest.raises(RuntimeError, match="did not take"):
        common.run_spike(SPIKE, "s1_warm_start", lambda: NUMBERS, environ=environ, events=Bus(failed=1))


def test_the_run_label(environ: dict[str, str]) -> None:
    # The label the owner gives when starting the job.
    bus = Bus()
    common.run_spike(SPIKE, "s1_warm_start", lambda: NUMBERS, environ={**environ, "RHI_RUN_LABEL": "cold"}, events=bus)
    assert runs(bus)[0][1].label == "cold"

    # Without one, the job's name with hyphens.
    bus = Bus()
    common.run_spike(SPIKE, "s3_harness_task", lambda: NUMBERS, environ=environ, events=bus)
    assert runs(bus)[0][1].label == "s3-harness-task"

    # A label of another shape, given (an empty one too) or made from the job's name: nothing is
    # measured and nothing is sent.
    measured: list[str] = []

    def measure() -> dict[str, int | float]:
        measured.append("measured")
        return NUMBERS

    bus = Bus()
    for label in ("warm_start", "Warm", ""):
        with pytest.raises(ValueError, match="run label"):
            common.run_spike(SPIKE, "s1_warm_start", measure, environ={**environ, "RHI_RUN_LABEL": label}, events=bus)
    with pytest.raises(ValueError, match="run label"):
        common.run_spike(SPIKE, "S1_warm_start", measure, environ=environ, events=bus)
    assert (measured, bus.entries) == ([], [])

    # A spike that raises before it has its numbers: one event, the verdict error and no number.
    def fails() -> dict[str, int | float]:
        raise TimeoutError("the instance did not start")

    with pytest.raises(TimeoutError):
        common.run_spike(SPIKE, "s1_warm_start", fails, environ=environ, events=bus)
    ((_, run),) = runs(bus)
    assert (run.label, run.verdict, dict(run.numbers)) == ("s1-warm-start", Verdict.NOT_MEASURED, {})


def test_a_job_that_cannot_post_measures_nothing(environ: dict[str, str]) -> None:
    measured: list[str] = []

    def measure() -> dict[str, int | float]:
        measured.append("measured")
        return NUMBERS

    bus = Bus()
    not_a_commit = {**environ, common.COMMIT_VARIABLE: "main"}
    without_a_bus = {name: value for name, value in environ.items() if name != BUS_VARIABLE}
    for incomplete in (not_a_commit, without_a_bus):
        with pytest.raises(ValueError, match="commit"):
            common.run_spike(SPIKE, "s1_warm_start", measure, environ=incomplete, events=bus)
    assert (measured, bus.entries) == ([], [])

    # Numbers under a name the spike does not declare are not posted: the run is not measured.
    with pytest.raises(ValueError, match="enumeration"):
        common.run_spike(SPIKE, "s1_warm_start", lambda: {**NUMBERS, "undeclared": 1}, environ=environ, events=bus)
    assert [(run.verdict, dict(run.numbers)) for _, run in runs(bus)] == [(Verdict.NOT_MEASURED, {})]
