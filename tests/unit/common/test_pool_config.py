"""A pool's entry: the fields of section 4.2, their defaults and their refusals (`rhi_common.pool_config`)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from rhi.config import pool_entry
from rhi_common.pool_config import HistogramBuckets, PoolConfigError, parse_pools
from rhi_common.records import SESSION_PARTITION

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
    "prices": {"g7e.2xlarge": 3.36},
    "max_instances": 2,
    "warm_instances": 2,
    "wake": "on-demand",
    "target_concurrency": 8,
    "hold_seconds": 420,
    "idle_minutes": 20,
    "drain_seconds": 300,
    "context_length": 262144,
    "largest_output": 32000,
}
MOCK = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.0168},
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
    assert (pool.context_length, pool.largest_output) == (262144, 32000)


def test_the_retired_gpu_hours_per_day_is_read_into_nothing() -> None:
    # An entry written before the field was removed loads to the same pool as one without it.
    (without,) = parse_pools({"coursework": COURSEWORK})
    for value in (16, 0):
        (with_hours,) = parse_pools({"coursework": changed(COURSEWORK, {"gpu_hours_per_day": value})})
        assert with_hours == without
    assert not hasattr(without, "gpu_hours_per_day")
    assert "gpu_hours_per_day" not in pool_entry(without)


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
    assert (mock.model, mock.revision, mock.measured_start_seconds) == (None,) * 3
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
        # The retired field is still checked: a whole number not below 0, and nothing else.
        *(
            pytest.param("coursework", {"gpu_hours_per_day": value}, "gpu_hours_per_day", id=f"gpu-hours-{value}")
            for value in (-1, 16.5, True, "16")
        ),
        pytest.param("coursework", {"wake": "always"}, "wake", id="wake-unknown"),
        pytest.param("coursework", {"max_instances": 0, "warm_instances": 0}, "max_instances", id="max-below-1"),
        pytest.param("coursework", {"target_concurrency": 0}, "target_concurrency", id="concurrency-below-1"),
        pytest.param("coursework", {"context_length": 0}, "context_length", id="context-below-1"),
        pytest.param("coursework", {"largest_output": 0}, "largest_output", id="output-below-1"),
        pytest.param("coursework", {"hold_seconds": 0}, "hold_seconds", id="hold-below-1"),
        pytest.param(
            "coursework", {"hold_seconds_non_streaming": 0}, "hold_seconds_non_streaming", id="other-hold-below-1"
        ),
        # None of these fields has a default: left out, it is refused.
        *(
            pytest.param("coursework", {field: ABSENT}, field, id=f"without-{field}")
            for field in (
                "engine",
                "instance_types",
                "prices",
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
        # Trusting a model repository's code is an exception a pool records with its reason.
        pytest.param(
            "coursework", {"trust_remote_code": True}, "trust_remote_code_reason", id="trust-without-a-reason"
        ),
        pytest.param(
            "coursework",
            {"trust_remote_code": True, "trust_remote_code_reason": ""},
            "trust_remote_code_reason",
            id="trust-with-an-empty-reason",
        ),
        pytest.param("coursework", {"trust_remote_code": "true"}, "trust_remote_code", id="trust-as-text"),
        pytest.param("coursework", {"trust_remote_code": 1}, "trust_remote_code", id="trust-as-a-number"),
        # The engine's cache has 8 or 16 bits, as a number.
        *(
            pytest.param("coursework", {"kv_cache_bits": bits}, "kv_cache_bits", id=f"cache-bits-{bits!r}")
            for bits in (4, 12, 32, "8", True)
        ),
        pytest.param("coursework", {"prefix_caching": "false"}, "prefix_caching", id="prefix-caching-as-text"),
        pytest.param("coursework", {"step_budget_tokens": 0}, "step_budget_tokens", id="step-budget-below-1"),
        pytest.param("coursework", {"draft_head_tokens": 0}, "draft_head_tokens", id="draft-head-below-1"),
        pytest.param("coursework", {"draft_head_tokens": True}, "draft_head_tokens", id="draft-head-as-boolean"),
        pytest.param("coursework", {"tool_call_parser": ""}, "tool_call_parser", id="tool-parser-empty"),
        # A parser's name becomes an argument of the engine, which must not read it as an option.
        pytest.param("coursework", {"reasoning_parser": "--an-option"}, "reasoning_parser", id="parser-an-option"),
        *(
            pytest.param("coursework", {"histogram_buckets": buckets}, "histogram_buckets", id=f"buckets-{case}")
            for case, buckets in (
                ("not-a-table", [0.02, 0.05]),
                ("another-family", {"request_tokens": [1, 10]}),
                ("empty", {"inter_token_latency": []}),
                ("not-increasing", {"inter_token_latency": [0.02, 0.05, 0.05]}),
                ("not-above-0", {"time_to_first_token": [0, 1]}),
                ("as-text", {"request_latency": ["1", "5"]}),
            )
        ),
        # A profile's entry is named as a module of the gateway's profile package, and once.
        *(
            pytest.param("coursework", {"request_profile": profile}, "request_profile", id=f"profile-{case}")
            for case, profile in (
                ("not-a-list", "reasoning_effort"),
                ("not-a-module-name", ["Reasoning-Effort"]),
                ("a-dotted-name", ["os.path"]),
                ("not-text", [1]),
                ("an-entry-twice", ["documents", "documents"]),
            )
        ),
        *(
            pytest.param("coursework", {"reasoning_effort": mapping}, "reasoning_effort", id=f"effort-{case}")
            for case, mapping in (
                ("not-a-table", ["high", "xhigh"]),
                ("a-value-not-text", {"high": 3}),
                ("an-empty-value", {"high": ""}),
            )
        ),
        # Student affinity: the load balancer's 1 second to 7 days, a share, and a time of 1 second or more.
        *(
            pytest.param("coursework", {field: value}, field, id=f"{field}-{value!r}")
            for field, values in (
                ("stickiness_seconds", (0, 604801, 3600.0, "3600", True)),
                ("rebalance_share", (-0.1, 1.5, "0.1", True, float("nan"))),
                ("rebalance_interval_seconds", (0, 60.0, "60")),
            )
            for value in values
        ),
        # A cache salt is a student's or the class's: the scope is one of the two words, as text.
        *(
            pytest.param("coursework", {"cache_salt_scope": scope}, "cache_salt_scope", id=f"salt-scope-{scope!r}")
            for scope in ("team", "Class", "", True)
        ),
        # The engine streams the weights or copies them to its disk first: one of the two words.
        *(
            pytest.param("coursework", {"loader": loader}, "loader", id=f"loader-{loader!r}")
            for loader in ("disk", "Download", "", True)
        ),
        # Only a mock pool may give a start delay, even at 0.
        *(
            pytest.param("coursework", {"start_delay_seconds": delay}, "start_delay_seconds", id=f"vllm-delay-{delay}")
            for delay in (400, 0)
        ),
        # A pool's zones are at least one zone name, each named once.
        *(
            pytest.param("coursework", {"zones": zones}, "zones", id=f"zones-{case}")
            for case, zones in (
                ("not-a-list", "example-zone-a"),
                # A list is read and a tuple is held: a tuple is not what a source gives.
                ("a-tuple", ("example-zone-a",)),
                ("empty", []),
                ("not-text", ["example-zone-a", 2]),
                ("a-zone-twice", ["example-zone-a", "example-zone-b", "example-zone-a"]),
                ("a-digit-last", ["example-zone-1"]),
                ("upper-case", ["Example-Zone-A"]),
                ("an-empty-name", [""]),
            )
        ),
    ],
)
def test_refusals(name: str, changes: dict[str, object], field: str) -> None:
    with pytest.raises(PoolConfigError) as refusal:
        parse_pools({name: changed(COURSEWORK, changes)})
    assert (refusal.value.pool, refusal.value.field) == (name, field)
    assert repr(name) in str(refusal.value)
    assert repr(field) in str(refusal.value)


def refused(name: str, entry: Mapping[str, object], **limit: float) -> PoolConfigError:
    with pytest.raises(PoolConfigError) as refusal:
        parse_pools({name: entry}, **limit)
    assert refusal.value.pool == name
    assert repr(name) in str(refusal.value)
    return refusal.value


def test_no_pool_can_take_the_session_switch_s_key() -> None:
    # The switch sits in the pool-state table under this partition key, beside the pools' items.
    assert refused(SESSION_PARTITION, COURSEWORK).field == "name"


def test_prices() -> None:
    fallback = changed(COURSEWORK, {"instance_types": ["g7e.2xlarge", "g7e.4xlarge"]})
    (pool,) = parse_pools({"coursework": changed(fallback, {"prices": {"g7e.4xlarge": 4.5, "g7e.2xlarge": 3.36}})})
    # The type it runs first, then its fallback, whatever order the prices were written in.
    assert list(pool.prices.items()) == [("g7e.2xlarge", 3.36), ("g7e.4xlarge", 4.5)]
    assert parse_pools({"coursework": changed(COURSEWORK, {"prices": {"g7e.2xlarge": 0}})})[0].prices == {
        "g7e.2xlarge": 0
    }

    unpriced = changed(fallback, {"prices": {"g7e.2xlarge": 3.36}})
    unlisted = changed(COURSEWORK, {"prices": {"g7e.2xlarge": 3.36, "g7e.4xlarge": 4.5}})
    for entry in (
        unpriced,
        unlisted,
        *(changed(COURSEWORK, {"prices": {"g7e.2xlarge": bad}}) for bad in (-0.01, "3", True)),
    ):
        refusal = refused("coursework", entry)
        assert refusal.field == "prices"
        assert repr("prices") in str(refusal)


def test_price_limit() -> None:
    def priced(price: float, wake: str, fallback: float | None = None) -> dict[str, object]:
        prices = {"g7e.2xlarge": price} if fallback is None else {"g7e.2xlarge": price, "g7e.4xlarge": fallback}
        return changed(COURSEWORK, {"instance_types": list(prices), "prices": prices, "wake": wake})

    assert parse_pools({"coursework": priced(3.36, "on-demand")})[0].wake == "on-demand"
    assert parse_pools({"coursework": priced(5, "on-demand")})[0].wake == "on-demand"
    assert refused("coursework", priced(16.57, "on-demand")).field == "wake"
    assert parse_pools({"coursework": priced(16.57, "scheduled-only")})[0].wake == "scheduled-only"
    assert refused("coursework", priced(3.36, "on-demand", fallback=6)).field == "wake"
    # A limit given replaces the default.
    assert parse_pools({"coursework": priced(16.57, "on-demand")}, on_demand_price_limit=20)[0].prices
    assert refused("coursework", priced(3.36, "on-demand"), on_demand_price_limit=3).field == "wake"


def test_expected_values() -> None:
    estimate = {
        "stream_tokens_per_second": [15, 22],
        "time_to_first_token_seconds": [0.3, 1.5],
        "cache_tokens": [1100000, 1800000],
    }
    (pool,) = parse_pools({"coursework": changed(COURSEWORK, {"expected": estimate})})
    assert pool.expected is not None
    assert pool.expected.stream_tokens_per_second == (15, 22)
    assert pool.expected.time_to_first_token_seconds == (0.3, 1.5)
    assert pool.expected.cache_tokens == (1_100_000, 1_800_000)

    assert parse_pools({"coursework": COURSEWORK})[0].expected is None
    (partial,) = parse_pools({"coursework": changed(COURSEWORK, {"expected": {"time_to_first_token_seconds": [0, 1]}})})
    assert partial.expected is not None
    assert partial.expected.time_to_first_token_seconds == (0, 1)
    assert (partial.expected.stream_tokens_per_second, partial.expected.cache_tokens) == (None, None)

    for bad in (
        {"stream_tokens_per_second": [22, 15]},
        {"stream_tokens_per_second": [-1, 15]},
        {"cache_tokens": [0, 0]},
        {"cache_tokens": [1, 2, 3]},
        {"cache_tokens": ["1", 2]},
        {"cache_hit_rate": [0.5, 0.9]},
    ):
        refusal = refused("coursework", changed(COURSEWORK, {"expected": bad}))
        assert refusal.field == "expected"
        assert repr("expected") in str(refusal)


def test_trusting_a_model_repositorys_code_is_off_unless_the_pool_records_its_exception() -> None:
    # Left out, as in every entry written before the two fields: false, and no reason.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert pool.trust_remote_code is False
        assert pool.trust_remote_code_reason is None

    exception = {"trust_remote_code": True, "trust_remote_code_reason": "the model's repository holds its layers"}
    (trusted,) = parse_pools({"coursework": changed(COURSEWORK, exception)})
    assert trusted.trust_remote_code is True
    assert trusted.trust_remote_code_reason == "the model's repository holds its layers"

    (explicit,) = parse_pools({"coursework": changed(COURSEWORK, {"trust_remote_code": False})})
    assert (explicit.trust_remote_code, explicit.trust_remote_code_reason) == (False, None)
    # Without its reason the exception is refused, by the pool's name and the field: `test_refusals`.


SERVING = {
    "tool_call_parser": "qwen3_coder",
    "reasoning_parser": "qwen3",
    "kv_cache_bits": 8,
    "prefix_caching": False,
    "step_budget_tokens": 8192,
    "histogram_buckets": {
        "request_latency": [1, 5, 30, 120],
        "time_to_first_token": [0.5, 1, 2.5, 5],
        "inter_token_latency": [0.02, 0.03, 0.04, 0.05],
    },
    "draft_head_tokens": 3,
}


def test_a_vllm_pools_serving_settings_are_optional_and_default_to_what_vllm_does_by_itself() -> None:
    (pool,) = parse_pools({"coursework": changed(COURSEWORK, SERVING)})
    assert (pool.tool_call_parser, pool.reasoning_parser) == ("qwen3_coder", "qwen3")
    assert (pool.kv_cache_bits, pool.prefix_caching) == (8, False)
    assert (pool.step_budget_tokens, pool.draft_head_tokens) == (8192, 3)
    assert pool.histogram_buckets == HistogramBuckets(
        request_latency=(1, 5, 30, 120),
        time_to_first_token=(0.5, 1, 2.5, 5),
        inter_token_latency=(0.02, 0.03, 0.04, 0.05),
    )
    # The entry the deployment writes for the gateway and the scaler reads back the same.
    assert parse_pools({"coursework": pool_entry(pool)}) == (pool,)

    # Both precisions parse as written, and a family the pool leaves out keeps vLLM's boundaries.
    partial = {"kv_cache_bits": 16, "prefix_caching": True, "histogram_buckets": {"inter_token_latency": [0.03]}}
    (pool,) = parse_pools({"coursework": changed(COURSEWORK, partial)})
    assert (pool.kv_cache_bits, pool.prefix_caching) == (16, True)
    assert pool.histogram_buckets == HistogramBuckets(None, None, (0.03,))

    # Left out, as in every entry written before the fields: no parser, 16 bits, prefix caching
    # on, vLLM's own step budget and boundaries, and no draft head.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert (pool.tool_call_parser, pool.reasoning_parser) == (None, None)
        assert (pool.kv_cache_bits, pool.prefix_caching) == (16, True)
        assert (pool.step_budget_tokens, pool.histogram_buckets, pool.draft_head_tokens) == (None, None, None)
    # A precision other than 8 or 16 bits is refused, by the pool's name and the field: `test_refusals`.


def test_a_pools_request_profile_and_effort_mapping_parse_as_written() -> None:
    # Neither name is sorted first: the order is the entry's own.
    profile = ["system_messages", "reasoning_effort", "documents"]
    mapping = {"low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh", "max": "xhigh"}
    (pool,) = parse_pools(
        {"coursework": changed(COURSEWORK, {"request_profile": profile, "reasoning_effort": mapping})}
    )
    assert pool.request_profile == ("system_messages", "reasoning_effort", "documents")
    assert pool.reasoning_effort == mapping
    # The entry the deployment writes for the gateway reads back the same.
    assert parse_pools({"coursework": pool_entry(pool)}) == (pool,)

    # Left out, as in every entry written before the fields: an empty profile and no mapping.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert (pool.request_profile, pool.reasoning_effort) == ((), None)
        assert parse_pools({name: pool_entry(pool)}) == (pool,)


AFFINITY = ("stickiness_seconds", "rebalance_share", "rebalance_interval_seconds")


def affinity(entry: Mapping[str, object], **changes: object) -> tuple[object, ...]:
    """The three settings of student affinity that the entry, with the changes, parses to."""
    (pool,) = parse_pools({"pool": changed(entry, changes)})
    # The entry the deployment writes for the gateway and the scaler reads back the same.
    assert parse_pools({"pool": pool_entry(pool)}) == (pool,)
    return tuple(getattr(pool, field) for field in AFFINITY)


def test_every_pool_has_the_three_settings_of_student_affinity() -> None:
    # Left out, as in every entry written before the fields: the defaults of section 17.
    for entry in (COURSEWORK, MOCK, FIRST_DEPLOYED_MOCK):
        assert affinity(entry) == (3600, 0.1, 60)

    given = {"stickiness_seconds": 900, "rebalance_share": 0.25, "rebalance_interval_seconds": 120}
    assert affinity(COURSEWORK, **given) == (900, 0.25, 120)
    # The limits themselves are accepted: 1 second and 7 days, no student and every student.
    assert affinity(COURSEWORK, stickiness_seconds=1, rebalance_share=0, rebalance_interval_seconds=1) == (1, 0, 1)
    assert affinity(COURSEWORK, stickiness_seconds=604800, rebalance_share=1)[:2] == (604800, 1)
    # Outside them a setting is refused, by the pool's name and the field: `test_refusals`.


def test_a_pools_cache_salt_scope_is_student_unless_it_says_class() -> None:
    # Left out, as in every entry written before the field: a salt for each student.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert pool.cache_salt_scope == "student"

    for scope in ("student", "class"):
        (pool,) = parse_pools({"coursework": changed(COURSEWORK, {"cache_salt_scope": scope})})
        assert pool.cache_salt_scope == scope
        # The entry the deployment writes for the gateway reads back the same.
        assert parse_pools({"coursework": pool_entry(pool)}) == (pool,)
    # Any other scope, such as `team`, is refused by the pool's name and the field: `test_refusals`.


def test_a_vllm_pool_streams_its_weights_unless_it_asks_for_a_download() -> None:
    # Left out, as in every entry written before the field: the streamer.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert pool.loader == "stream"

    for loader in ("stream", "download"):
        (pool,) = parse_pools({"coursework": changed(COURSEWORK, {"loader": loader})})
        assert pool.loader == loader
        # The entry the deployment writes for the gateway reads back the same.
        assert parse_pools({"coursework": pool_entry(pool)}) == (pool,)
    # Any other loader, such as `disk`, is refused by the pool's name and the field: `test_refusals`.


def test_a_mock_pools_start_delay_is_0_unless_it_gives_one() -> None:
    # Left out, as in every entry written before the field: the mock is ready at once.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert pool.start_delay_seconds == 0

    for delay in (0, 400):
        (pool,) = parse_pools({"mock": changed(MOCK, {"start_delay_seconds": delay})})
        assert pool.start_delay_seconds == delay
        # The entry the deployment writes reads back the same.
        assert parse_pools({"mock": pool_entry(pool)}) == (pool,)

    # A negative value, or one that is not a whole number, is refused by the pool's name and the
    # field; so is the field on a vllm pool (`test_refusals`).
    for delay in (-1, 400.0, "400", True):
        refusal = refused("mock", changed(MOCK, {"start_delay_seconds": delay}))
        assert refusal.field == "start_delay_seconds"
        assert repr("start_delay_seconds") in str(refusal)


def test_a_pools_zones_are_none_unless_it_lists_them() -> None:
    # Left out, as in every entry written before the field: the group spans the VPC's first three.
    for name, entry in (("coursework", COURSEWORK), ("mock", MOCK), ("mock", FIRST_DEPLOYED_MOCK)):
        (pool,) = parse_pools({name: entry})
        assert pool.zones is None
        assert "zones" not in pool_entry(pool)

    # Given, they are held in the order written, which no rule here sorts. The module knows no
    # other key: whether a zone is one the VPC spans is the configuration loader's to say.
    for zones in (["example-zone-d", "example-zone-a"], ["example-zone-b"]):
        (pool,) = parse_pools({"coursework": changed(COURSEWORK, {"zones": zones})})
        assert pool.zones == tuple(zones)
        # The entry the deployment writes for the gateway and the scaler reads back the same.
        assert pool_entry(pool)["zones"] == zones
        assert parse_pools({"coursework": pool_entry(pool)}) == (pool,)
    # A list that is empty, or names a zone twice, is refused by the pool's name and the field:
    # `test_refusals`.


# The mock pool's entry as `config.example.toml` gave it when the owner first wrote it to SSM. A
# copy, never read from the example: the deployed entry does not change when the example does.
FIRST_DEPLOYED_MOCK = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.02},
    "wake": "on-demand",
    "max_instances": 2,
    "warm_instances": 1,
    "target_concurrency": 8,
    "context_length": 262144,
    "largest_output": 32000,
}


def test_the_first_deployed_pool_still_parses() -> None:
    # A field added later without a default is missing from this entry, and the parser refuses it.
    (mock,) = parse_pools({"mock": FIRST_DEPLOYED_MOCK})
    assert (mock.engine, mock.instance_types, mock.wake) == ("mock", ("t4g.small",), "on-demand")
    assert mock.prices == {"t4g.small": 0.02}
    assert (mock.max_instances, mock.warm_instances, mock.target_concurrency) == (2, 1, 8)
    assert (mock.context_length, mock.largest_output) == (262144, 32000)
    assert (mock.expected, mock.measured_start_seconds) == (None, None)
