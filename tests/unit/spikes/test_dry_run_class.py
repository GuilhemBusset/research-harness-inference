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
from rhi_common.keys import generate_key
from s5_capacity import Shape
from s7_drain import Recorder

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

START = 1_800_000_000.0
# Turns of the event loop that let every task of a run go on until it waits on the clock.
YIELDS = 50
STAGES = {"boot": 20.0, "image": 1.5, "release-check": 0.5, "container-start": 2.0, "weights": 70.0, "hook": 1.0}


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
    usage = {"input_tokens": 10, "cache_read_input_tokens": 30, "output_tokens": 3}
    events = [
        _event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
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
    """The gateway's answers by student: the first held (a keep-alive first), the second warm with its
    third request cut, the third refused for its budget."""

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
        stream, clock = _stream(ping=student == 0), self.clock

        async def paced() -> Any:
            for event in stream.split(b"\n\n")[:-1]:
                await clock.sleep(0.25)
                yield event + b"\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=paced())


def _series(labels: dict[str, str], *samples: tuple[float, float]) -> common.Series:
    return common.Series(labels, samples)


class Workspace:
    """The workspace: two instances in service, the second's launch inside the hour; the first, from
    before it, still exporting its stages."""

    def __init__(self, before: str, during: str) -> None:
        self.before, self.during = before, during
        self.queries: list[str] = []

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        self.queries.append(query)
        late = start > START + 30
        both = [self.before, self.during] if late else [self.before]
        if dry.STAGE_METRIC in query:
            old = [
                _series(
                    {"instance": self.before, "transition": "promotion", "stage": stage},
                    *((at, seconds) for at in (start, START, START + 30, end)),
                )
                for stage, seconds in STAGES.items()
            ]
            new = [
                _series(
                    {"instance": self.during, "transition": "promotion", "stage": stage},
                    *((at, seconds + 3) for at in (START + 45, end)),
                )
                for stage, seconds in STAGES.items()
            ]
            return old + new
        if "timestamp(rhi_lifecycle_release_info" in query:
            return [_series({"instance": one}, (end, end - 5)) for one in both]
        if dry.INSTANCES_IN_SERVICE in query:
            return [_series({}, (end, float(len(both))))]
        if dry.CACHE_CAPACITY_METRIC in query:
            return [_series({"instance": self.before}, (end, 250_000.0))]
        return [_series({"instance": one}, (end, 1000.0)) for one in both]


def _conversation(seed: int) -> loadgen.Session:
    turns = (loadgen.Turn(5.0, 4), loadgen.Turn(5.0, 4))
    return loadgen.SyntheticSession(dry.POOL, 8, turns, 16, seed)


def _rehearsal(
    lines: list[str], *, after_wake: list[str] | None = None, shape: Shape = Shape.HANDS_ON
) -> tuple[dry.Rehearsal, list[str], list[str]]:
    clock = Clock()
    keys = [generate_key() for _ in range(3)]
    instances = [f"i-{secrets.token_hex(8)}" for _ in range(2)]

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
    assert {"pool_state", "request", "hour_ended", "instance_started", "end"} <= kinds
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
    # The held student's streams had a keep-alive; the warm student's had none.
    assert all(line["held"] and line["kept_alive"] for line in answered if line["session"] == 0)
    assert any(not line["held"] for line in answered if line["session"] == 1)

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


def test_a_request_sent_while_the_status_said_not_ready_is_held_without_a_keep_alive() -> None:
    lines: list[str] = []
    # Ready for the wait, then warming at the hour's first status read.
    rehearsal, _, _ = _rehearsal(lines, after_wake=["ready", "warming"])
    _run(rehearsal)
    written = _kinds(lines)

    assert [line["state"] for line in written if line["kind"] == "pool_state"] == ["warming", "ready"]
    first_warm = next(line for line in written if line["kind"] == "request" and line["session"] == 1)
    assert (first_warm["held"], first_warm["kept_alive"], first_warm["pool_not_ready_read"]) == (True, False, True)


def test_the_readings_name_instances_and_tell_a_launch_in_the_hour_from_one_before_it() -> None:
    lines: list[str] = []
    rehearsal, _, _ = _rehearsal(lines)
    _run(rehearsal)
    written = _kinds(lines)

    counters = {line["moment"]: line["instances"] for line in written if line["kind"] == "engine_counters"}
    assert set(counters["start"]) == {"instance-1"}
    assert set(counters["end"]) == {"instance-1", "instance-2"}
    assert {"input_tokens", "cached_input_tokens", "preemptions"} <= set(counters["end"]["instance-2"])
    (capacity,) = [line for line in written if line["kind"] == "cache_capacity"]
    assert capacity["instances"] == {"instance-1": 250_000.0}
    in_service = {line["moment"]: line for line in written if line["kind"] == "in_service"}
    assert (in_service["start"]["scaler_in_service"], in_service["start"]["serving"]) == (1, ["instance-1"])
    assert (in_service["end"]["scaler_in_service"], in_service["end"]["serving"]) == (2, ["instance-1", "instance-2"])

    # The instance from before the hour still exports its stages in it: only the new launch is written.
    (started,) = [line for line in written if line["kind"] == "instance_started"]
    assert (started["instance"], started["transition"]) == ("instance-2", "promotion")
    assert started["stages"] == {stage: seconds + 3 for stage, seconds in STAGES.items()}


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
    refused = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["kind"], line["reason"]) for line in refused] == [
        ("refused", "no_class_size"),
        ("refused", "no_class_size"),
        ("refused", "no_shape"),
    ]
    with pytest.raises(ValueError, match="more students than"):
        dry.Rehearsal(
            engine="vllm",
            class_size=4,
            shape=Shape.SEVERAL_AGENTS,
            keys=[generate_key() for _ in range(3)],
            endpoint="https://gateway.example.com",
            gateway=Gateway([]),
            workspace=Workspace("a", "b"),
            recorder=Recorder(clock, lines.append),
            clock=clock,
        )
