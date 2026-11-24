"""Spike S5's runs (`spikes/s5_capacity.py`): which runs the job makes from its inputs, how recorded
workspace answers read back as each instance's quantities, and what is computed from recorded
load-generator records, the failed requests among them. No number here is a measurement; the
instance names are made up, and the keys are generated.
"""

from __future__ import annotations

import json
import re
import urllib.error
from email.message import Message
from typing import TYPE_CHECKING

import httpx
import pytest

import common
import loadgen
import s5_capacity as s5
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_gateway import errors

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

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
    assert 'vllm:request_time_per_output_token_seconds_bucket{pool="coursework"}' in vllm[Key.TOKEN_TIME_P90]
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


def refuse_anchored(query: str) -> None:
    """Refuses, as Amazon Managed Prometheus does, a query that holds the `anchored` range modifier:
    an HTTP 400, which the workspace's client raises as urllib does."""
    if "anchored" in query:
        url = "https://workspace.example.com/api/v1/query"
        raise urllib.error.HTTPError(url, 400, "the anchored modifier is not enabled", Message(), None)


class Workspace:
    """Answers the release query with one instance in service the whole time, and nothing else."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        refuse_anchored(query)
        if not query.startswith("timestamp(rhi_lifecycle_release_info"):
            return []
        labels = {"instance": "instance-a", "instance_type": "g7e.2xlarge", "model_revision": "build-4bit"}
        return [common.Series(labels, ((start, start), (end, end)))]

    def read_samples(self, query: str, *, at: float) -> list[common.Series]:
        refuse_anchored(query)
        return []


def answered_as_the_gateway(request: httpx.Request) -> httpx.Response:
    """The gateway's answer to the numbered request of a run: an error of its own for three of them,
    rendered as it renders them (`rhi_gateway.errors`), and an answer for every other."""
    number, stream = json.loads(request.content)["request"], {"content-type": "text/event-stream"}
    if number in (10, 30):
        cause = Cause.ENGINE_ERROR if number == 10 else Cause.CAPACITY_UNAVAILABLE
        refused = errors.render_json(cause, errors.WireFormat.MESSAGES)
        return httpx.Response(refused.status, headers=refused.headers, content=refused.body)
    if number == 20:
        # Answered 200, then ended by the error event, as an engine restart ends a stream.
        ended = errors.render_stream_event(Cause.ENGINE_ERROR, errors.WireFormat.MESSAGES)
        return httpx.Response(200, headers=stream, content=ended)
    return httpx.Response(200, headers=stream, content=b"")


def test_a_run_posts_its_failed_requests_by_cause() -> None:
    clock = Clock()

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        # 100 requests, each a session of its own, read by the load generator itself.
        clock.now += 600
        sent = [
            loadgen.Request("POST", loadgen.MESSAGES_PATH, {}, json.dumps({"request": n}).encode()) for n in range(100)
        ]
        sessions = tuple(loadgen.RecordedSession((loadgen.Exchange(0, request),)) for request in sent)
        transport = httpx.MockTransport(answered_as_the_gateway)
        return await loadgen.replay(
            loadgen.Load(sessions, len(sessions)), keys, endpoint, at_once=4, transport=transport
        )

    job = s5.Job(
        pool="coursework", engine="vllm", prices={"g7e.2xlarge": 3.36}, lever="baseline", streams=(1,),
        class_size=1, duration=600, keys=[generate_key()], endpoint="https://gateway.example.com",
        gateway=Gateway(), workspace=Workspace(), replay=replay, wall=clock, sleep=clock.sleep,
    )  # fmt: skip
    run = job.run(job.runs[0])
    assert (run.experience.requests, run.experience.failed) == (100, 3)
    numbers = s5.numbers_of([run], 1)
    assert numbers["requests_hands_on_1"] == 100
    assert {name: value for name, value in numbers.items() if name.startswith("failed_")} == {
        "failed_requests_hands_on_1": 3,
        "failed_share_hands_on_1": 3,
        "failed_engine_error_hands_on_1": 2,
        "failed_in_stream_hands_on_1": 1,
        "failed_capacity_unavailable_hands_on_1": 1,
    }


def test_the_counts_by_cause_and_without_a_cause_add_up_to_the_failed_requests() -> None:
    def failure(status: int | None, cause: str | None = None) -> loadgen.Record:
        return loadgen.Record(0, 0, False, status, None, None, None, None, None, None, cause=cause)

    # One refusal for each cause of the gateway's, each with its status; and three that name none:
    # no answer at all, an answer of the edge, and a cause that is not of the vocabulary.
    named = [failure(cause.status, cause.value) for cause in Cause]
    unnamed = [failure(None), failure(403), failure(502, "another_cause")]
    seen = s5.experience_of([record(True, 1.0), *named, *unnamed, record(False, 1.0)])
    assert (seen.requests, seen.failed) == (16, 14)
    label = s5.Label("coursework", ("g7e.2xlarge",), "8bit", "baseline", s5.Shape.HANDS_ON, 1, 1, None)
    figures = s5.figures_of(s5.Measured(label, 600.0, seen, None))
    by_cause = {cause.value: figures[s5.Figure(f"failed_{cause.value}")] for cause in Cause}
    assert by_cause == dict.fromkeys(by_cause, 1) and len(by_cause) == 11
    assert figures[s5.Figure.FAILED_WITHOUT_CAUSE] == 3
    assert figures[s5.Figure.FAILED_REQUESTS] == 11 + 3
    # None was answered 200, and a run with no request has no share to post.
    assert figures[s5.Figure.FAILED_IN_STREAM] is None
    assert s5.figures_of(s5.Measured(label, 600.0, s5.experience_of([]), None))[s5.Figure.FAILED_SHARE] is None


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
    at = [END + n for n in (0, 15, 30, 45)]
    labels = {"instance": "instance-a", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    scraped = Scraped(
        ("rhi_lifecycle_release_info", labels, dict.fromkeys(at, 1.0)),
        ("vllm:generation_tokens_total", labels, dict(zip(at, (0.0, 500.0, 500.0, 500.0), strict=True))),
    )
    run = run_on(scraped, END + 5, END + 35)
    assert run.instances["instance-a"].engine.output_tokens_per_second == pytest.approx(500 / 30)


def test_a_pool_serving_two_builds_in_a_run_is_refused() -> None:
    def served(instance: str, build: str) -> common.Series:
        labels = {"instance": instance, "instance_type": "g7e.2xlarge", "model_revision": build}
        return common.Series(labels, ((END, END),))

    with pytest.raises(ValueError, match="2 builds"):
        s5.build_of(s5.in_service_of([served("instance-a", "8bit"), served("instance-b", "4bit")], END - 60, END))


class Scraped:
    """A workspace holding raw scrapes, `None` for a stale marker, which answers the job's queries as
    Prometheus would: a range query at each point of its grid, from the last sample at or before it,
    none after a stale marker; `last_over_time` from the last in its range; a range selector read at
    one time with the samples in its range, as scraped, its stale markers left out. It refuses the
    `anchored` modifier, and keeps every query it was asked."""

    def __init__(self, *series: tuple[str, dict[str, str], Mapping[float, float | None]]) -> None:
        self.series = series
        self.asked: list[str] = []

    def _held(self, query: str) -> list[tuple[str, dict[str, str], list[tuple[float, float | None]]]]:
        self.asked.append(query)
        refuse_anchored(query)
        return [
            (name, labels, sorted(samples.items())) for name, labels, samples in self.series if f"{name}{{" in query
        ]

    @staticmethod
    def _range(query: str) -> float:
        number, unit = re.findall(r"\[(\d+)(ms|s)\]", query)[-1]
        return float(number) / (1000 if unit == "ms" else 1)

    def read_samples(self, query: str, *, at: float) -> list[common.Series]:
        refuse_anchored(query)
        left = at - self._range(query)
        found = []
        for name, labels, samples in self._held(query):
            kept = tuple((t, value) for t, value in samples if left < t <= at and value is not None)
            if kept:
                found.append(common.Series({"__name__": name, **labels}, kept))
        return found

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        held = self._held(query)
        by: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = {}
        if "last_over_time" in query:
            left = end - self._range(query)
            lasts = [
                (labels, [v for at, v in samples if left < at <= end and v is not None][-1:])
                for _, labels, samples in held
            ]
            return [common.Series(labels, ((end, last[0]),)) for labels, last in lasts if last]
        points = [start + n * step for n in range(int((end - start) / step) + 1)]
        for _, labels, samples in held:
            for point in points:
                for at, value in [(at, value) for at, value in samples if point - 300 < at <= point][-1:]:
                    if value is not None:
                        by.setdefault(tuple(labels.items()), []).append((point, at if "timestamp(" in query else value))
        return [common.Series(dict(k), tuple(sorted(s))) for k, s in by.items()]


def test_a_short_run_reads_every_instance_scraped_during_it_and_the_scrape_after() -> None:
    # The run lasts from 1000 to 1004. The resident instance is scraped at 987, 1002 and 1017; one
    # that joined is first scraped at 1016, all it generated already counted; one of another build
    # is first scraped a whole scrape after the run's end, and served none of it.
    resident = {"instance": "resident", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    joined = {**resident, "instance": "joined"}
    later = {**resident, "instance": "later", "model_revision": "other-build"}
    scraped = Scraped(
        ("rhi_lifecycle_release_info", resident, {987.0: 1, 1002.0: 1, 1017.0: 1}),
        ("rhi_lifecycle_release_info", joined, {1016.0: 1}),
        ("rhi_lifecycle_release_info", later, {1019.0: 1}),
        ("rhi_lifecycle_engine_cache_capacity_tokens", resident, {987.0: 1_400_000, 1017.0: 1_400_000}),
        ("rhi_lifecycle_engine_cache_capacity_tokens", joined, {1016.0: 1_200_000}),
        ("rhi_lifecycle_engine_cache_capacity_tokens", later, {1019.0: 900_000}),
        ("vllm:generation_tokens_total", resident, {987.0: 100, 1002.0: 300, 1017.0: 300}),
        ("vllm:generation_tokens_total", joined, {1016.0: 500}),
        ("vllm:num_requests_running", resident, {987.0: 0, 1002.0: 2, 1017.0: 0}),
    )
    clock = Clock()
    clock.now = 1000.0

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        clock.now = 1004.0
        return [record(True, 1.0, output=700)]

    job = s5.Job(
        pool="coursework", engine="vllm", prices={"g7e.2xlarge": 3.36}, lever="baseline", streams=(1,),
        class_size=1, duration=4, keys=[generate_key()], endpoint="https://gateway.example.com",
        gateway=Gateway(), workspace=scraped, replay=replay, wall=clock, sleep=lambda _: None,
    )  # fmt: skip
    run = job.run(job.runs[0])
    assert run.label.build == "build"
    assert sorted(run.instances) == ["joined", "resident"]
    there, new = run.instances["resident"], run.instances["joined"]
    assert (there.seconds_in_service, new.seconds_in_service) == (4.0, 3.0)
    assert run.cost_per_million_output_tokens == pytest.approx(3.36 * 7 / 3600 / 700 * 1_000_000)
    assert (there.engine.cache_capacity_tokens, new.engine.cache_capacity_tokens) == (1_400_000, 1_200_000)
    assert there.engine.output_tokens_per_second == pytest.approx(200 / 4)
    assert new.engine.output_tokens_per_second == pytest.approx(500 / 4)
    assert there.engine.running_requests_peak == 2


@pytest.mark.parametrize("baseline_at", [990.0, 1000.0])
def test_a_run_between_whole_seconds_counts_only_the_output_generated_in_it(baseline_at: float) -> None:
    # The run lasts from 1000.25 to 1030.75 and generates 500 tokens; the counter held 1,000 at the
    # last scrape before the run, at 990 or at 1000, and 600 at the one before that.
    at = [baseline_at + 15 * n for n in range(-1, 4)]
    labels = {"instance": "worker", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    scraped = Scraped(
        ("rhi_lifecycle_release_info", labels, dict.fromkeys(at, 1.0)),
        ("vllm:generation_tokens_total", labels, dict(zip(at, (600.0, 1000.0, 1500.0, 1500.0, 1500.0), strict=True))),
    )
    clock = Clock()
    clock.now = 1000.25

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        clock.now = 1030.75
        return [record(True, 1.0)]

    job = s5.Job(
        pool="coursework", engine="vllm", prices={"g7e.2xlarge": 3.36}, lever="baseline", streams=(1,),
        class_size=1, duration=30, keys=[generate_key()], endpoint="https://gateway.example.com",
        gateway=Gateway(), workspace=scraped, replay=replay, wall=clock, sleep=lambda _: None,
    )  # fmt: skip
    run = job.run(job.runs[0])
    assert run.instances["worker"].engine.output_tokens_per_second == pytest.approx(500 / 30.5)


def test_a_warm_instance_is_not_charged_while_stopped_between_two_periods_of_service() -> None:
    labels = {"instance": "worker", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    # Four scrapes in service, stopped for 13 minutes, then four more.
    taken = (1000.0, 1015.0, 1030.0, 1045.0, 1855.0, 1870.0, 1885.0, 1900.0)
    in_service = s5.in_service_of([common.Series(labels, tuple((t, t) for t in taken))], 1000.0, 1900.0)
    # A scrape either side of each period's samples, inside the window: 60 seconds each.
    assert in_service["worker"].seconds == 120.0
    cost = s5.cost_per_million(in_service, {"g7e.2xlarge": 3.36}, 500)
    assert cost == pytest.approx(3.36 * 120 / 3600 / 500 * 1_000_000)


def test_a_counter_first_scraped_just_before_the_run_is_its_baseline() -> None:
    # The worker's first scrape, at 1000, is a quarter second before the run starts: what its
    # counter held then is the baseline, not output of the run, which generates 500 tokens.
    labels = {"instance": "worker", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    at = (1000.0, 1015.0, 1030.0, 1045.0)
    scraped = Scraped(
        ("rhi_lifecycle_release_info", labels, dict.fromkeys(at, 1.0)),
        ("vllm:generation_tokens_total", labels, dict(zip(at, (100.0, 600.0, 600.0, 600.0), strict=True))),
    )
    clock = Clock()
    clock.now = 1000.25

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        clock.now = 1030.75
        return [record(True, 1.0)]

    job = s5.Job(
        pool="coursework", engine="vllm", prices={"g7e.2xlarge": 3.36}, lever="baseline", streams=(1,),
        class_size=1, duration=30, keys=[generate_key()], endpoint="https://gateway.example.com",
        gateway=Gateway(), workspace=scraped, replay=replay, wall=clock, sleep=lambda _: None,
    )  # fmt: skip
    run = job.run(job.runs[0])
    assert run.instances["worker"].engine.output_tokens_per_second == pytest.approx(500 / 30.5)


def test_a_release_reported_only_after_the_run_does_not_label_it() -> None:
    # The worker serves build A throughout; its first sample of build B is a whole scrape after the
    # run's end.
    serving = {"instance": "worker", "instance_type": "g7e.2xlarge", "model_revision": "build-a"}
    series = [
        common.Series(serving, ((1000.0, 1000.0), (1015.0, 1015.0), (1030.0, 1030.0))),
        common.Series({**serving, "model_revision": "build-b"}, ((1045.0, 1045.0),)),
    ]
    in_service = s5.in_service_of(series, 1000.0, 1030.0)
    assert s5.build_of(in_service) == "build-a"
    assert in_service["worker"].seconds == 30.0


def test_a_counter_that_went_stale_before_the_run_and_resumed_in_it_counts_only_the_runs_output() -> None:
    # The worker's counter held 1,000, went stale at END + 15, and its engine resumed in the run, from
    # END + 15.25 to END + 45.75, generating 500 tokens. A plain selector shows nothing after the stale
    # marker; a range one leaves it out, so the increase starts from 1,000 and sees the reset.
    labels = {"instance": "worker", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    scraped = Scraped(
        ("rhi_lifecycle_release_info", labels, {END + n: 1.0 for n in (0, 15, 30, 45, 60)}),
        ("vllm:generation_tokens_total", labels, {END: 1000.0, END + 15: None, END + 30: 500.0, END + 45: 500.0}),
    )
    run = run_on(scraped, END + 15.25, END + 45.75)
    assert run.instances["worker"].engine.output_tokens_per_second == pytest.approx(500 / 30.5)


def test_each_engine_counter_uses_its_own_baseline_before_the_instance_sum() -> None:
    resident = {"__name__": "vllm:generation_tokens_total", "instance": "worker", "engine": "0"}
    # Engine 0 held 1,000 tokens at the start and added 200. Engine 1 emitted its first 500 tokens
    # before its first scrape and added 50 after it; a series with one label more is another series,
    # and the same.
    samples = [
        common.Series(resident, ((985.0, 900.0), (1000.0, 1000.0), (1015.0, 1200.0), (1030.0, 1200.0))),
        common.Series({**resident, "engine": "1"}, ((1015.0, 500.0), (1030.0, 550.0))),
        common.Series({**resident, "zone": "example-zone"}, ((1015.0, 500.0), (1030.0, 550.0))),
    ]
    assert s5.counted_of(samples, 1000.0)[("worker", "")] == 1300.0


def run_on(workspace: s5.Reader, start: float, end: float) -> s5.Measured:
    """One run of the job on a vllm pool from `start` to `end`, read from `workspace`."""
    clock = Clock()
    clock.now = start

    async def replay(load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int) -> list[loadgen.Record]:
        clock.now = end
        return [record(True, 1.0)]

    job = s5.Job(
        pool="coursework", engine="vllm", prices={"g7e.2xlarge": 3.36}, lever="baseline", streams=(1,),
        class_size=1, duration=end - start, keys=[generate_key()], endpoint="https://gateway.example.com",
        gateway=Gateway(), workspace=workspace, replay=replay, wall=clock, sleep=lambda _: None,
    )  # fmt: skip
    return job.run(job.runs[0])


def scrapes(
    base: dict[str, str], instance: str, values: Mapping[float, float]
) -> list[tuple[str, dict[str, str], Mapping[float, float]]]:
    """An instance's release, and every counter and histogram a vllm pool is read by, all holding `values`."""
    labels = {**base, "instance": instance}
    names = [one.query.split("{")[0] for one in s5.reads("coursework", "vllm", 1.0).values() if one.how is s5.How.SUM]
    held = [("rhi_lifecycle_release_info", labels, dict.fromkeys(values, 1.0))]
    return held + [(name, {**labels, "le": "+Inf"} if name.endswith("_bucket") else labels, values) for name in names]


