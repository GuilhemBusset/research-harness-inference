"""What the gateway knows of each pool (`rhi_gateway.readiness`) and the wakes it asks for (`rhi_gateway.waker`).

Doubles stand in for the load-balancing API, the pool-state table and the scaler: each exposes only
the one call the modules may make and records every call. The clock is fake, and `run` is given a
sleep that moves it. Target groups are opaque names.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from rhi_common.records import Mode, PoolState, ScalerNote, Session
from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import Waker

POOLS = {"coursework": "group-coursework", "upgrade": "group-upgrade", "mock": "group-mock"}
START = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class LoadBalancing:
    """`describe_target_health`, with the states of each group's targets; a group in `failing` raises."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.states: dict[str, list[str]] = {}
        self.failing: set[str] = set()
        self.reads: list[tuple[str, float]] = []

    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        self.reads.append((TargetGroupArn, self.clock.now))
        if TargetGroupArn in self.failing:
            raise ConnectionError("unreachable")
        states = self.states.get(TargetGroupArn, [])
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": state}} for state in states]}


class PoolStateTable:
    """`get_item` on the pool-state table; raises while `failing`."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.failing = False
        self.reads: list[tuple[str, str]] = []

    def put(self, record: PoolState | ScalerNote | Session) -> None:
        item = record.to_item()
        self.items[str(item["pool"]), str(item["record"])] = item

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        self.reads.append((Key["pool"], Key["record"]))
        if self.failing:
            raise ConnectionError("unreachable")
        item = self.items.get((Key["pool"], Key["record"]))
        return {} if item is None else {"Item": item}


class Scaler:
    """`invoke` on the scaler function; it blocks until `release` is set."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **arguments: Any) -> dict[str, Any]:
        self.release.wait(10)
        self.calls.append(arguments)
        return {"StatusCode": 202}


def build() -> tuple[Clock, LoadBalancing, PoolStateTable, Readiness]:
    clock = Clock()
    elbv2 = LoadBalancing(clock)
    table = PoolStateTable()
    return clock, elbv2, table, Readiness(POOLS, elbv2=elbv2, table=table, clock=clock)


class Stop(Exception):
    pass


def run_for(readiness: Readiness, clock: Clock, cycles: int, callers: int = 0) -> tuple[list[float], int]:
    """Run the real loop for `cycles` cycles on the fake clock; the sleeps it asked for, and the callers' reads."""
    slept: list[float] = []
    asked = 0

    async def main() -> None:
        done = asyncio.Event()

        async def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.now += seconds
            await asyncio.sleep(0)
            if len(slept) == cycles:
                raise Stop

        async def caller() -> None:
            nonlocal asked
            while not done.is_set():
                for pool in POOLS:
                    readiness.pool(pool)
                    asked += 1
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(caller()) for _ in range(callers)]
        try:
            await readiness.run(sleep)
        except Stop:
            pass
        finally:
            done.set()
            await asyncio.gather(*tasks)

    asyncio.run(main())
    return slept, asked


def test_ready_with_one_healthy_target_not_ready_with_none_or_a_failed_read() -> None:
    _, elbv2, _, readiness = build()
    elbv2.states = {"group-coursework": ["unhealthy", "healthy"], "group-upgrade": ["unhealthy", "draining"]}
    assert not readiness.pool("coursework").ready  # nothing read yet
    readiness.refresh()
    assert readiness.pool("coursework").ready
    assert not readiness.pool("upgrade").ready
    assert not readiness.pool("mock").ready  # no target at all

    elbv2.failing.add("group-coursework")
    readiness.refresh()
    assert not readiness.pool("coursework").ready


def test_the_records_are_kept_and_a_failed_read_keeps_the_previous_ones() -> None:
    _, elbv2, table, readiness = build()
    mode = PoolState("coursework", Mode.ENABLED, set_by="admin", set_at=1)
    note = ScalerNote("coursework", capacity_asked=1, no_capacity_at=None, written_at=2)
    table.put(mode)
    table.put(note)
    readiness.refresh()
    assert (readiness.pool("coursework").mode, readiness.pool("coursework").note) == (mode, note)
    assert (readiness.pool("upgrade").mode, readiness.pool("upgrade").note) == (None, None)

    table.failing = True
    elbv2.states = {"group-coursework": ["healthy"]}
    readiness.refresh()
    view = readiness.pool("coursework")
    assert (view.ready, view.mode, view.note) == (True, mode, note)

    table.failing = False
    disabled = PoolState("coursework", Mode.DISABLED, set_by="admin", set_at=3)
    table.put(disabled)
    readiness.refresh()
    assert readiness.pool("coursework").mode == disabled


