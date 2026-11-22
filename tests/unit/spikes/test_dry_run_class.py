"""The dry-run class (`spikes/dry_run_class.py`): its steps against stubbed clients, on a clock the test
controls. The gateway, its status and the workspace are stubs; the instance IDs and the keys are
drawn while the tests run."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import secrets
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import common
import dry_run_class as dry
import loadgen
from rhi_common.causes import Cause
from rhi_common.keys import generate_key
from rhi_gateway.errors import WireFormat, render_stream_event
from rhi_gateway.hold import HELD_HEADER
from s5_capacity import Shape
from s7_drain import Recorder

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

START = 1_800_000_000.0
# Turns of the event loop that let every task of a run go on until it waits on the clock.
YIELDS = 50
STAGES = {"boot": 20.0, "image": 1.5, "release-check": 0.5, "container-start": 2.0, "weights": 70.0, "hook": 1.0}
# The stages a launch has ended by the time its engine starts loading the weights.
EARLY = ("boot", "image", "release-check", "container-start")


class Clock:
    """A clock the test moves: `drive` runs a coroutine, and each time every task of it waits on
    the clock, moves the time to the earliest wait's end and ends that wait."""

    def __init__(self) -> None:
        self.at = START
        self._waits: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def now(self) -> float:
        return self.at

    async def sleep(self, seconds: float) -> None:
        wait = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waits, (self.at + max(seconds, 0.0), next(self._order), wait))
        await wait

    async def drive(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(work)
        while not task.done():
            # Every task runs until it waits: nothing here waits on anything but the clock.
            for _ in range(YIELDS):
                await asyncio.sleep(0)
            if task.done():
                break
            assert self._waits, "a task waits on something other than the clock"
            ends, _, wait = heapq.heappop(self._waits)
            self.at = max(self.at, ends)
            wait.set_result(None)
        task.result()


class Gateway:
    """The gateway's status and wake: cold until woken, then each state of `after_wake` in turn, then ready."""

    def __init__(self, after_wake: list[str]) -> None:
        self.woken = False
        self.after_wake = list(after_wake)

    def state(self) -> str:
        if not self.woken:
            return "cold"
        return self.after_wake.pop(0) if self.after_wake else "ready"

    def wake(self) -> None:
        self.woken = True


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _stream(*, ping: bool) -> bytes:
    # The last usage object repeats the cached input as null: the count reported first stands.
    first = {"input_tokens": 10, "cache_read_input_tokens": 30, "output_tokens": 1}
    usage = {"cache_read_input_tokens": None, "output_tokens": 3}
    events = [
        _event("message_start", {"type": "message_start", "message": {"usage": first}}),
        *(
            _event(
                "content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word}}
            )
            for word in ("one ", "two ", "six ")
        ),
        _event("message_delta", {"type": "message_delta", "usage": usage}),
        _event("message_stop", {"type": "message_stop"}),
    ]
    return (_event("ping", {"type": "ping"}) if ping else b"") + b"".join(events)


class Pool(httpx.AsyncBaseTransport):
    """The gateway's answers by student: the first held, with a keep-alive first on its first request
    only, the second warm, its engine opening each stream with a comment, with its third request cut,
    the third refused for its budget."""

    def __init__(self, keys: list[str], clock: Clock) -> None:
        self.keys, self.clock, self.sent = keys, clock, 0
        self.by_student = [0, 0, 0]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        student = self.keys.index(request.headers["authorization"].removeprefix("Bearer "))
        self.by_student[student] += 1
        if student == 2:
            body = {"type": "error", "error": {"type": "budget_reached", "message": "spent"}}
            return httpx.Response(402, json=body)
        if student == 1 and self.by_student[1] == 3:
            raise httpx.ReadError("cut")
        stream, clock = _stream(ping=student == 0 and self.by_student[0] == 1), self.clock
        if student == 1:
            stream = b": engine\n\n" + stream
        headers = {"content-type": "text/event-stream", **({HELD_HEADER: "true"} if student == 0 else {})}

        async def paced() -> Any:
            for event in stream.split(b"\n\n")[:-1]:
                await clock.sleep(0.25)
                yield event + b"\n\n"

        return httpx.Response(200, headers=headers, content=paced())


def _series(labels: dict[str, str], *samples: tuple[float, float]) -> common.Series:
    return common.Series(labels, samples)


class Workspace:
    """The workspace, answering each query as it stood at the query's end: two instances in service,
    the second's launch inside the hour, a third's launch after it. The first, from before the hour,
    still exports its stages. The second's launch is taken for a promotion, then moved to a cold
    launch, and ends its last stages after the hour. A counter's value is the second it is read at."""

    def __init__(self, before: str, during: str, after: str) -> None:
        self.before, self.during, self.after = before, during, after
        self.queries: list[str] = []

    def _stages(self, start: float, end: float) -> list[common.Series]:
        launched = {stage: seconds + 3 for stage, seconds in STAGES.items()}
        rows = [({"instance": self.before, "transition": "promotion"}, STAGES, START - 600)]
        rows.append(({"instance": self.during, "transition": "promotion"}, {"boot": launched["boot"]}, START + 45))
        cold = {"instance": self.during, "transition": "cold-launch"}
        rows.append((cold, {stage: launched[stage] for stage in EARLY}, START + 60))
        rows.append((cold, {stage: launched[stage] for stage in launched if stage not in EARLY}, START + 135))
        # The third's scrape that lacks its image places its launch after the hour.
        rows.append(({"instance": self.after, "transition": "promotion"}, {"boot": 10.0}, START + 105))
        rows.append(({"instance": self.after, "transition": "promotion"}, {"image": 1.5}, START + 120))
        series = []
        for labels, stages, first in rows:
            # A promotion's labels last until the launch turns out to be a cold one.
            last = START + 45 if labels == {"instance": self.during, "transition": "promotion"} else end
            at = [at for at in range(int(START) - 600, int(end) + 1, 15) if max(start, first) <= at <= min(end, last)]
            series += [
                _series({**labels, "stage": stage}, *((t, seconds) for t in at)) for stage, seconds in stages.items()
            ]
        return [one for one in series if one.samples]

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        self.queries.append(query)
        both = [self.before, self.during] if end > START + 45 else [self.before]
        if dry.STAGE_METRIC in query:
            # These stubs' samples are scraped on the query's grid: each step's sample was taken then.
            stages = self._stages(start, end)
            if query.startswith("timestamp("):
                return [_series(dict(one.labels), *((t, t) for t, _ in one.samples)) for one in stages]
            return stages
        if "timestamp(rhi_lifecycle_release_info" in query:
            return [_series({"instance": one}, (end, end - 5)) for one in both]
        if dry.INSTANCES_IN_SERVICE in query:
            return [_series({}, (end, float(len(both))))]
        if dry.CACHE_CAPACITY_METRIC in query:
            return [_series({"instance": self.before}, (end, 250_000.0))]
        return [_series({"instance": one}, (end, end - START)) for one in both]


def _conversation(seed: int) -> loadgen.Session:
    turns = (loadgen.Turn(5.0, 4), loadgen.Turn(5.0, 4))
    return loadgen.SyntheticSession(dry.POOL, 8, turns, 16, seed)


def _rehearsal(
    lines: list[str], *, after_wake: list[str] | None = None, shape: Shape = Shape.HANDS_ON
) -> tuple[dry.Rehearsal, list[str], list[str]]:
    clock = Clock()
    keys = [generate_key() for _ in range(3)]
    instances = [f"i-{secrets.token_hex(8)}" for _ in range(3)]

    async def inline(action: Callable[[], Any]) -> Any:
        return action()

    rehearsal = dry.Rehearsal(
        engine="vllm",
        class_size=3,
        shape=shape,
        keys=keys,
        endpoint="https://gateway.example.com",
        gateway=Gateway(["warming"] if after_wake is None else after_wake),
        workspace=Workspace(*instances),
        recorder=Recorder(clock, lines.append),
        clock=clock,
        conversation=_conversation,
        duration=60.0,
        transport=Pool(keys, clock),
        offload=inline,
    )
    return rehearsal, keys, instances


def _run(rehearsal: dry.Rehearsal) -> None:
    clock = rehearsal.clock
    assert isinstance(clock, Clock)
    asyncio.run(clock.drive(rehearsal.run()))


def _kinds(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines]


def test_each_step_writes_lines_with_a_time_and_a_kind_and_no_identifier_or_key() -> None:
    lines: list[str] = []
    rehearsal, keys, instances = _rehearsal(lines)
    _run(rehearsal)

    written = _kinds(lines)
    assert all(isinstance(line, dict) and line["at"].endswith("Z") and line["kind"] for line in written)
    kinds = {line["kind"] for line in written}
    assert {"start", "pool_ready", "hour_started", "engine_counters", "cache_capacity", "in_service"} <= kinds
    assert {"request", "hour_ended", "instance_started", "end"} <= kinds
    assert not any(secret in line for line in lines for secret in (*keys, *instances))

    (start,) = [line for line in written if line["kind"] == "start"]
    assert (start["class_size"], start["shape"], start["pool"]) == (3, "hands-on", "coursework")
    assert [line["woke"] for line in written if line["kind"] == "pool_ready"] == [True]


def test_each_request_line_holds_its_times_tokens_hold_and_first_turn() -> None:
    lines: list[str] = []
    rehearsal, _, _ = _rehearsal(lines)
    _run(rehearsal)
    requests = [line for line in _kinds(lines) if line["kind"] == "request"]

    answered = [line for line in requests if line["status"] == 200 and line["failure"] is None]
    assert answered
    for line in answered:
        assert line["sent_at"] < line["first_token_at"] <= line["last_token_at"]
        assert (line["input_tokens"], line["cached_input_tokens"], line["output_tokens"]) == (40, 30, 3)
        assert isinstance(line["first_turn"], bool)
    # Each session of the hour goes on with new conversations: both kinds of turn are there.
    assert {line["first_turn"] for line in answered} == {True, False}
    assert {line["conversation"] for line in answered if line["session"] == 0} >= {0, 1, 2}
    # The gateway said it held the first student's requests, whether a keep-alive came or not; the
    # warm student's answers did not say so, though their engine opened each stream with a comment.
    assert all(line["held"] for line in answered if line["session"] == 0)
    assert not any(line["held"] for line in answered if line["session"] == 1)

    refused = [line for line in requests if line["session"] == 2]
    assert refused
    assert all((line["status"], line["cause"], line["held"]) == (402, "budget_reached", False) for line in refused)
    # The cut request is a line of its own, and the session goes on after it.
    cut = [line for line in requests if line["failure"] is not None]
    assert [(line["session"], line["failure"], line["status"]) for line in cut] == [(1, "read_error", None)]
    assert any(line["sent_at"] > cut[0]["sent_at"] for line in requests if line["session"] == 1)
    # No request was sent once the hour had passed.
    hour = next(line for line in _kinds(lines) if line["kind"] == "hour_started")
    assert all(line["sent_at"] < hour["ends_at"] for line in requests)


@pytest.mark.parametrize("cut", [True, False], ids=["cut-after-headers", "error-event"])
def test_an_answer_that_never_came_ends_its_conversation(cut: bool) -> None:
    lines: list[str] = []
    rehearsal, _, _ = _rehearsal(lines, after_wake=[])
    rehearsal.class_size = 1

    async def broken() -> Any:
        yield _event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}})
        if cut:
            raise httpx.ReadError("cut")
        yield render_stream_event(Cause.ENGINE_ERROR, WireFormat.MESSAGES)

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=broken())

    rehearsal.transport = httpx.MockTransport(answer)
    _run(rehearsal)
    requests = [line for line in _kinds(lines) if line["kind"] == "request"]
    assert len(requests) > 1
    # No answer to grow the transcript by: each next request opens a new conversation.
    assert all(line["first_turn"] for line in requests)
    assert {line["failure"] or line["cause"] for line in requests} == {"read_error" if cut else "engine_error"}