def test_a_counter_reset_and_a_series_born_in_the_run_count_as_the_anchored_increase_did() -> None:
    # The run lasts from 1000.25 to 1045.75; scrapes every 15 seconds from 990. The resident
    # instance's counters held 1,000 before the run, rose to 1,300, restarted with their engine and
    # were scraped again at 200 a tenth of a second later, then reached 400: 300 + 200 + 200. The
    # joined instance's were first scraped in the run, at 500, then 550: all of it is the run's.
    base = {"instance_type": "g7e.2xlarge", "model_revision": "build"}
    at = (990.0, 1005.0, 1005.1, 1020.0, 1050.0)
    resident = dict(zip(at, (1000.0, 1300.0, 200.0, 400.0, 400.0), strict=True))
    scraped = Scraped(*scrapes(base, "resident", resident), *scrapes(base, "joined", {1035.0: 500.0, 1050.0: 550.0}))
    run = run_on(scraped, 1000.25, 1045.75)
    there, new = run.instances["resident"].engine, run.instances["joined"].engine
    assert (there.output_tokens_per_second, new.output_tokens_per_second) == (700 / 45.5, 550 / 45.5)
    assert (there.first_token_requests, new.first_token_requests) == (700, 550)
    assert (there.preemptions, new.preemptions) == (700, 550)
    assert there.input_tokens_per_request == {"+Inf": 700}


