"""How long a pool has been in service while no session is on (`rhi_scaler.outside_sessions`)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rhi_common.records import SessionSwitch
from rhi_scaler.outside_sessions import minutes, stretch_since

T0 = 1_800_000_000
OFF = SessionSwitch(on=False, set_by="admin", set_at=T0 - 600)
ON = SessionSwitch(on=True, set_by="admin", set_at=T0 - 600)


def counts(runs: Sequence[tuple[int, int, SessionSwitch | None]]) -> list[int]:
    """The count after each run, a run being its time, its instances in service and the switch it read."""
    since: int | None = None
    out = []
    for now, in_service, switch in runs:
        since = stretch_since(since, in_service, switch, now)
        out.append(minutes(since, now))
    return out


@pytest.mark.parametrize("switch", [OFF, None], ids=["off", "no-record"])
def test_an_hour_and_a_minute_in_service_with_no_session_counts_61_minutes(switch: SessionSwitch | None) -> None:
    runs = [(T0 + 60 * n, 1, switch) for n in range(62)]
    assert counts(runs)[-1] == 61


def test_minutes_while_the_switch_reads_on_count_nothing() -> None:
    assert set(counts([(T0 + 60 * n, 2, ON) for n in range(90)])) == {0}


def test_the_count_restarts_after_the_pool_went_to_zero_and_when_the_switch_is_turned_on() -> None:
    after_zero: list[tuple[int, int, SessionSwitch | None]] = [(T0 + 60 * n, 1, OFF) for n in range(31)]
    after_zero += [(T0 + 1860, 0, OFF)]
    after_zero += [(T0 + 1920 + 60 * n, 1, OFF) for n in range(11)]
    assert counts(after_zero)[30:] == [30, 0, *range(11)]

    after_on: list[tuple[int, int, SessionSwitch | None]] = [(T0 + 60 * n, 1, OFF) for n in range(31)]
    after_on += [(T0 + 1860, 1, ON)]
    after_on += [(T0 + 1920 + 60 * n, 1, OFF) for n in range(11)]
    assert counts(after_on)[30:] == [30, 0, *range(11)]


def test_setting_an_off_switch_off_again_keeps_the_count() -> None:
    again = SessionSwitch(on=False, set_by="admin", set_at=T0 + 3600)
    runs = [(T0 + 60 * n, 1, OFF if n < 60 else again) for n in range(62)]
    assert counts(runs)[-1] == 61


def test_a_run_that_could_not_read_the_switch_still_ends_the_stretch_of_an_empty_pool() -> None:
    assert stretch_since(T0, 0, None, T0 + 3660, read=False) is None
    assert stretch_since(T0, 1, None, T0 + 3660, read=False) == T0