def test_the_readings_name_instances_and_tell_a_launch_in_the_hour_from_one_before_it() -> None:
    lines: list[str] = []
    rehearsal, _, _ = _rehearsal(lines)
    _run(rehearsal)
    written = _kinds(lines)

    hour = next(line for line in written if line["kind"] == "hour_started")
    counters = {line["moment"]: line for line in written if line["kind"] == "engine_counters"}
    assert set(counters["start"]["instances"]) == {"instance-1"}
    assert set(counters["end"]["instances"]) == {"instance-1", "instance-2"}
    assert {"input_tokens", "cached_input_tokens", "preemptions"} <= set(counters["end"]["instances"]["instance-2"])
    # The counters are as they stood at the hour's two bounds, whenever they were read.
    for moment, bound in (("start", "started_at"), ("end", "ends_at")):
        assert counters[moment]["read_at"] == hour[bound]
        assert dry.utc(START + counters[moment]["instances"]["instance-1"]["input_tokens"]) == hour[bound]
    (capacity,) = [line for line in written if line["kind"] == "cache_capacity"]
    assert capacity["instances"] == {"instance-1": 250_000.0}
    in_service = {line["moment"]: line for line in written if line["kind"] == "in_service"}
    assert (in_service["start"]["scaler_in_service"], in_service["start"]["serving"]) == (1, ["instance-1"])
    assert (in_service["end"]["scaler_in_service"], in_service["end"]["serving"]) == (2, ["instance-1", "instance-2"])

    # The instance from before the hour still exports its stages in it, and the third began after it:
    # only the launch in the hour is written, once, under its settled label, with the stages it
    # ended after the hour.
    (started,) = [line for line in written if line["kind"] == "instance_started"]
    assert (started["instance"], started["transition"]) == ("instance-2", "cold-launch")
    assert started["stages"] == {stage: seconds + 3 for stage, seconds in STAGES.items()}
    assert hour["started_at"] < started["began_after_at"] < started["began_at"] <= hour["ends_at"]
    assert started["in_hour"] == "certain"


