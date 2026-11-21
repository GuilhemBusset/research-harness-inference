"""Spike S5's runs (`spikes/s5_capacity.py`): which runs the job makes from its inputs, how recorded
workspace answers read back as each instance's quantities, and what is computed from recorded
load-generator records. No number here is a measurement; the instance names are made up, and the
keys are generated.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import common
import loadgen
import s5_capacity as s5
from rhi_common.keys import generate_key

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

Key = s5.Key
END = 10_000.0


def answer(*rows: tuple[dict[str, str], float]) -> list[common.Series]:
    """A recorded answer of the workspace to a query evaluated at `END`: one series a row."""
    result = [{"metric": labels, "values": [[END, repr(value)]]} for labels, value in rows]
    return common.series_of(json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}}))


def a(value: float, b: float) -> list[common.Series]:
    return answer(({"instance": "instance-a"}, value), ({"instance": "instance-b"}, b))


def record(first_turn: bool, ttft: float, speed: float | None = 20.0, output: int = 500) -> loadgen.Record:
    return loadgen.Record(0, 0, first_turn, 200, ttft, ttft, speed, 60_000, 50_000, output)


def test_recorded_workspace_answers_read_back_as_each_instances_quantities() -> None:
    window = 600.0
    vllm = s5.queries("coursework", "vllm", window)
    # The names of the per-engine table, over the run's window, for the pool.
    assert 'vllm:request_time_per_output_token_seconds_bucket{pool="coursework"}[600s]' in vllm[Key.TOKEN_TIME_P90]
    assert "vllm:generation_tokens_total" in vllm[Key.OUTPUT_TOKENS]
    assert "vllm:request_prefill_kv_computed_tokens_sum" in vllm[Key.PREFILL_COMPUTED_TOKENS]
    assert "vllm:request_generation_tokens_sum" in vllm[Key.REQUEST_OUTPUT_TOKENS]
    assert "rhi_lifecycle_engine_cache_capacity_tokens" in vllm[Key.CACHE_CAPACITY]
    # The mock's row has none of the further quantities: they are not asked for, and read None.
    assert set(vllm) - set(s5.queries("coursework", "mock", window)) == {
        Key.REQUEST_OUTPUT_TOKENS, Key.DECODE_SECONDS, Key.PREFILL_COMPUTED_TOKENS, Key.PREFILL_SECONDS,
        Key.PREEMPTIONS, Key.INPUT_DISTRIBUTION, Key.OUTPUT_DISTRIBUTION,
    }  # fmt: skip

    answers = {
        Key.OUTPUT_TOKENS: a(90_000, 60_000),
        Key.INPUT_TOKENS: a(10_000_000, 8_000_000),
        Key.CACHED_INPUT_TOKENS: a(9_000_000, 4_000_000),
        Key.FIRST_TOKEN_REQUESTS: a(180, 120),
        Key.FIRST_TOKEN_MEDIAN: a(0.8, 1.2),
        Key.FIRST_TOKEN_P95: a(3.5, float("nan")),
        Key.TOKEN_TIME_MEDIAN: a(0.05, 0.04),
        Key.TOKEN_TIME_P90: a(0.08, 0.1),
        Key.RUNNING_MEAN: a(7.5, 5.0),
        Key.RUNNING_PEAK: a(8, 6),
        Key.WAITING_MEAN: a(0.5, 0),
        Key.WAITING_PEAK: a(2, 0),
        Key.CACHE_USE_MEAN: a(0.6, 0.4),
        Key.CACHE_USE_PEAK: a(0.9, 0.5),
        Key.CACHE_CAPACITY: a(1_400_000, 1_400_000),
        Key.REQUEST_OUTPUT_TOKENS: a(90_000, 60_000),
        Key.DECODE_SECONDS: a(4_500, 2_000),
        Key.PREFILL_COMPUTED_TOKENS: a(1_000_000, 0),
        Key.PREFILL_SECONDS: a(200, 0),
        Key.PREEMPTIONS: a(0, 3),
        Key.INPUT_DISTRIBUTION: answer(
            ({"instance": "instance-a", "le": "65536"}, 20), ({"instance": "instance-a", "le": "+Inf"}, 180)
        ),
    }
    read = s5.quantities_of(answers, window)
    one, other = read["instance-a"], read["instance-b"]
    assert one.output_tokens_per_second == 150.0
    assert other.output_tokens_per_second == 100.0
    assert one.output_tokens_per_second_per_stream_mean == 20.0
    assert one.output_tokens_per_second_per_stream_median == pytest.approx(20.0)
    assert one.output_tokens_per_second_per_stream_slowest_tenth == pytest.approx(12.5)
    assert other.output_tokens_per_second_per_stream_slowest_tenth == pytest.approx(10.0)
    assert one.prefill_tokens_per_second == 5_000.0
    assert other.prefill_tokens_per_second is None
    assert (one.first_token_seconds_median, one.first_token_seconds_p95, one.first_token_requests) == (0.8, 3.5, 180)
    assert other.first_token_seconds_p95 is None
    assert (one.cached_share, other.cached_share) == (0.9, 0.5)
    assert (one.cache_use_mean, one.cache_use_peak, one.cache_capacity_tokens) == (0.6, 0.9, 1_400_000)
    assert (one.preemptions, other.preemptions) == (0, 3)
    assert (one.running_requests_mean, one.running_requests_peak) == (7.5, 8)
    assert (one.waiting_requests_mean, one.waiting_requests_peak) == (0.5, 2)
    assert one.input_tokens_per_request == {"65536": 20, "+Inf": 180}
    assert other.input_tokens_per_request is None
    assert one.output_tokens_per_request is None


def test_a_first_turn_of_8_seconds_is_left_out_of_the_95th_percentile_of_later_turns() -> None:
    later = [record(False, ttft) for ttft in (0.8, 0.9, 1.0, 1.1, 1.2, 1.0, 0.9, 1.0, 1.1, 1.0)]
    seen = s5.experience_of([record(True, 8.0), *later, record(False, 1.0, speed=None)])
    assert seen.first_turns == 1
    assert seen.later_turns == 11
    assert seen.first_token_seconds_p95 is not None
    assert seen.first_token_seconds_p95 < 1.2
    assert seen.first_turn_first_token_seconds_p95 == 8.0
    speeds = s5.experience_of([record(False, 1.0, speed) for speed in range(10, 30)])
    assert speeds.tokens_per_second_median == pytest.approx(19.5)
    assert speeds.tokens_per_second_slowest_tenth == pytest.approx(11.9)


def test_the_cost_per_million_output_tokens_at_3_36_an_hour_matches_the_recorded_throughput() -> None:
    start, end = END - 600, END
    # 150 output tokens a second for the 600 seconds of the run.
    records = [record(False, 1.0, output=500) for _ in range(180)]
    tokens = s5.experience_of(records).output_tokens
    throughput = tokens / (end - start)
    whole = [(t, t) for t in range(int(start), int(end), 15)]
    release = common.series_of(
        json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"instance": "instance-a", "instance_type": "g7e.2xlarge", "model_revision": "r"},
                            "values": [[at, repr(float(taken))] for at, taken in whole],
                        }
                    ],
                },
            }
        )
    )
    in_service = s5.in_service_of(release, start, end)
    assert in_service["instance-a"].seconds == 600.0
    cost = s5.cost_per_million(in_service, {"g7e.2xlarge": 3.36}, tokens)
    assert cost == pytest.approx(3.36 / (throughput * 3600) * 1_000_000)
    # An instance that joined halfway through is charged for the half it ran, at its type's price.
    joined = s5.InService("g7e.4xlarge", "r", 300.0)
    both = s5.cost_per_million({**in_service, "instance-b": joined}, {"g7e.2xlarge": 3.36, "g7e.4xlarge": 4.0}, tokens)
    assert both == pytest.approx((3.36 * 600 + 4.0 * 300) / 3600 / tokens * 1_000_000)


class Gateway:
    def __init__(self) -> None:
        self.woken = 0

    def state(self) -> str:
        return "ready" if self.woken else "cold"

    def wake(self) -> None:
        self.woken += 1


class Clock:
    def __init__(self) -> None:
        self.now = END

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Workspace:
    """Answers the release query with one instance in service the whole time, and nothing else."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if not query.startswith("timestamp(rhi_lifecycle_release_info"):
            return []
        labels = {"instance": "instance-a", "instance_type": "g7e.2xlarge", "model_revision": "build-4bit"}
        return [common.Series(labels, ((start, start), (end, end)))]


def test_the_runs_follow_the_inputs() -> None:
    clock = Clock()

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        # Every session at once, in step: each turn takes its pause, then 30 seconds to answer.
        assert at_once == len(load.sessions)
        sessions = [session.requests() for session in load.sessions]
        steps = [next(session, None) for session in sessions]
        records, turn = [], 0
        while any(steps):
            clock.now += max(step[0] for step in steps if step) + 30
            for number, step in enumerate(steps):
                if step is None:
                    continue
                student = number // load.sessions_per_student
                records.append(loadgen.Record(student, number, turn == 0, 200, 1.0, 1.0, 20.0, 1000, 900, 500))
                steps[number] = next_step(sessions[number])
            turn += 1
        runs.append((load.students, {keys[r.student] for r in records}))
        return records

    def next_step(session: Generator[tuple[float, loadgen.Request], str]) -> tuple[float, loadgen.Request] | None:
        try:
            return session.send("answer")
        except StopIteration:
            return None

    runs: list[tuple[int, set[str]]] = []
    job = s5.Job(
        pool="upgrade",
        engine="vllm",
        prices={"g7e.2xlarge": 3.36},
        lever="draft-head",
        streams=(1, 8, 32),
        class_size=20,
        duration=600,
        keys=[generate_key() for _ in range(32)],
        endpoint="https://gateway.example.com",
        gateway=Gateway(),
        workspace=Workspace(),
        replay=replay,
        wall=clock,
        sleep=clock.sleep,
        log=lambda _: None,
    )
    measured = job.measure()
    labels = [one.label for one in measured]
    given = [(label.shape, label.class_size or label.streams) for label in labels]
    assert sorted(given) == sorted((shape, count) for shape in s5.Shape for count in (1, 8, 32, 20))
    assert {(label.pool, label.build, label.lever, label.instance_types) for label in labels} == {
        ("upgrade", "build-4bit", "draft-head", ("g7e.2xlarge",))
    }
    by = {(label.shape, label.class_size or label.streams): label for label in labels}
    # At a number of streams, that many sessions at once; at the class size, every student at once.
    assert (by[s5.Shape.HANDS_ON, 32].streams, by[s5.Shape.HANDS_ON, 32].students) == (32, 32)
    assert (by[s5.Shape.SEVERAL_AGENTS, 8].streams, by[s5.Shape.SEVERAL_AGENTS, 8].students) == (8, 4)
    assert (by[s5.Shape.HANDS_ON, 20].streams, by[s5.Shape.HANDS_ON, 20].students) == (20, 20)
    assert (by[s5.Shape.SEVERAL_AGENTS, 20].streams, by[s5.Shape.SEVERAL_AGENTS, 20].students) == (40, 20)
    # Each student its own canary key; no request sent once a run's duration had passed, and each
    # run lasted the duration it was given and its last answers.
    assert all(len(keys) == students for students, keys in runs)
    assert all(600 <= one.seconds <= 600 + 30 for one in measured)
    assert all(one.experience.first_turns >= one.label.streams for one in measured)
    assert all(one.cost_per_million_output_tokens is not None for one in measured)


def test_output_before_the_first_scrape_in_a_run_counts_toward_its_throughput() -> None:
    # Scrapes every 15 seconds from END; the run lasts from END + 5 to END + 35, and its one answer,
    # 500 tokens, is in the first scrape within it. The scrape before the run holds the baseline.
    counter = [(END + at, 500.0 if at else 0.0) for at in (0, 15, 30, 45)]
    clock = Clock()
    clock.now = END + 5

    class Counting(Workspace):
        """Answers the release query, and the output tokens' increase over a range as Prometheus
        does with `anchored`: from the last sample at or before the range's start."""

        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            if "vllm:generation_tokens_total" not in query:
                return super().read_series(query, start=start, end=end, step=step)
            if "anchored" not in query:
                return []
            left = end - float(query.split("[")[1].split("s]")[0])
            seen = [sample for sample in counter if sample[0] <= end]
            before = [sample for sample in seen if sample[0] <= left][-1:]
            first = (before or seen)[0][1]
            return [common.Series({"instance": "instance-a"}, ((end, seen[-1][1] - first),))]

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        clock.now = END + 35
        return [record(True, 1.0)]

    job = s5.Job(
        pool="coursework", engine="vllm", prices={"g7e.2xlarge": 3.36}, lever="baseline", streams=(1,),
        class_size=1, duration=30, keys=[generate_key()], endpoint="https://gateway.example.com",
        gateway=Gateway(), workspace=Counting(), replay=replay, wall=clock, sleep=lambda _: None,
    )  # fmt: skip
    run = job.run(job.runs[0])
    assert run.instances["instance-a"].engine.output_tokens_per_second == pytest.approx(500 / 30)


def test_a_pool_serving_two_builds_in_a_run_is_refused() -> None:
    def served(instance: str, build: str) -> common.Series:
        labels = {"instance": instance, "instance_type": "g7e.2xlarge", "model_revision": build}
        return common.Series(labels, ((END, END),))

    with pytest.raises(ValueError, match="2 builds"):
        s5.build_of(s5.in_service_of([served("instance-a", "8bit"), served("instance-b", "4bit")], END - 60, END))
