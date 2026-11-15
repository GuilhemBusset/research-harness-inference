"""A pool's entry: the fields of section 4.2, their defaults and their refusals (`rhi_common.pool_config`)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from rhi_common.pool_config import PoolConfigError, parse_pools

# A commit hash made when the test runs: 40 hexadecimal characters that name no real commit.
REVISION = hashlib.sha1(b"a pinned commit", usedforsecurity=False).hexdigest()
# Marks a field a case leaves out of the entry.
ABSENT = object()

# The coursework entry of section 4.2, with a revision, a context length and a largest output.
COURSEWORK = {
    "engine": "vllm",
    "model": "Qwen/Qwen3.8-27B-FP8",
    "revision": REVISION,
    "instance_types": ["g7e.2xlarge"],
    "max_instances": 2,
    "warm_instances": 2,
    "wake": "on-demand",
    "target_concurrency": 8,
    "hold_seconds": 420,
    "idle_minutes": 20,
    "drain_seconds": 300,
    "gpu_hours_per_day": 16,
    "context_length": 262144,
    "largest_output": 32000,
}
MOCK = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "max_instances": 1,
    "warm_instances": 0,
    "wake": "scheduled-only",
    "target_concurrency": 64,
    "context_length": 8192,
    "largest_output": 1024,
}


def changed(entry: Mapping[str, object], changes: Mapping[str, object]) -> dict[str, object]:
    """The entry with each change applied: a field set to a value, or left out."""
    return {field: value for field, value in {**entry, **changes}.items() if value is not ABSENT}


def test_coursework_entry_parses_to_section_4_2() -> None:
    (pool,) = parse_pools({"coursework": COURSEWORK})
    assert pool.name == "coursework"
    assert (pool.engine, pool.model, pool.revision) == ("vllm", "Qwen/Qwen3.8-27B-FP8", REVISION)
    assert pool.instance_types == ("g7e.2xlarge",)
    assert (pool.max_instances, pool.warm_instances, pool.wake) == (2, 2, "on-demand")
    assert pool.target_concurrency == 8
    assert (pool.hold_seconds, pool.idle_minutes, pool.drain_seconds) == (420, 20, 300)
    assert pool.gpu_hours_per_day == 16
    assert (pool.context_length, pool.largest_output) == (262144, 32000)


def test_defaults_of_section_17() -> None:
    # Given other values, the three fields are read as given: 420, 20 and 300 are defaults.
    given = changed(COURSEWORK, {"hold_seconds": 600, "idle_minutes": 5, "drain_seconds": 0})
    (pool,) = parse_pools({"coursework": given})
    assert (pool.hold_seconds, pool.idle_minutes, pool.drain_seconds) == (600, 5, 0)

    left_out = changed(COURSEWORK, {"hold_seconds": ABSENT, "idle_minutes": ABSENT, "drain_seconds": ABSENT})
    (pool,) = parse_pools({"coursework": left_out})
    assert (pool.hold_seconds, pool.idle_minutes, pool.drain_seconds) == (420, 20, 300)
    assert pool.hold_seconds_non_streaming == 240


def test_optional_fields() -> None:
    measured = changed(MOCK, {"measured_start_seconds": 95})
    mock, second = parse_pools({"mock": changed(MOCK, {"hold_seconds_non_streaming": 120}), "mock-2": measured})
    assert (mock.name, mock.engine, second.name) == ("mock", "mock", "mock-2")
    assert (mock.model, mock.revision, mock.gpu_hours_per_day, mock.measured_start_seconds) == (None,) * 4
    assert mock.hold_seconds_non_streaming == 120
    assert (mock.context_length, mock.largest_output) == (8192, 1024)
    assert second.measured_start_seconds == 95
    assert second.hold_seconds_non_streaming == 240


@pytest.mark.parametrize(
    ("name", "changes", "field"),
    [
        pytest.param("coursework", {"engine": "sglang"}, "engine", id="engine-sglang"),
        pytest.param("qwen/qwen3-8-27b", {}, "name", id="name-with-a-slash"),
        pytest.param("Coursework", {}, "name", id="name-with-an-upper-case-letter"),
        pytest.param("course_work", {}, "name", id="name-with-an-underscore"),
        pytest.param("", {}, "name", id="name-empty"),
        pytest.param("coursework", {"warm_instances": 3}, "warm_instances", id="warm-above-max"),
        pytest.param("coursework", {"warm_instances": -1}, "warm_instances", id="warm-below-0"),
        pytest.param("coursework", {"prewarm_minutes": 60}, "prewarm_minutes", id="unknown-field"),
        pytest.param("coursework", {"revision": "main"}, "revision", id="revision-main"),
        pytest.param("coursework", {"revision": "g" * 40}, "revision", id="revision-not-hexadecimal"),
        pytest.param("coursework", {"revision": REVISION[:39]}, "revision", id="revision-39-characters"),
        pytest.param("coursework", {"revision": ABSENT}, "revision", id="vllm-without-revision"),
        pytest.param("coursework", {"model": ABSENT}, "model", id="vllm-without-model"),
        pytest.param("coursework", {"gpu_hours_per_day": ABSENT}, "gpu_hours_per_day", id="vllm-without-gpu-hours"),
        pytest.param("coursework", {"wake": "always"}, "wake", id="wake-unknown"),
        pytest.param("coursework", {"max_instances": 0, "warm_instances": 0}, "max_instances", id="max-below-1"),
        pytest.param("coursework", {"target_concurrency": 0}, "target_concurrency", id="concurrency-below-1"),
        pytest.param("coursework", {"context_length": 0}, "context_length", id="context-below-1"),
        pytest.param("coursework", {"largest_output": 0}, "largest_output", id="output-below-1"),
        pytest.param("coursework", {"hold_seconds": 0}, "hold_seconds", id="hold-below-1"),
        pytest.param(
            "coursework", {"hold_seconds_non_streaming": 0}, "hold_seconds_non_streaming", id="other-hold-below-1"
        ),
        # No field but the four of section 17 has a default: left out, it is refused.
        *(
            pytest.param("coursework", {field: ABSENT}, field, id=f"without-{field}")
            for field in (
                "engine",
                "instance_types",
                "max_instances",
                "warm_instances",
                "wake",
                "target_concurrency",
                "context_length",
                "largest_output",
            )
        ),
        # The pools are typed: a number is a whole number, not its text and not a boolean.
        pytest.param("coursework", {"max_instances": "2"}, "max_instances", id="number-as-text"),
        pytest.param("coursework", {"target_concurrency": True}, "target_concurrency", id="number-as-boolean"),
    ],
)
def test_refusals(name: str, changes: dict[str, object], field: str) -> None:
    with pytest.raises(PoolConfigError) as refusal:
        parse_pools({name: changed(COURSEWORK, changes)})
    assert (refusal.value.pool, refusal.value.field) == (name, field)
    assert repr(name) in str(refusal.value)
    assert repr(field) in str(refusal.value)
