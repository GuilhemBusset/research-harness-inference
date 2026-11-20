"""Spike S8's verdict (`spikes/s8_stickiness.py`): whether a student's requests stayed on one instance,
from recorded counts of what each instance served. No number here is a measurement; the instance
names are made up.
"""

from __future__ import annotations

import pytest

import common
import s8_stickiness as s8
from rhi_common.keys import generate_key
from rhi_reporter.measure import Verdict, measurement_event

Counts, Phase = s8.Counts, s8.Phase

STUCK = {1: Counts(10, {"instance-a": 10}), 2: Counts(10, {"instance-b": 10})}
REMAINED = {1: Counts(10, {"instance-a": 10}), 2: Counts(10, {"instance-a": 10})}


def test_each_student_on_one_instance_before_and_after_the_departure_passes() -> None:
    numbers = s8.numbers_of(2, {Phase.BEFORE: STUCK, Phase.AFTER: REMAINED})
    assert set(numbers) == {name.value for name in s8.Number}
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS
    # The numbers are the declared names, in the shape the reporter takes.
    names = [name.value for name in s8.Number]
    measurement_event("S8", "s8-stickiness", Verdict.PASS, numbers, names=names, commit="a" * 40, bus="bus")


def test_a_student_split_between_two_instances_before_the_departure_fails() -> None:
    split = {**STUCK, 2: Counts(10, {"instance-a": 3, "instance-b": 7})}
    numbers = s8.numbers_of(2, {Phase.BEFORE: split, Phase.AFTER: REMAINED})
    assert numbers[s8.most_served(2, Phase.BEFORE)] == 7
    assert s8.SPIKE.verdict(numbers) is Verdict.FAIL


def test_two_instances_in_one_zone_give_no_verdict() -> None:
    numbers = s8.numbers_of(1, {Phase.BEFORE: STUCK, Phase.AFTER: REMAINED})
    assert s8.SPIKE.verdict(numbers) is Verdict.NOT_MEASURED


def test_the_counts_are_the_rise_of_each_instance_s_count_in_the_window() -> None:
    start, end = 1000.0, 1300.0
    series = [
        common.Series({"instance": "instance-a", "zone": "zone-1"}, ((start, 40.0), (end, 52.0))),
        common.Series({"instance": "instance-b", "zone": "zone-2"}, ((start, 9.0), (end, 9.0))),
        # An instance whose series began inside the window counts from 0.
        common.Series({"instance": "instance-c", "zone": "zone-2"}, ((end, 3.0),)),
    ]
    assert s8.served_between(series, start, end) == {"instance-a": 12, "instance-c": 3}


def test_a_phase_is_measured_again_while_the_rebalance_number_changes() -> None:
    readings = iter([4, 4, 4, 5, 5, 5])
    measured: list[int] = []

    def measure() -> s8.PhaseCounts:
        measured.append(1)
        return STUCK

    # Unchanged on the first attempt: measured once.
    assert s8.measure_undisturbed(Phase.BEFORE, measure, lambda: next(readings), log=lambda _: None) == STUCK
    assert len(measured) == 1
    # Changed from 4 to 5 on the next: measured again, and kept once it holds.
    s8.measure_undisturbed(Phase.AFTER, measure, lambda: next(readings), log=lambda _: None)
    assert len(measured) == 3


def test_a_phase_disturbed_on_every_attempt_is_not_measured() -> None:
    numbers = iter(range(100))
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: STUCK, lambda: next(numbers), attempts=2, log=lambda _: None)
    # Counts that do not add up to what the student sent: another request was in its window.
    extra = {1: Counts(10, {"instance-a": 11}), 2: STUCK[2]}
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: extra, lambda: 0, attempts=2, log=lambda _: None)


def _answered(load: s8.loadgen.Load) -> list[s8.loadgen.Record]:
    """A record answered 200 for every request of the load's sessions."""
    records = []
    for number, session in enumerate(load.sessions):
        requests = session.requests()
        step = next(requests, None)
        while step is not None:
            records.append(s8.loadgen.Record(0, number, True, 200, 0.1, 0.1, 50.0, 200, 0, 20))
            try:
                step = requests.send("answer")
            except StopIteration:
                step = None
    return records


class _Pool:
    """A pool whose workspace answers are given in the order the job reads them."""

    name = "mock"

    def __init__(self, served: list[dict[str, int]] | None = None, observed: list[dict[str, str]] | None = None):
        self.windows, self.observed, self.seen = iter(served or []), observed or [], []

    def served(self, start: float, end: float) -> dict[str, int]:
        return next(self.windows)

    def serving(self) -> dict[str, str]:
        self.seen.append(self.observed[min(len(self.seen), len(self.observed) - 1)])
        return self.seen[-1]

    def present(self) -> set[str]:
        return set(self.seen[-1])

    def busy(self) -> int:
        return 0


@pytest.mark.parametrize(
    ("second_student_on", "verdict"),
    [("instance-a", Verdict.PASS), ("instance-b", Verdict.FAIL)],
)
def test_the_first_requests_at_once_count_toward_the_verdict(
    monkeypatch: pytest.MonkeyPatch, second_student_on: str, verdict: Verdict
) -> None:
    # The two first requests reach two instances; student 1 is then on instance-b. Student 2 then
    # either stays on instance-a, or moves to instance-b while both instances are in service.
    async def replay(load: s8.loadgen.Load, keys: object, endpoint: str, *, at_once: int) -> list:
        return _answered(load)

    monkeypatch.setattr(s8.loadgen, "replay", replay)
    monkeypatch.setattr(s8, "SETTLE_SECONDS", 0)
    firsts = {"instance-a": 1, "instance-b": 1}
    pool = _Pool([firsts, {"instance-b": 3}, {second_student_on: 3}])
    run = s8.Run(pool, "https://gateway.example.com", [generate_key()] * 2, 3, 8)  # ty: ignore[invalid-argument-type] - a test double
    before = s8.asyncio.run(run.phase(first_at_once=True))
    assert all(held.sent == 4 and held.adds_up() for held in before.values())
    assert s8.SPIKE.verdict(s8.numbers_of(2, {Phase.BEFORE: before, Phase.AFTER: REMAINED})) is verdict


def test_two_instances_are_two_seen_in_service_together(monkeypatch: pytest.MonkeyPatch) -> None:
    async def held(load: s8.loadgen.Load, keys: object, endpoint: str, *, at_once: int) -> list:
        (stream, *_) = load.sessions
        assert isinstance(stream, s8.HeldStream)
        await stream.stop.wait()
        return []

    monkeypatch.setattr(s8.loadgen, "replay", held)
    monkeypatch.setattr(s8, "POLL_SECONDS", 0)
    observed = [{"instance-a": "zone-1"}, {"instance-b": "zone-2"}, {"instance-a": "zone-1", "instance-b": "zone-2"}]
    pool = _Pool(observed=observed)
    run = s8.Run(pool, "https://gateway.example.com", [generate_key()] * 8, 3, 8)  # ty: ignore[invalid-argument-type] - a test double
    # Instance-a alone, then instance-b alone, is not two instances: the load is held until both are seen at once.
    assert s8.asyncio.run(run.two_instances()) == observed[2]
    assert len(pool.seen) == 3