def test_each_pool_keeps_its_own_session_and_a_failed_read_keeps_the_session_last_read() -> None:
    _, _, table, readiness = build()
    now = int(START)
    booked = Session("coursework", instances=2, starts_at=now, ends_at=now + 3600, set_by="owner", set_at=now)
    table.put(booked)
    table.failing = True
    readiness.refresh()
    # In the table, and not read yet: the pool has no session until a first read succeeds.
    assert readiness.pool("coursework").session is None
    table.failing = False
    readiness.refresh()
    assert {pool: readiness.pool(pool).session for pool in POOLS} == {"coursework": booked, "upgrade": None, "mock": None}

    # Stopped in the table, behind a read that fails, and then an item that is no session: the session last read is kept.
    stopped = Session("coursework", instances=2, starts_at=now, ends_at=now + 60, set_by="owner", set_at=now + 60)
    table.put(stopped)
    table.failing = True
    readiness.refresh()
    assert readiness.pool("coursework").session == booked
    table.failing = False
    table.items["coursework", "session"] = {"pool": "coursework", "record": "session"}
    readiness.refresh()
    assert readiness.pool("coursework").session == booked
    table.put(stopped)
    readiness.refresh()
    assert readiness.pool("coursework").session == stopped
    # No item is an answer: the pool has no session.
    del table.items["coursework", "session"]
    readiness.refresh()
    assert readiness.pool("coursework").session is None


def test_each_pool_gives_the_rebalance_number_of_the_note_last_read() -> None:
    _, _, table, readiness = build()
    assert readiness.pool("coursework").rebalance_number is None  # nothing read yet
    table.put(ScalerNote("coursework", capacity_asked=2, no_capacity_at=None, written_at=2, rebalance_number=7))
    table.put(ScalerNote("upgrade", capacity_asked=1, no_capacity_at=None, written_at=2))
    readiness.refresh()
    numbers = {pool: readiness.pool(pool).rebalance_number for pool in POOLS}
    # A note the scaler has raised no number in gives 0, and a pool with no note gives none.
    assert numbers == {"coursework": 7, "upgrade": 0, "mock": None}

    # A read that fails keeps the note, and so its number; the next one that answers gives the new number.
    table.put(ScalerNote("coursework", capacity_asked=2, no_capacity_at=None, written_at=9, rebalance_number=8))
    table.failing = True
    readiness.refresh()
    assert readiness.pool("coursework").rebalance_number == 7
    table.failing = False
    readiness.refresh()
    assert readiness.pool("coursework").rebalance_number == 8


def test_read_every_5_seconds_for_every_pool() -> None:
    clock, elbv2, table, readiness = build()
    slept, _ = run_for(readiness, clock, cycles=6)
    assert slept == [5.0] * 6
    for pool, group in POOLS.items():
        assert [at for read, at in elbv2.reads if read == group] == [START + 5 * n for n in range(6)]
        assert table.reads.count((pool, "mode")) == 6
        assert table.reads.count((pool, "note")) == 6
        # The pool's session, once a cycle with its two other records.
        assert table.reads.count((pool, "session")) == 6
    # Nothing but the pools' own records: the session switch is never read.
    assert {pool for pool, _ in table.reads} == set(POOLS)
    alone = (len(elbv2.reads), len(table.reads))

    clock, elbv2, table, readiness = build()
    _, asked = run_for(readiness, clock, cycles=6, callers=100)
    assert asked >= 100 * len(POOLS)
    assert (len(elbv2.reads), len(table.reads)) == alone


def test_ask_returns_before_the_scaler_answers() -> None:
    _, _, _, readiness = build()
    scaler = Scaler()
    waker = Waker(readiness, lambda_client=scaler, function_name="scaler-function")
    future = waker.ask("coursework")
    assert not future.done()
    assert scaler.calls == []
    scaler.release.set()
    future.result(timeout=10)
    [call] = scaler.calls
    assert call["FunctionName"] == "scaler-function"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"pool": "coursework"}


