"""What the gateway knows of each pool (`rhi_gateway.readiness`).

Doubles stand in for the load-balancing API and the pool-state table: each exposes only
the one call the module may make and records every call. The clock is fake, and `run` is given a
sleep that moves it. Target groups are opaque names.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rhi_common.causes import Cause
from rhi_common.records import Mode, PoolState, ScalerNote, Session
from rhi_gateway.readiness import Readiness

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


def test_the_note_is_kept_and_a_failed_read_keeps_the_previous_one() -> None:
    _, elbv2, table, readiness = build()
    note = ScalerNote("coursework", capacity_asked=1, no_capacity_at=None, written_at=2, starting=1)
    table.put(note)
    readiness.refresh()
    assert (readiness.pool("coursework").note, readiness.pool("upgrade").note) == (note, None)

    table.failing = True
    elbv2.states = {"group-coursework": ["healthy"]}
    readiness.refresh()
    view = readiness.pool("coursework")
    assert (view.ready, view.note) == (True, note)

    # An item that is not a note keeps the note last read, and the next note read replaces it.
    table.failing = False
    table.items["coursework", "note"] = {"pool": "coursework", "record": "note"}
    readiness.refresh()
    assert readiness.pool("coursework").note == note
    newer = ScalerNote("coursework", capacity_asked=0, no_capacity_at=5, written_at=6)
    table.put(newer)
    readiness.refresh()
    assert readiness.pool("coursework").note == newer


def test_whether_a_pool_can_serve_is_its_session_its_health_and_the_note_s_count_of_instances_starting() -> None:
    _, elbv2, table, readiness = build()
    now = int(START)

    def refusal(pool: str, at: int = now) -> Cause | None:
        readiness.refresh()
        return readiness.pool(pool).refusal(at, session_max_hours=12)

    assert refusal("coursework") is Cause.NO_SESSION
    table.put(Session("coursework", instances=1, starts_at=now, ends_at=now + 3600, set_by="owner", set_at=now))
    # No note, a note stored without the count, and a note that counts none starting: none is starting.
    assert refusal("coursework") is Cause.CAPACITY_UNAVAILABLE
    table.items["coursework", "note"] = {"pool": "coursework", "record": "note", "capacity_asked": 1, "written_at": now}
    assert refusal("coursework") is Cause.CAPACITY_UNAVAILABLE
    table.put(ScalerNote("coursework", capacity_asked=1, no_capacity_at=None, written_at=now, starting=0, waiting=1))
    assert refusal("coursework") is Cause.CAPACITY_UNAVAILABLE
    table.put(ScalerNote("coursework", capacity_asked=1, no_capacity_at=None, written_at=now, starting=1))
    assert refusal("coursework") is Cause.WARMING
    elbv2.states = {"group-coursework": ["healthy"]}
    assert refusal("coursework") is None
    # Once the session has ended the pool serves no one, healthy or not.
    assert refusal("coursework", at=now + 3600) is Cause.NO_SESSION

    # The admin's mode record is not read: whatever it holds, the pool serves by the rule above.
    for mode in (Mode.DISABLED, Mode.SCHEDULED_ONLY):
        table.put(PoolState("coursework", mode, set_by="admin", set_at=now))
        assert refusal("coursework") is None
    assert ("coursework", "mode") not in table.reads


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
    assert {pool: readiness.pool(pool).session for pool in POOLS} == {
        "coursework": booked,
        "upgrade": None,
        "mock": None,
    }

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
        assert table.reads.count((pool, "note")) == 6
        # The pool's session, once a cycle with its note.
        assert table.reads.count((pool, "session")) == 6
    # Nothing but the pools' own two records: no pool's mode and no session switch is ever read.
    assert set(table.reads) == {(pool, record) for pool in POOLS for record in ("note", "session")}
    alone = (len(elbv2.reads), len(table.reads))

    clock, elbv2, table, readiness = build()
    _, asked = run_for(readiness, clock, cycles=6, callers=100)
    assert asked >= 100 * len(POOLS)
    assert (len(elbv2.reads), len(table.reads)) == alone
