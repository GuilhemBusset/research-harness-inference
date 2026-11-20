"""Spike S7's verdict (`spikes/s7_drain.py`): how long an idle instance took to leave on each path,
whether the held streams completed and whether a wake was lost, from recorded numbers. No number
here is a measurement; the instance names are made up.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import common
import s7_drain as s7
from rhi_common.keys import generate_key
from rhi_mock_engine.app import Settings, create_app
from rhi_reporter.measure import Verdict, measurement_event

Number = s7.Number

RECORDED: dict[str, int | float] = {
    Number.LEAVE_SECONDS_TERMINATED: 90,
    Number.LEAVE_SECONDS_WARM_POOL: 90,
    Number.STREAM_COMPLETED_SCALE_IN: 1,
    Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT: 1,
    Number.WAKES_SENT: 3,
    Number.WAKES_LOST: 0,
}


def test_an_idle_instance_gone_in_90_seconds_with_every_stream_complete_and_no_wake_lost_passes() -> None:
    assert s7.SPIKE.measurement == "S7"
    assert set(RECORDED) == set(Number)
    assert s7.SPIKE.verdict(RECORDED) is Verdict.PASS
    # The numbers are the declared names, in the shape the reporter takes.
    names = [name.value for name in Number]
    measurement_event("S7", "s7-drain", Verdict.PASS, RECORDED, names=names, commit="a" * 40, bus="bus")


@pytest.mark.parametrize("path", [Number.LEAVE_SECONDS_TERMINATED, Number.LEAVE_SECONDS_WARM_POOL])
def test_an_idle_instance_gone_in_180_seconds_fails(path: Number) -> None:
    assert s7.SPIKE.verdict({**RECORDED, path: 180}) is Verdict.FAIL


def test_one_lost_wake_fails() -> None:
    assert s7.SPIKE.verdict({**RECORDED, Number.WAKES_LOST: 1}) is Verdict.FAIL


@pytest.mark.parametrize("stream", [Number.STREAM_COMPLETED_SCALE_IN, Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT])
def test_a_held_stream_that_did_not_complete_fails(stream: Number) -> None:
    assert s7.SPIKE.verdict({**RECORDED, stream: 0}) is Verdict.FAIL


def test_an_instance_has_left_once_its_collector_has_been_silent() -> None:
    asked_at = 1000.0
    series = [
        common.Series({"instance": "instance-a"}, ((1000.0, 990.0), (1015.0, 1005.0), (1030.0, 1005.0))),
        common.Series({"instance": "instance-b"}, ((1000.0, 990.0), (1015.0, 1080.0))),
    ]
    written = s7.last_writes(series)
    assert written == {"instance-a": 1005.0, "instance-b": 1080.0}
    # instance-b still writes; instance-a has been silent long enough to have stopped.
    assert s7.stopped(written, {"instance-a", "instance-b"}, now=1100.0) == {"instance-a": 1005.0}
    assert s7.leave_seconds(asked_at, 1005.0) == 1005.0 + s7.SCRAPE_SECONDS - asked_at


def test_the_path_follows_the_warm_count() -> None:
    assert s7.path_of(0, 0, newcomer=False) is s7.Path.TERMINATED
    assert s7.path_of(0, 1, newcomer=False) is s7.Path.WARM_POOL
    # A fresh instance launched into the warm pool may stand for a terminated one: not told.
    assert s7.path_of(0, 1, newcomer=True) is None
    assert s7.warm_around([(990.0, 0.0), (1005.0, 0.0), (1020.0, 1.0)], 1000.0) == (0, 1)


def test_a_run_without_both_paths_is_not_measured() -> None:
    streams = {Number.STREAM_COMPLETED_SCALE_IN: True, Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT: True}
    terminated = s7.Departure(s7.Path.TERMINATED, 60.0)
    with pytest.raises(ValueError, match="warm_pool"):
        s7.numbers_of([terminated, s7.Departure(None, 30.0)], streams, 3, 0)
    numbers = s7.numbers_of([terminated, s7.Departure(s7.Path.WARM_POOL, 40.0), terminated], streams, 3, 0)
    assert numbers[Number.LEAVE_SECONDS_WARM_POOL] == 40.0
    assert s7.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_slow_stream_is_read_over_the_seconds_given_and_completes() -> None:
    key = generate_key()

    async def no_delay(seconds: float) -> None:
        pass

    paused: list[float] = []

    async def pause(seconds: float) -> None:
        paused.append(seconds)

    transport = httpx.ASGITransport(app=create_app(Settings(secret=key), sleep=no_delay))
    request = s7._request("mock")
    assert asyncio.run(s7.slow_stream("http://engine", key, request, 60.0, transport=transport, sleep=pause))
    assert sum(paused) == pytest.approx(60.0)
    # A student the engine refuses has no stream to complete.
    refused = httpx.ASGITransport(app=create_app(Settings(secret=key), sleep=no_delay))
    assert not asyncio.run(s7.slow_stream("http://engine", generate_key(), request, 60.0, transport=refused))
