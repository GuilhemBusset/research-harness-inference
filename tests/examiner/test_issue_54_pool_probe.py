"""Independent behavioral checks for issue 54's shared pool parser."""

import tomllib
from dataclasses import asdict

import pytest

from rhi_common.pool_config import Pool, PoolConfigError, parse_pools


def coursework():
    return tomllib.loads('''
engine = "vllm"
model = "Qwen/Qwen3.8-27B-FP8"
instance_types = ["g7e.2xlarge"]
prices = { "g7e.2xlarge" = 3.36 }
max_instances = 2
warm_instances = 2
wake = "on-demand"
target_concurrency = 8
context_length = 262144
largest_output = 32000
gpu_hours_per_day = 16
''') | {"revision": "ab" * 20}


@pytest.mark.parametrize("explicit", [False, True])
def test_coursework_and_defaults(explicit):
    entry = coursework()
    defaults = dict(hold_seconds=420, hold_seconds_non_streaming=240, idle_minutes=20, drain_seconds=300)
    if explicit:
        entry.update(defaults)
    (pool,) = parse_pools({"coursework": entry})
    assert isinstance(pool, Pool)
    expected = entry | defaults | dict(name="coursework", measured_start_seconds=None, expected=None)
    expected["instance_types"] = ("g7e.2xlarge",)
    assert asdict(pool) == expected


def test_optional_values_and_overrides():
    entry = coursework() | dict(engine="mock", warm_instances=0, wake="scheduled-only")
    for key in ("model", "revision", "gpu_hours_per_day"):
        del entry[key]
    entry.update(hold_seconds_non_streaming=120, context_length=1024, largest_output=128)
    first, second = parse_pools({"mock-1": entry, "mock-2": entry | dict(measured_start_seconds=93)})
    assert (first.model, first.revision, first.gpu_hours_per_day, first.measured_start_seconds) == (None,) * 4
    assert (first.context_length, first.largest_output, first.hold_seconds_non_streaming) == (1024, 128, 120)
    assert second.measured_start_seconds == 93
    (custom,) = parse_pools({"a-1": coursework() | dict(hold_seconds=1, idle_minutes=7, drain_seconds=12)})
    assert (custom.hold_seconds, custom.idle_minutes, custom.drain_seconds) == (1, 7, 12)


@pytest.mark.parametrize("name", ["a/b", "UPPER", "under_score", "dotted.name", "", "é", "a\n"])
def test_names(name):
    with pytest.raises(PoolConfigError) as caught:
        parse_pools({name: coursework()})
    assert caught.value.pool == name
    assert caught.value.field == "name"
    assert repr(name) in str(caught.value) and "name" in str(caught.value)


@pytest.mark.parametrize("field,value", [
    ("engine", "sglang"), ("wake", "always"), ("warm_instances", -1), ("warm_instances", 3),
    ("max_instances", 0), ("target_concurrency", 0), ("context_length", 0), ("largest_output", 0),
    ("hold_seconds", 0), ("hold_seconds_non_streaming", 0), ("unexpected", 1),
    ("revision", "main"), ("revision", "z" * 40), ("revision", "ab" * 19),
    ("model", ""), ("target_concurrency", True), ("max_instances", "2"),
])
def test_refusals(field, value):
    with pytest.raises(PoolConfigError) as caught:
        parse_pools({"coursework": coursework() | {field: value}})
    assert (caught.value.pool, caught.value.field) == ("coursework", field)
    assert "coursework" in str(caught.value) and field in str(caught.value)


@pytest.mark.parametrize("field", [
    "engine", "instance_types", "max_instances", "warm_instances", "wake", "target_concurrency",
    "context_length", "largest_output", "model", "revision", "gpu_hours_per_day",
])
def test_required_fields_are_not_defaulted(field):
    entry = coursework()
    del entry[field]
    with pytest.raises(PoolConfigError) as caught:
        parse_pools({"coursework": entry})
    assert (caught.value.pool, caught.value.field) == ("coursework", field)
    assert "coursework" in str(caught.value) and field in str(caught.value)