def test_an_explicit_wake_is_pending_for_10_minutes_or_until_ready() -> None:
    clock, elbv2, _, readiness = build()
    waker = Waker(readiness, lambda_client=Scaler(), function_name="scaler-function", clock=clock)
    readiness.refresh()
    waker.record("coursework")
    waker.record("upgrade")
    for _ in range(10):
        clock.now += 5
        readiness.refresh()
        assert (waker.pending("coursework"), waker.pending("upgrade")) == (1, 1)

    clock.now += 5
    turned_healthy = clock.now
    elbv2.states["group-coursework"] = ["healthy"]
    readiness.refresh()
    assert waker.pending("coursework") == 0
    assert waker.ended_ready_at("coursework") == turned_healthy
    assert waker.pending("upgrade") == 1

    clock.now = START + 600 - 1
    readiness.refresh()
    assert waker.pending("upgrade") == 1
    clock.now = START + 600
    assert waker.pending("upgrade") == 0
    assert waker.ended_ready_at("upgrade") is None

    waker.record("coursework")  # on a ready pool, it ends at once
    assert waker.pending("coursework") == 0
    assert waker.ended_ready_at("coursework") == START + 600


def test_each_record_keeps_its_previous_value_when_only_its_read_fails() -> None:
    _, _, table, readiness = build()
    mode = PoolState("coursework", Mode.ENABLED, set_by="admin", set_at=1)
    table.put(mode)
    table.put(ScalerNote("coursework", capacity_asked=1, no_capacity_at=None, written_at=2))
    readiness.refresh()
    newer = ScalerNote("coursework", capacity_asked=0, no_capacity_at=5, written_at=6)
    table.put(newer)
    table.items["coursework", "mode"] = {"pool": "coursework", "record": "mode"}  # not a mode record
    readiness.refresh()
    assert (readiness.pool("coursework").mode, readiness.pool("coursework").note) == (mode, newer)


def test_a_wake_ends_ready_only_at_what_a_read_observed_while_the_clock_moved(monkeypatch: Any) -> None:
    clock, elbv2, table, readiness = build()
    waker = Waker(readiness, lambda_client=Scaler(), function_name="scaler-function", clock=clock)
    read_health, read_record = elbv2.describe_target_health, table.get_item

    def health_answers_after(during: Any) -> None:
        def slow(**arguments: Any) -> dict[str, Any]:
            during()
            return read_health(**arguments)

        monkeypatch.setattr(elbv2, "describe_target_health", slow)

    # A wake recorded while the read is in flight ends when the answer comes back, not before.
    def request_arrives() -> None:
        clock.now = START + 1
        waker.record("coursework")
        clock.now = START + 2

    elbv2.states["group-coursework"] = ["healthy"]
    health_answers_after(request_arrives)
    readiness.refresh()
    assert (waker.pending("coursework"), waker.ended_ready_at("coursework")) == (0, START + 2)

    # Health first seen ready after a wake's 10 minutes: that wake expired, it did not end ready.
    waker.record("mock")

    def turns_healthy_after_expiry() -> None:
        clock.now = START + 2 + 601
        elbv2.states["group-mock"] = ["healthy"]

    clock.now = START + 2 + 599
    health_answers_after(turns_healthy_after_expiry)
    readiness.refresh()
    assert (waker.pending("mock"), waker.ended_ready_at("mock")) == (0, None)

    # Seen ready before expiry, published after it: a caller asking in between erases nothing.
    monkeypatch.setattr(elbv2, "describe_target_health", read_health)
    clock.now = START + 1000
    elbv2.states["group-upgrade"] = []
    readiness.refresh()
    waker.record("upgrade")
    elbv2.states["group-upgrade"] = ["healthy"]
    clock.now = START + 1000 + 599

    def slow_record(**arguments: Any) -> dict[str, Any]:
        if arguments["Key"]["pool"] == "upgrade":
            clock.now = START + 1000 + 601
            waker.pending("upgrade")
        return read_record(**arguments)

    monkeypatch.setattr(table, "get_item", slow_record)
    readiness.refresh()
    assert (waker.pending("upgrade"), waker.ended_ready_at("upgrade")) == (0, START + 1000 + 599)