@pytest.mark.parametrize("boot", [40.0, 108.0], ids=["seen-after-the-hour", "booting-past-the-final-reads"])
def test_a_launch_that_began_just_before_the_hour_ended_is_written(boot: float) -> None:
    lines: list[str] = []
    rehearsal, _, instances = _rehearsal(lines, after_wake=[])
    # The kernel starts 3 seconds before the end of the hour; each stage is first sampled on the
    # 15-second grid after it ends.
    stages = {"boot": boot, "image": 10.0, "release-check": 1.0, "container-start": 3.0, "weights": 6.0, "hook": 1.0}
    ends = list(itertools.accumulate(stages.values(), initial=START + rehearsal.duration - 3))[1:]

    class Launching(Workspace):
        def _stages(self, start: float, end: float) -> list[common.Series]:
            series = []
            for (stage, seconds), ended in zip(stages.items(), ends, strict=True):
                first = START + 15 * -(-(ended - START) // 15)
                at = [t for t in range(int(first), int(end) + 1, 15) if t >= start]
                labels = {"instance": self.after, "transition": "promotion", "stage": stage}
                series.append(_series(labels, *((t, seconds) for t in at)))
            return [one for one in series if one.samples]

    rehearsal.workspace = Launching(*instances)
    _run(rehearsal)
    (started,) = [line for line in _kinds(lines) if line["kind"] == "instance_started"]
    hour = next(line for line in _kinds(lines) if line["kind"] == "hour_started")
    assert started["stages"] == stages
    # The latest it can have begun, which its later samples bring down into the hour.
    assert dry.utc(START + rehearsal.duration - 3) <= started["began_at"] <= hour["ends_at"]


@pytest.mark.parametrize(
    ("kernel", "phase", "in_hour"),
    [(-2.0, 4.0, []), (58.0, 4.0, ["certain"]), (56.0, 0.0, ["uncertain"])],
    ids=["just-before", "just-inside", "scraped-long-after-the-hour-ended"],
)
def test_a_launch_is_placed_by_when_its_stages_were_scraped_not_by_the_query_step(
    kernel: float, phase: float, in_hour: list[str]
) -> None:
    lines: list[str] = []
    rehearsal, _, instances = _rehearsal(lines, after_wake=[])
    stages = {"boot": 20.0, "image": 2.0, "release-check": 1.0, "container-start": 3.0, "weights": 148.0}
    stages |= {"warm-up": 3.0, "hook": 1.0}
    ends = list(itertools.accumulate(stages.values(), initial=START + kernel))[1:]

    class OutOfPhase(Workspace):
        """The collector scrapes `phase` seconds after each step of the query's grid; at each step the
        query returns the last sample scraped before it."""

        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            if dry.STAGE_METRIC not in query:
                return []
            series = []
            for (stage, seconds), ended in zip(stages.items(), ends, strict=True):
                steps = [start + n * step for n in range(int((end - start) // step) + 1)]
                scrapes = {t: START + phase + ((t - START - phase) // 15) * 15 for t in steps}
                taken = [(t, scrapes[t]) for t in steps if scrapes[t] >= ended]
                values = [(t, scrape if query.startswith("timestamp(") else seconds) for t, scrape in taken]
                series.append(_series({"instance": self.after, "transition": "promotion", "stage": stage}, *values))
            return [one for one in series if one.samples]

    rehearsal.workspace = OutOfPhase(*instances)
    rehearsal.clock.at = START + 300  # ty: ignore[missing-attribute] - the test's clock
    deadline = START + rehearsal.duration
    asyncio.run(rehearsal.started_instances(START, deadline))
    started = [line for line in _kinds(lines) if line["kind"] == "instance_started"]
    # A launch whose stages were each scraped up to a scrape after they ended may have begun in the
    # hour though the latest it can have begun is after it: it is written, with both bounds.
    assert [line["in_hour"] for line in started] == in_hour
    assert all(line["stages"] == stages for line in started)
    if in_hour == ["uncertain"]:
        assert started[0]["began_after_at"] <= dry.utc(deadline) < started[0]["began_at"]


@pytest.mark.parametrize("kernel", [-5.0, 55.0], ids=["just-before", "just-before-the-end"])
def test_a_first_scrape_long_after_its_stages_ended_leaves_the_earliest_unknown(kernel: float) -> None:
    lines: list[str] = []
    rehearsal, _, instances = _rehearsal(lines, after_wake=[])
    stages = {"boot": 20.0, "image": 5.0, "release-check": 1.0, "container-start": 4.0, "weights": 5.0}
    stages |= {"warm-up": 4.0, "hook": 1.0}
    hooked = START + kernel + sum(stages.values())

    class Late(Workspace):
        """The node's collector starts with the agent: its first scrape comes 25 seconds after the
        hook, with every stage already ended, then one every 15 seconds."""

        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            if dry.STAGE_METRIC not in query:
                return []
            scrapes = itertools.takewhile(lambda t: t <= end, itertools.count(hooked + 25, 15))
            at = [t for t in scrapes if t >= start]
            timed = query.startswith("timestamp(")
            labels = {"instance": self.after, "transition": "promotion"}
            return [
                _series({**labels, "stage": stage}, *((t, t if timed else seconds) for t in at))
                for stage, seconds in stages.items()
            ]

    rehearsal.workspace = Late(*instances)
    rehearsal.clock.at = START + 300  # ty: ignore[missing-attribute] - the test's clock
    asyncio.run(rehearsal.started_instances(START, START + rehearsal.duration))
    # Its latest is after the hour's start and nothing bounds it from below: it may have begun in the
    # hour, so it is written with its stages, and not as certain.
    (started,) = [line for line in _kinds(lines) if line["kind"] == "instance_started"]
    assert (started["in_hour"], started["began_after_at"], started["stages"]) == ("uncertain", None, stages)


@pytest.mark.parametrize("kernel", [-3.0, 55.0], ids=["just-before", "just-before-the-end"])
def test_a_launch_without_its_boot_is_not_bounded_from_below_by_its_own_scrapes(kernel: float) -> None:
    lines: list[str] = []
    rehearsal, _, instances = _rehearsal(lines, after_wake=[])
    # The agent could not read the uptime, so it wrote no boot: the kernel started 20 seconds before
    # the image stage, which no sample shows. Each stage is scraped on the grid after it ends.
    stages = {"image": 5.0, "release-check": 12.0, "hook": 1.0}
    ends = list(itertools.accumulate(stages.values(), initial=START + kernel + 20))[1:]

    class Unbooted(Workspace):
        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            if dry.STAGE_METRIC not in query:
                return []
            timed = query.startswith("timestamp(")
            labels = {"instance": self.after, "transition": "entry"}
            return [
                _series({**labels, "stage": stage}, *((t, t if timed else seconds) for t in grid))
                for (stage, seconds), ended in zip(stages.items(), ends, strict=True)
                if (grid := [t for t in range(int(START), int(end) + 1, 15) if t >= max(start, ended)])
            ]

    rehearsal.workspace = Unbooted(*instances)
    rehearsal.clock.at = START + 120  # ty: ignore[missing-attribute] - the test's clock
    asyncio.run(rehearsal.started_instances(START, START + rehearsal.duration))
    (started,) = [line for line in _kinds(lines) if line["kind"] == "instance_started"]
    assert (started["in_hour"], started["began_after_at"], started["stages"]) == ("uncertain", None, stages)


def test_a_counter_that_cannot_be_read_leaves_the_others_written() -> None:
    lines: list[str] = []
    rehearsal, _, _ = _rehearsal(lines)

    class Failing(Workspace):
        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            if "num_preemptions" in query:
                raise TimeoutError
            return super().read_series(query, start=start, end=end, step=step)

    rehearsal.workspace = Failing(*(f"i-{secrets.token_hex(8)}" for _ in range(3)))
    _run(rehearsal)
    written = _kinds(lines)
    for counters in (line for line in written if line["kind"] == "engine_counters"):
        assert counters["unread"] == ["preemptions"]
        assert {"input_tokens", "cached_input_tokens"} <= set(counters["instances"]["instance-1"])
    errors = [(line["step"], line["quantity"]) for line in written if line["kind"] == "error"]
    assert errors == [("start_counters", "preemptions"), ("end_counters", "preemptions")]


def test_several_agents_run_at_once_inside_each_of_the_class_sessions() -> None:
    lines: list[str] = []
    rehearsal, _, _ = _rehearsal(lines, shape=Shape.SEVERAL_AGENTS)
    _run(rehearsal)
    written = _kinds(lines)

    (start,) = [line for line in written if line["kind"] == "start"]
    assert (start["sessions"], start["agents_per_session"], start["shape"]) == (3, 2, "several-agents")
    hour = next(line for line in written if line["kind"] == "hour_started")
    firsts = {
        (line["session"], line["agent"])
        for line in written
        if line["kind"] == "request" and line["conversation"] == 0 and line["sent_at"] == hour["started_at"]
    }
    assert firsts == {(session, agent) for session in range(3) for agent in range(2)}


def test_the_job_refuses_a_class_it_was_not_given_or_cannot_key(capsys: pytest.CaptureFixture[str]) -> None:
    lines: list[str] = []
    clock = Clock()
    assert dry.main({dry.SHAPE_VARIABLE: "hands-on"}, clock) == 1
    assert dry.main({dry.CLASS_SIZE_VARIABLE: "0", dry.SHAPE_VARIABLE: "hands-on"}, clock) == 1
    assert dry.main({dry.CLASS_SIZE_VARIABLE: "12", dry.SHAPE_VARIABLE: "lecture"}, clock) == 1
    # A character `isdigit` takes and `int` does not.
    assert dry.main({dry.CLASS_SIZE_VARIABLE: "\u00b2", dry.SHAPE_VARIABLE: "hands-on"}, clock) == 1
    refused = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["kind"], line["reason"]) for line in refused] == [
        ("refused", "no_class_size"),
        ("refused", "no_class_size"),
        ("refused", "no_shape"),
        ("refused", "no_class_size"),
    ]
    with pytest.raises(ValueError, match="more students than"):
        dry.Rehearsal(
            engine="vllm",
            class_size=4,
            shape=Shape.SEVERAL_AGENTS,
            keys=[generate_key() for _ in range(3)],
            endpoint="https://gateway.example.com",
            gateway=Gateway([]),
            workspace=Workspace("a", "b", "c"),
            recorder=Recorder(clock, lines.append),
            clock=clock,
        )
