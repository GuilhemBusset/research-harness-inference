"""Independent checks of cached readiness, background reads and explicit wake demand."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any

from rhi_common.records import Mode, PoolState, ScalerNote
from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import Waker

GROUPS = {"coursework": "group-coursework", "mock": "group-mock"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Health:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.states: dict[str, list[str]] = {}
        self.failed: set[str] = set()
        self.calls: list[tuple[str, float]] = []
        self.before_return: Callable[[], None] | None = None

    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        self.calls.append((TargetGroupArn, self.clock()))
        if TargetGroupArn in self.failed:
            raise ConnectionError("test health read failed")
        if self.before_return is not None:
            self.before_return()
        return {"TargetHealthDescriptions": [
            {"TargetHealth": {"State": state}} for state in self.states.get(TargetGroupArn, [])
        ]}


class Table:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.failed: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, str, float]] = []

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        key = (Key["pool"], Key["record"])
        self.calls.append((*key, self.clock()))
        if key in self.failed:
            raise ConnectionError("test record read failed")
        return {"Item": self.items[key]} if key in self.items else {}


class Scaler:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(kwargs)
        self.entered.set()
        assert self.release.wait(3), "test must release the scaler double"
        return {"StatusCode": 202}


def build() -> tuple[Clock, Health, Table, Readiness]:
    clock = Clock()
    health = Health(clock)
    table = Table(clock)
    readiness = Readiness(GROUPS, elbv2=health, table=table, clock=clock)
    return clock, health, table, readiness


def test_health_and_independent_record_failures() -> None:
    _, health, table, readiness = build()
    mode = PoolState("coursework", Mode.ENABLED, "admin", 1)
    note = ScalerNote("coursework", 2, 3, 4)
    table.items["coursework", "mode"] = mode.to_item()
    table.items["coursework", "note"] = note.to_item()
    health.states["group-coursework"] = ["draining", "healthy"]
    health.states["group-mock"] = ["unhealthy"]
    readiness.refresh()
    assert readiness.pool("coursework").ready
    assert not readiness.pool("mock").ready
    assert readiness.pool("coursework").mode == mode
    assert readiness.pool("coursework").note == note

    changed_note = ScalerNote("coursework", 0, None, 5)
    table.items["coursework", "note"] = changed_note.to_item()
    table.failed.add(("coursework", "mode"))
    health.failed.add("group-coursework")
    readiness.refresh()
    assert not readiness.pool("coursework").ready
    assert readiness.pool("coursework").mode == mode
    assert readiness.pool("coursework").note == changed_note

    table.failed.clear()
    table.items["coursework", "mode"] = {"record": "mode"}
    table.failed.add(("coursework", "note"))
    readiness.refresh()
    assert readiness.pool("coursework").mode == mode
    assert readiness.pool("coursework").note == changed_note
    table.failed.clear()
    table.items.clear()
    health.failed.clear()
    health.states.clear()
    readiness.refresh()
    assert readiness.pool("coursework").mode is None
    assert readiness.pool("coursework").note is None
    assert not readiness.pool("coursework").ready


def test_background_reads_have_same_cadence_with_no_callers_and_100_callers() -> None:
    class Finished(Exception):
        pass

    async def scenario(callers: int) -> tuple[list[tuple[str, float]], list[tuple[str, str, float]]]:
        clock, health, table, readiness = build()
        waker = Waker(readiness, lambda_client=Scaler(), function_name="scaler", clock=clock)
        stop = asyncio.Event()
        ticks = 0

        async def reader() -> None:
            while not stop.is_set():
                for pool in GROUPS:
                    readiness.pool(pool)
                    readiness.views()
                    waker.pending(pool)
                    waker.ended_ready_at(pool)
                await asyncio.sleep(0)

        async def advance(seconds: float) -> None:
            nonlocal ticks
            assert seconds == 5
            ticks += 1
            clock.now += seconds
            await asyncio.sleep(0)
            if ticks == 4:
                raise Finished

        tasks = [asyncio.create_task(reader()) for _ in range(callers)]
        try:
            await readiness.run(advance)
        except Finished:
            pass
        finally:
            stop.set()
            await asyncio.gather(*tasks)
        return health.calls, table.calls

    idle = asyncio.run(scenario(0))
    busy = asyncio.run(scenario(100))
    assert idle == busy
    health_calls, table_calls = idle
    for pool, group in GROUPS.items():
        assert [at for name, at in health_calls if name == group] == [1000, 1005, 1010, 1015]
        for record in ("mode", "note"):
            assert [at for name, kind, at in table_calls if (name, kind) == (pool, record)] == [
                1000, 1005, 1010, 1015,
            ]


def test_scaler_is_invoked_without_waiting_for_its_answer() -> None:
    clock, _, _, readiness = build()
    scaler = Scaler()
    waker = Waker(readiness, lambda_client=scaler, function_name="scaler", clock=clock)
    try:
        future = waker.ask("mock")
        assert scaler.entered.wait(2)
        assert not future.done()
        [call] = scaler.calls
        assert call["FunctionName"] == "scaler"
        assert call["InvocationType"] == "Event"
        assert json.loads(call["Payload"]) == {"pool": "mock"}
    finally:
        scaler.release.set()
    future.result(timeout=2)


def test_explicit_wakes_count_per_pool_until_readiness_or_individual_expiry() -> None:
    clock, health, _, readiness = build()
    waker = Waker(readiness, lambda_client=Scaler(), function_name="scaler", clock=clock)
    waker.record("mock")
    waker.record("coursework")
    clock.now = 1010
    waker.record("mock")
    assert waker.pending("mock") == 2
    clock.now = 1030
    health.states["group-coursework"] = ["healthy"]
    readiness.refresh()
    assert waker.pending("coursework") == 0
    assert waker.ended_ready_at("coursework") == 1030
    assert waker.pending("mock") == 2
    clock.now = 1599
    assert waker.pending("mock") == 2
    clock.now = 1600
    assert waker.pending("mock") == 1
    clock.now = 1610
    assert waker.pending("mock") == 0
    assert waker.ended_ready_at("mock") is None


def test_wake_recorded_during_read_cannot_end_before_it_was_recorded() -> None:
    clock = Clock()
    health = Health(clock)
    readiness = Readiness({"mock": "group-mock"}, elbv2=health, table=Table(clock), clock=clock)
    waker = Waker(readiness, lambda_client=Scaler(), function_name="scaler", clock=clock)
    health.states["group-mock"] = ["healthy"]

    def request_arrives_while_health_is_being_read() -> None:
        clock.now = 1001
        waker.record("mock")
        assert waker.pending("mock") == 1
        clock.now = 1002

    health.before_return = request_arrives_while_health_is_being_read
    readiness.refresh()
    assert waker.pending("mock") == 0
    ended = waker.ended_ready_at("mock")
    assert ended is not None
    assert 1001 <= ended <= 1002, "a wake cannot have ended before it was recorded"


def test_health_first_observed_after_expiry_does_not_complete_expired_wake() -> None:
    clock = Clock()
    health = Health(clock)
    readiness = Readiness({"mock": "group-mock"}, elbv2=health, table=Table(clock), clock=clock)
    waker = Waker(readiness, lambda_client=Scaler(), function_name="scaler", clock=clock)
    waker.record("mock")
    clock.now = 1599

    def read_finishes_after_the_wake_expired() -> None:
        clock.now = 1601
        health.states["group-mock"] = ["healthy"]

    health.before_return = read_finishes_after_the_wake_expired
    readiness.refresh()
    assert waker.pending("mock") == 0
    assert waker.ended_ready_at("mock") is None, "the 600-second expiry happened before readiness was observed"
