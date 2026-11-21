"""The heartbeat release smoke test (tests/smoke/deployed/test_release_heartbeats.py) fails when an
instance launched or in service during the run, the one that woke the pool included, wrote another
release in its heartbeat row, whatever a partner's row says; a row from before the run neither
passes nor fails it. A fake heartbeats table, whose rows are rewritten as time passes, and a fake
clock stand in for DynamoDB and for time: no account, no network.
"""

from __future__ import annotations

import runpy
import warnings
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "smoke" / "deployed" / "test_release_heartbeats.py"
RELEASE = ("sha256:" + "a" * 64, "b" * 40)
OTHER = ("sha256:" + "c" * 64, "b" * 40)
# The wake and hold of the run's first stream, which ends this long after the run began.
HOLD = 60.0

# One write of an instance's row: (instance, release, stage, seconds after the run began).
Write = tuple[str, tuple[str, str], str, float]


class Clock:
    """Time that passes only when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    monotonic = time


class Table:
    """The heartbeats table: each instance's latest write by now, one row a page, as boto3's
    table resource returns them."""

    def __init__(self, writes: Sequence[Write], clock: Clock, started: float) -> None:
        self.writes, self.clock, self.started = writes, clock, started

    def query(self, **request: Any) -> dict[str, Any]:
        assert request["ConsistentRead"] is True
        values = request["ExpressionAttributeValues"]
        assert values == {":pool": "coursework", ":instance": "instance#"}
        latest: dict[str, Write] = {}
        for write in sorted(self.writes, key=lambda write: write[3]):
            if self.started + write[3] <= self.clock.now:
                latest[write[0]] = write
        items = [
            {
                "pool": "coursework",
                "source": f"instance#{instance}",
                "running": Decimal(0),
                "waiting": Decimal(0),
                "image_digest": served[0],
                "model_revision": served[1],
                "stage": stage,
                "written_at": Decimal(int(self.started + at)),
                "expires_at": Decimal(int(self.started + at) + 3600),
            }
            for instance, served, stage, at in sorted(latest.values())
        ]
        start = request.get("ExclusiveStartKey", 0)
        page: dict[str, Any] = {"Items": items[start : start + 1]}
        if start + 1 < len(items):
            page["LastEvaluatedKey"] = start + 1
        return page


def run_against_writes(monkeypatch: pytest.MonkeyPatch, writes: Sequence[Write]) -> None:
    """The smoke test, called once the run's first stream has woken the coursework pool, against
    a table whose rows `writes` made."""
    with warnings.catch_warnings():  # the smoke tests' mark is registered by their own conftest
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
        smoke = runpy.run_path(str(SMOKE))
    check = smoke["test_every_instance_of_the_run_heartbeats_the_deployed_release"]
    clock = Clock()
    started = clock.now
    clock.sleep(HOLD)
    monkeypatch.setitem(check.__globals__, "time", clock)
    heartbeats = smoke["Heartbeats"](table=Table(writes, clock, started), pool="coursework")
    check(heartbeats=heartbeats, deployed_release=RELEASE, run_started=started)


def launched(instance: str, served: tuple[str, str], *, at: float = 5.0, until: float = 1000.0) -> list[Write]:
    """`instance` starting at `at`, then serving `served` and writing its row every 5 seconds."""
    stages = [(instance, served, "image", at), (instance, served, "engine", at + 10)]
    return stages + [(instance, served, "serving", float(t)) for t in range(int(at) + 20, int(until), 5)]


def serving(instance: str, served: tuple[str, str]) -> list[Write]:
    """`instance` in service since before the run, writing its row every 5 seconds."""
    return [(instance, served, "serving", float(t)) for t in range(-600, 1000, 5)]


def test_the_woken_instance_serving_the_deployed_release_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    run_against_writes(monkeypatch, [*launched("woken", RELEASE), *serving("partner", RELEASE)])


def test_the_woken_instance_serving_another_release_fails_despite_a_partner_s_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AssertionError, match="another release"):
        run_against_writes(monkeypatch, [*launched("woken", OTHER), *serving("partner", RELEASE)])


def test_an_instance_that_served_the_wake_and_stopped_fails_with_its_other_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance that served the wake wrote another release and stopped before the stream ended;
    its partner, of the deployed release, serves on."""
    with pytest.raises(AssertionError, match="another release"):
        run_against_writes(monkeypatch, [*launched("woken", OTHER, until=HOLD - 5), *serving("partner", RELEASE)])


def test_a_row_written_before_the_run_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replaced instance of another release last wrote its row before the run began."""
    stopped = [("replaced", OTHER, "serving", float(t)) for t in range(-900, -5, 5)]
    run_against_writes(monkeypatch, [*stopped, *launched("woken", RELEASE)])


def test_no_instance_in_service_since_the_stream_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The woken instance stopped writing its row before the stream ended."""
    with pytest.raises(AssertionError, match="no instance of the pool in service"):
        run_against_writes(monkeypatch, launched("woken", RELEASE, until=HOLD - 5))
