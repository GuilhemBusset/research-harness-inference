"""Spike S5's verdict and record (`spikes/s5_capacity.py`): the floors at the class's size, where
the floors come from, the numbers posted, and the record that holds the whole matrix. No number here
is a measurement; the instance names are made up, and the keys are generated.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import itertools
import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import common
import loadgen
import s5_capacity as s5
from rhi import config
from rhi_common.keys import generate_key
from rhi_reporter.measure import Verdict, decode, read_event

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))
FLOORS = (
    "target_ttft_p95_seconds",
    "target_stream_median_tokens_per_second",
    "target_stream_p10_tokens_per_second",
    "target_failed_requests_percent",
)
COMMIT = "c" * 40


def measurement_format() -> Any:
    """The format's own test, `tests/unit/test_measurements.py`."""
    spec = importlib.util.spec_from_file_location("measurement_format", REPO / "tests/unit/test_measurements.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(**keys: object) -> config.Config:
    """The example configuration, with the keys given; a key given None is left out."""
    data = {name: value for name, value in (EXAMPLE | keys).items() if value is not None}
    return config.parse(data, "test")


def run(
    shape: s5.Shape,
    streams: int,
    *,
    class_size: int | None = None,
    p95: float = 4.0,
    median: float = 16.0,
    p10: float = 11.0,
    preemptions: float | None = 0,
    build: str = "8bit",
    requests: int = 400,
    failed: int = 0,
) -> s5.Measured:
    """A recorded run: its students' times and speeds, how many of its requests failed, and one
    instance's engine."""
    label = s5.Label("coursework", ("g7e.2xlarge",), build, "baseline", shape, streams, streams, class_size)
    seen = s5.Experience(requests, failed, 360, 40, 1.2, p95, 3.0, 4.5, median, p10, 200_000)
    engine = s5.EngineQuantities(
        output_tokens_per_second=150.0,
        prefill_tokens_per_second=5_000.0,
        cached_share=0.9,
        cache_capacity_tokens=1_400_000,
        running_requests_peak=8,
        preemptions=preemptions,
    )
    instance = s5.Instance("g7e.2xlarge", 600.0, engine)
    return s5.Measured(label, 600.0, seen, 6.5, {"instance-a": instance})


def job(class_size: int = 20, build: str = "8bit", **at_class: Any) -> list[s5.Measured]:
    """A job's runs: both shapes at 1 and 8 streams, and at the class's size."""
    runs = [run(shape, streams, build=build) for shape in s5.Shape for streams in (1, 8)]
    # A run at a number of streams below the floors does not enter the verdict.
    runs.append(run(s5.Shape.HANDS_ON, 16, p95=30.0, median=5.0, p10=2.0, preemptions=4, build=build))
    return runs + [run(shape, class_size, class_size=class_size, build=build, **at_class) for shape in s5.Shape]


def verdict(measured: list[s5.Measured], configuration: config.Config | None = None) -> Verdict:
    floors = s5.floors_of(configuration or settings())
    return s5.spike_of(floors).verdict(s5.numbers_of(measured, 20))


@pytest.mark.parametrize(
    ("at_class", "expected"),
    [
        ({}, Verdict.PASS),
        ({"p95": 6.0}, Verdict.FAIL),
        ({"median": 14.0}, Verdict.FAIL),
        ({"p10": 9.0}, Verdict.FAIL),
        ({"preemptions": 1}, Verdict.FAIL),
        # An engine that does not count preemptions cannot show there were none.
        ({"preemptions": None}, Verdict.NOT_MEASURED),
        # 4 failed requests of 400 are above the floor, whatever else is missing.
        ({"failed": 4}, Verdict.FAIL),
        ({"failed": 4, "preemptions": None}, Verdict.FAIL),
        # A run that sent no request has no failed share; a floor it misses still fails the job.
        ({"requests": 0}, Verdict.NOT_MEASURED),
        ({"requests": 0, "p95": 6.0}, Verdict.FAIL),
    ],
)
def test_the_class_size_runs_are_judged_against_the_floors(at_class: dict[str, Any], expected: Verdict) -> None:
    assert verdict(job(**at_class)) is expected
    # Either shape alone missing a floor fails the job.
    if expected is Verdict.FAIL:
        one_shape = job()
        one_shape[-1] = run(s5.Shape.SEVERAL_AGENTS, 20, class_size=20, **at_class)
        assert verdict(one_shape) is Verdict.FAIL


def test_a_class_size_failed_share_above_its_floor_fails_the_job() -> None:
    def shares(hands_on: int, several_agents: int, configuration: config.Config | None = None) -> Verdict:
        """The verdict of a job whose class-size runs, of 400 requests each, had that many failed,
        and meet every other floor."""
        failed = {s5.Shape.HANDS_ON: hands_on, s5.Shape.SEVERAL_AGENTS: several_agents}
        runs = job()[:-2] + [run(shape, 20, class_size=20, failed=failed[shape]) for shape in s5.Shape]
        numbers = s5.numbers_of(runs, 20)
        assert numbers["failed_share_hands_on_class"] == 100 * hands_on / 400
        assert numbers["failed_share_several_agents_class"] == 100 * several_agents / 400
        return verdict(runs, configuration)

    # 2 of 400 are a share of 0.5, the default floor, which both shapes may reach; 4 are a share of 1.
    assert shares(2, 2) is Verdict.PASS
    assert shares(4, 2) is Verdict.FAIL
    assert shares(2, 4) is Verdict.FAIL
    # 6 are a share of 1.5, which a configuration whose floor is 2 passes.
    assert shares(6, 6) is Verdict.FAIL
    assert shares(6, 6, settings(target_failed_requests_percent=2)) is Verdict.PASS
    # The runs at a number of streams do not enter the verdict, whatever failed in them.
    assert verdict([*job(), run(s5.Shape.HANDS_ON, 8, failed=200)]) is Verdict.PASS


def test_the_floors_come_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Left out, the keys take section 17's values, and the failed share its own default.
    assert s5.floors_of(settings(**dict.fromkeys(FLOORS))) == s5.Floors(5, 15, 10, 0.5)
    stricter = settings(target_stream_median_tokens_per_second=17.5)
    assert verdict(job(median=16.0)) is Verdict.PASS
    assert verdict(job(median=16.0), stricter) is Verdict.FAIL

    # The job reads them from the deployed configuration, and posts the numbers with their verdict.
    sent: list[Mapping[str, str]] = []
    events = SimpleNamespace(put_events=lambda Entries: sent.extend(Entries) or {"FailedEntryCount": 0})
    monkeypatch.setattr(s5.config, "read_ssm", lambda: stricter)
    monkeypatch.setattr(s5.Job, "measure", lambda self: job(self.runs[-1].class_size or 0, median=16.0))
    monkeypatch.setattr(s5.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(s5.boto3, "client", lambda service: events)
    monkeypatch.setattr(common, "canary_keys", lambda: SimpleNamespace(students=(generate_key(),)))
    environ = {
        "AWS_REGION": "us-east-1",
        "RHI_SPIKE_WORKSPACE_URL": "https://example.com/workspace",
        "RHI_SPIKE_ENDPOINT": "example.com",
        "RHI_S5_BUILD": "8bit",
        "RHI_S5_CLASS_SIZE": "20",
        "RHI_S5_DURATION_SECONDS": "600",
        "RHI_MEASUREMENT_BUS": "rhi-measurements",
        "CODEBUILD_RESOLVED_SOURCE_VERSION": COMMIT,
    }
    assert s5.main(environ) is Verdict.FAIL
    (entry,) = sent
    (posted,) = decode(read_event({"detail": json.loads(entry["Detail"])}))
    assert (posted.measurement, posted.label, posted.verdict) == ("S5", "8bit-baseline-1", Verdict.FAIL)
    assert posted.numbers["stream_median_hands_on_class"] == 16.0
    assert posted.numbers["class_size"] == 20


def test_the_names_are_declared_one_per_quantity_and_condition() -> None:
    names = [number.value for number in s5.Number]
    conditions = len(s5.Shape) * (len(s5.DECLARED_STREAMS) + 1)
    # And two of the job: the class's size, and how many numbers the posting holds.
    assert len(set(names)) == len(names) == len(s5.Figure) * conditions + 2
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names)
    # What each run measured is posted, the engine's quantities among them.
    numbers = s5.numbers_of(job(), 20)
    assert set(numbers) <= set(names)
    assert numbers["prefill_speed_several_agents_8"] == 5_000.0
    assert numbers["cached_share_hands_on_class"] == pytest.approx(90.0)
    assert numbers["cache_capacity_hands_on_1"] == 1_400_000
    # A count no name is declared for is refused before anything is sent.
    assert s5.streams_of({"RHI_S5_STREAMS": "1,32"}) == (1, 32)
    with pytest.raises(ValueError, match="among"):
        s5.streams_of({"RHI_S5_STREAMS": "1,12"})


def test_a_run_the_pool_was_not_held_for_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    one = run(s5.Shape.HANDS_ON, 1)
    two = dataclasses.replace(one, instances={**one.instances, "instance-b": one.instances["instance-a"]})
    late = s5.Instance("g7e.2xlarge", 200.0, s5.EngineQuantities())
    grown = dataclasses.replace(two, instances={**one.instances, "instance-b": late})
    assert s5.held_at(one, 1) and s5.held_at(two, 2)
    assert not s5.held_at(two, 1) and not s5.held_at(grown, 2)

    held = s5.Job(
        pool="coursework", engine="vllm", prices={}, lever="baseline", streams=(1,), class_size=1, duration=60,
        keys=[generate_key()], endpoint="https://example.com", gateway=None, workspace=None, log=lambda _: None,  # ty: ignore[invalid-argument-type] - no run reaches them
        instances=2,
    )  # fmt: skip
    monkeypatch.setattr(held, "run", lambda planned: one)
    with pytest.raises(ValueError, match="instances"):
        held.measure()


def test_a_session_compacts_at_the_window() -> None:
    session = dataclasses.replace(loadgen.agent_session("coursework", 10, seed=3), start_tokens=90_000)

    def sent(one: loadgen.Session) -> list[tuple[float, int]]:
        steps, found = one.requests(), []
        step = next(steps)
        while True:
            body = json.loads(step[1].body)
            found.append((step[0], sum(len(message["content"].split()) for message in body["messages"])))
            try:
                step = steps.send("an answer")
            except StopIteration:
                return found

    whole, windowed = sent(session), sent(s5.Windowed(session, 100_000))
    assert [pause for pause, _ in windowed] == [pause for pause, _ in whole]
    # The whole transcript grows past the window; the compacted one starts again, from new text.
    assert whole[-1][1] > 100_000 > max(tokens for _, tokens in windowed)
    assert any(later < earlier for (_, earlier), (_, later) in itertools.pairwise(windowed))
    assert s5.load_of(s5.Planned(s5.Shape.SEVERAL_AGENTS, 3, 2), "coursework", 0, 100_000).sessions_per_student == 2


def blank_record(path: Path) -> Path:
    """The record at `path` with none of its runs: a run the real record holds would join a
    test's runs in the record's verdict."""
    tool = s5.record_tool()
    record, prose = tool.read(s5.RECORD.read_text(encoding="utf-8"))
    path.write_text(tool.written({**record, "verdict": "pending", "runs": [], "rows": []}) + prose, encoding="utf-8")
    return path


def test_the_record_holds_the_matrix(tmp_path: Path) -> None:
    form = measurement_format()
    assert form.file_problems(s5.RECORD.read_text(encoding="utf-8"), "s5.md") == []

    statuses = []
    for build in ("8bit", "4bit"):
        numbers = s5.numbers_of(job(build=build), 20)
        event = {"detail": {
            "commit": COMMIT, "measurement": "S5", "label": f"{build}-baseline-1", "verdict": "pass",
            "names": [name.value for name in s5.Number], "numbers": numbers,
        }}  # fmt: skip
        statuses += [
            {"context": one.context, "state": str(one.state), "description": one.description,
             "created_at": "2026-11-20T10:00:00Z"}
            for one in read_event(event)
        ]  # fmt: skip
    path = blank_record(tmp_path / "s5.md")

    assert s5.record_statuses(statuses, COMMIT, pool="coursework", instance_type="g7e.2xlarge", path=path) == [
        "8bit-baseline-1",
        "4bit-baseline-1",
    ]
    text = path.read_text(encoding="utf-8")
    assert form.file_problems(text, "s5.md") == []
    held = form.front_matter(text)
    assert held["verdict"] == "pass"
    # One name a quantity, its rows told apart by their labels: two builds, two shapes, and the
    # streams, two counts and the class.
    medians = [row["labels"] for row in held["rows"] if row["name"] == "stream_median"]
    assert {(labels["build"], labels["session_shape"], labels["streams"]) for labels in medians} == {
        (build, shape, streams)
        for build in ("8bit", "4bit")
        for shape in ("hands-on", "several-agents")
        for streams in ("1", "8", "class")
    } | {("8bit", "hands-on", "16"), ("4bit", "hands-on", "16")}
    assert {labels["lever"] for labels in medians} == {"baseline"}
    # Every quantity but the counts that are posted only above zero: these runs had no failed request.
    unposted = {*s5.FAILED_BY_CAUSE.values(), s5.Figure.FAILED_IN_STREAM, s5.Figure.FAILED_WITHOUT_CAUSE}
    assert {row["name"] for row in held["rows"]} == {
        *(figure.value for figure in s5.Figure if figure not in unposted),
        "class_size",
        "numbers_posted",
    }
