"""Spike S5's job (`spikes/s5_capacity.py`): what it sends, what one job's verdict rests on, and how
a commit's runs reach the record. No number here is a measurement; the keys are generated.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import s5_capacity as s5
from rhi import config
from rhi_common.keys import generate_key
from rhi_gateway import routes
from rhi_gateway.profile.reasoning_effort import rewrite
from rhi_reporter.measure import Verdict, encode

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO = Path(__file__).resolve().parents[3]
COMMIT = "c" * 40
FLOORS = s5.Floors(5, 15, 10, 0.5)


def class_numbers(**changed: float) -> dict[str, float]:
    """Class-size numbers of both shapes that pass, with `changed` (by the several-agents name's
    quantity) for the several-agents run; a value of -1 leaves the number out."""
    passing = {
        s5.Figure.TTFT_P95: 4,
        s5.Figure.STREAM_MEDIAN: 16,
        s5.Figure.STREAM_P10: 11,
        s5.Figure.PREEMPTIONS: 0,
        s5.Figure.FAILED_SHARE: 0,
    }
    numbers: dict[str, float] = {"class_size": 20}
    for shape in s5.Shape:
        for figure, value in passing.items():
            numbers[s5.name_of(figure, shape, None)] = value
    for figure, value in changed.items():
        name = s5.name_of(s5.Figure(figure), s5.Shape.SEVERAL_AGENTS, None)
        if value < 0:
            numbers.pop(name)
        else:
            numbers[name] = value
    return numbers


def test_a_missed_floor_fails_even_when_another_number_is_missing() -> None:
    spike = s5.spike_of(FLOORS)
    assert spike.verdict(class_numbers()) is Verdict.PASS
    assert spike.verdict(class_numbers(ttft_p95=-1)) is Verdict.NOT_MEASURED
    assert spike.verdict(class_numbers(ttft_p95=-1, preemptions=1)) is Verdict.FAIL
    assert spike.verdict(class_numbers(preemptions=-1, stream_median=14)) is Verdict.FAIL
    # The failed share is one of those numbers: missing, no verdict; above its floor, a fail.
    assert spike.verdict(class_numbers(failed_share=-1)) is Verdict.NOT_MEASURED
    assert spike.verdict(class_numbers(failed_share=-1, preemptions=1)) is Verdict.FAIL
    assert spike.verdict(class_numbers(ttft_p95=-1, failed_share=1)) is Verdict.FAIL


def test_one_job_is_one_served_build(monkeypatch: pytest.MonkeyPatch) -> None:
    job = s5.Job(
        pool="coursework", engine="vllm", prices={}, lever="baseline", streams=(1,), class_size=1, duration=600,
        keys=[generate_key()], endpoint="https://example.com", gateway=None, workspace=None, log=lambda _: None,  # ty: ignore[invalid-argument-type] - no run reaches them
    )  # fmt: skip

    def run(planned: s5.Planned) -> s5.Measured:
        build = "a" * 40 if planned.shape is s5.Shape.HANDS_ON else "b" * 40
        label = s5.Label("coursework", ("g7e.2xlarge",), build, "baseline", planned.shape, 1, 1, planned.class_size)
        seen = s5.Experience(400, 0, 360, 40, 1.2, 4.0, 3.0, 4.5, 16.0, 11.0, 200_000)
        return s5.Measured(label, 600.0, seen, 6.5, {"a": s5.Instance("g7e.2xlarge", 600.0, s5.EngineQuantities())})

    monkeypatch.setattr(job, "run", run)
    with pytest.raises(ValueError, match="build"):
        job.measure()


def first_bodies(planned: s5.Planned, window: int | None = None) -> list[dict[str, Any]]:
    load = s5.load_of(planned, "coursework", 0, window)
    return [json.loads(next(session.requests())[1].body) for session in load.sessions]


def test_requests_are_sent_as_claude_code_sends_them() -> None:
    bodies = first_bodies(s5.Planned(s5.Shape.HANDS_ON, 2, 2))
    starts = [s5.loadgen.agent_session("coursework", 1, seed).start_tokens for seed in (0, 1)]
    # Every session opens with the same opening, then text of its own, and keeps its size.
    for body, start in zip(bodies, starts, strict=True):
        assert body["system"] == s5.opening()
        assert len(body["system"].split()) + len(body["messages"][0]["content"].split()) == start
    assert bodies[0]["messages"] != bodies[1]["messages"]
    # A compacted transcript opens with the same opening.
    windowed = first_bodies(s5.Planned(s5.Shape.HANDS_ON, 1, 1), 100_000)
    assert windowed[0]["system"] == s5.opening()

    # The effort Claude Code asks for is the one the pool's mapping rewrites: the lever changes it.
    example = config.parse(tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8")), "test")
    pool = next(one for one in example.pools if one.name == "coursework")
    lower = replace(pool, reasoning_effort={**(pool.reasoning_effort or {}), "high": "medium"})
    mapped = [rewrite(bodies[0], one, routes.MESSAGES)["output_config"]["effort"] for one in (pool, lower)]
    assert mapped == ["xhigh", "medium"]


def statuses(label: str, verdict: Verdict, numbers: Mapping[str, float], at: str) -> list[dict[str, str]]:
    """The statuses of one posting, as GitHub's API lists them."""
    return [
        {"context": one.context, "state": str(one.state), "description": one.description, "created_at": at}
        for one in encode("S5", label, verdict, numbers, names=[n.value for n in s5.Number], commit=COMMIT)
    ]