def test_a_counter_scraped_only_before_the_run_counts_nothing_not_zero() -> None:
    # The worker serves the whole run, but its preemption counter was last scraped before it.
    labels = {"instance": "worker", "instance_type": "g7e.2xlarge", "model_revision": "build"}
    scraped = Scraped(
        ("rhi_lifecycle_release_info", labels, {990.0: 1.0, 1005.0: 1.0, 1020.0: 1.0}),
        ("vllm:num_preemptions_total", labels, {990.0: 7.0}),
    )
    assert run_on(scraped, 1000.0, 1010.0).instances["worker"].engine.preemptions is None


def test_the_job_sends_the_workspace_no_query_with_the_anchored_modifier() -> None:
    # The stand-in refuses one, as Amazon Managed Prometheus does; a run on a vllm pool reads every
    # quantity there is.
    for refusing in (
        lambda query: Scraped().read_series(query, start=END, end=END, step=1.0),
        lambda query: Scraped().read_samples(query, at=END),
    ):
        with pytest.raises(urllib.error.HTTPError) as refused:
            refusing("sum(increase(x[1m] anchored))")
        assert refused.value.code == 400
    base = {"instance_type": "g7e.2xlarge", "model_revision": "build"}
    scraped = Scraped(*scrapes(base, "worker", {990.0: 0.0, 1005.0: 10.0, 1020.0: 20.0}))
    run_on(scraped, 1000.0, 1010.0)
    every = {one.query for one in s5.reads("coursework", "vllm", 10.0).values() if one.how is not s5.How.LAST}
    assert all(any(asked.startswith(query) for asked in scraped.asked) for query in every)
    assert not [query for query in scraped.asked if "anchored" in query]
