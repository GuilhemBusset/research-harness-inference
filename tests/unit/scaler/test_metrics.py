"""The scaler's remote-write series (`rhi_scaler.metrics`)."""

from __future__ import annotations

from rhi_common.pool_config import parse_pools
from rhi_common.records import Mode
from rhi_scaler.metrics import DECISIONS, DecisionCounts, decode, encode, fleet_series
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
POOL = parse_pools(
    {
        "mock": {
            "engine": "mock",
            "instance_types": ["t4g.small"],
            "prices": {"t4g.small": 0.0168},
            "max_instances": 2,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 4,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]


def run(now: int, reason: Reason, counts: DecisionCounts) -> dict[tuple[str, ...], list[tuple[float, int]]]:
    inputs = Inputs(now=now, pools=(PoolInputs(config=POOL, mode=Mode.ENABLED, supply=Supply(desired=1, in_service=("i-a",))),))
    decision = Decision(pools=(PoolDecision("mock", 1, (), False, Memory(), reason),))
    out = {}
    for timeseries in decode(encode(fleet_series(inputs, decision, counts))).timeseries:
        names = [label.name for label in timeseries.labels]
        assert names == sorted(names) and names[0] == "__name__"
        key = tuple(label.value for label in timeseries.labels)
        out[key] = [(sample.value, sample.timestamp) for sample in timeseries.samples]
    return out


def test_the_decisions_counter_counts_from_zero_and_keeps_counting() -> None:
    counts = DecisionCounts()
    first = run(NOW, Reason.WAKE, counts)
    # A series the process sends for the first time starts at zero, so the first decision counts.
    assert first[(DECISIONS, "mock", "wake")] == [(0.0, NOW * 1000 - 1), (1.0, NOW * 1000)]
    second = run(NOW + 60, Reason.UNCHANGED, counts)
    third = run(NOW + 120, Reason.UNCHANGED, counts)
    assert second[(DECISIONS, "mock", "wake")] == [(1.0, (NOW + 60) * 1000)]
    assert third[(DECISIONS, "mock", "unchanged")] == [(2.0, (NOW + 120) * 1000)]
    assert third[("rhi_scaler_instances_in_service", "mock")] == [(1.0, (NOW + 120) * 1000)]