def rows_of(path: Path) -> list[dict[str, Any]]:
    return tomllib.loads(path.read_text(encoding="utf-8").split("+++")[1])["rows"]


@pytest.fixture
def record(tmp_path: Path) -> Path:
    path = tmp_path / "s5.md"
    shutil.copy(s5.RECORD, path)
    return path


def test_the_record_reads_paginated_statuses_and_each_run_from_its_newest_posting(record: Path) -> None:
    many = {s5.name_of(figure, shape, 8): 1.5 for figure in s5.Figure for shape in s5.Shape}
    first = statuses("8bit-baseline-1", Verdict.PASS, many, "2026-11-20T10:00:00Z")
    retry = statuses("8bit-baseline-1", Verdict.NOT_MEASURED, {}, "2026-11-20T11:00:00Z")
    assert len(first) > 1 == len(retry)
    # GitHub lists the newest first, a page at a time, the pages one after the other.
    listed = [{"context": "ci", "state": "success", "created_at": "2026-11-20T09:00:00Z"}, *retry, *first]
    pages = json.dumps(listed[:2]) + "\n" + json.dumps(listed[2:])
    assert s5.statuses_of(pages) == listed == s5.statuses_of(json.dumps([listed[:2], listed[2:]]))
    with pytest.raises(ValueError, match="statuses"):
        s5.statuses_of('{"message": "Not Found"}')

    s5.record_statuses(s5.statuses_of(pages), COMMIT, pool="coursework", instance_type="g7e.2xlarge", path=record)
    held = tomllib.loads(record.read_text(encoding="utf-8").split("+++")[1])
    assert [(run["run"], run["verdict"]) for run in held["runs"]] == [("8bit-baseline-1", "pending")]
    assert held.get("rows", []) == []
    # Without the retry, the first posting is the run.
    s5.record_statuses(first, COMMIT, pool="coursework", instance_type="g7e.2xlarge", path=record)
    assert len(rows_of(record)) == len(many)


def test_the_runs_of_each_instance_type_are_written_apart(record: Path) -> None:
    numbers = {"stream_median_hands_on_8": 16}
    baseline = statuses("8bit-baseline-1", Verdict.PASS, numbers, "2026-11-20T10:00:00Z")
    larger = statuses("8bit-larger-instance-1", Verdict.PASS, numbers, "2026-11-20T12:00:00Z")
    s5.record_statuses(baseline, COMMIT, pool="coursework", instance_type="g7e.2xlarge", path=record)
    before = record.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="instance type"):
        s5.record_statuses(larger + baseline, COMMIT, pool="coursework", instance_type="g7e.4xlarge", path=record)
    assert record.read_text(encoding="utf-8") == before
    written = s5.record_statuses(
        larger + baseline, COMMIT, pool="coursework", instance_type="g7e.4xlarge", path=record,
        only=["8bit-larger-instance-1"],
    )  # fmt: skip
    assert written == ["8bit-larger-instance-1"]
    assert {row["labels"]["lever"]: row["labels"]["instance_type"] for row in rows_of(record)} == {
        "baseline": "g7e.2xlarge",
        "larger-instance": "g7e.4xlarge",
    }
