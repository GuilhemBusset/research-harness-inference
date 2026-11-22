"""The gateway's settings (`rhi_gateway.settings`): what its environment gives, and each way it is refused.

The reader is given environments as mappings: nothing here reads the process's own. The ARNs carry
the placeholder account, and the secret a test places in a variable is drawn when the tests run.
"""

from __future__ import annotations

import datetime
import json
import secrets
import traceback

import pytest

from rhi import config
from rhi_common.boundaries import SPEED_BUCKETS, WAIT_BUCKETS
from rhi_common.causes import Cause
from rhi_common.pool_config import parse_pools
from rhi_gateway.settings import Settings, SettingsError, read_settings

MOCK = {
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
POOLS = {"mock": MOCK, "second": {**MOCK, "wake": "scheduled-only", "hold_seconds": 60}}
ACCOUNT = "123456789012"
TARGET_GROUPS = {
    name: f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:targetgroup/{name}/0123456789abcdef" for name in POOLS
}
POOL_SECRETS = {name: f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:pool-{name}-AbCdEf" for name in POOLS}

# A full environment: every variable the gateway reads, the numeric ones away from their defaults.
FULL = {
    "RHI_POOLS": json.dumps(POOLS),
    "RHI_DEFAULT_POOL": "second",
    "RHI_UPSTREAM_URL": "http://internal.example.com:8000",
    "RHI_TARGET_GROUPS": json.dumps(TARGET_GROUPS),
    "RHI_POOL_SECRETS": json.dumps(POOL_SECRETS),
    "RHI_KEYS_TABLE": "keys-of-the-test",
    "RHI_USAGE_TABLE": "usage-of-the-test",
    "RHI_HEARTBEAT_TABLE": "heartbeats-of-the-test",
    "RHI_POOL_STATE_TABLE": "pool-state-of-the-test",
    "RHI_SCALER_FUNCTION": "scaler-of-the-test",
    "RHI_COURSE_END": "2027-01-15",
    "RHI_BODY_LIMIT_BYTES": "1048576",
    "RHI_LONGEST_REQUEST_SECONDS": "900",
    "RHI_KEEPALIVE_SECONDS": "7",
    "RHI_RETRY_AFTER_WARMING": "31",
    "RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE": "61",
    "RHI_RETRY_AFTER_CONCURRENCY_LIMIT": "6",
}
REQUIRED = (
    "RHI_POOLS",
    "RHI_UPSTREAM_URL",
    "RHI_TARGET_GROUPS",
    "RHI_POOL_SECRETS",
    "RHI_KEYS_TABLE",
    "RHI_USAGE_TABLE",
    "RHI_HEARTBEAT_TABLE",
    "RHI_POOL_STATE_TABLE",
    "RHI_SCALER_FUNCTION",
    "RHI_COURSE_END",
)
# Each numeric variable, the field it fills and what the field is when the variable is unset (section 17).
NUMERIC = {
    "RHI_BODY_LIMIT_BYTES": ("body_limit_bytes", 8388608),
    "RHI_LONGEST_REQUEST_SECONDS": ("longest_request_seconds", 1800),
    "RHI_KEEPALIVE_SECONDS": ("keepalive_seconds", 10),
    "RHI_RETRY_AFTER_WARMING": ("retry_after_warming", 30),
    "RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE": ("retry_after_capacity_unavailable", 60),
    "RHI_RETRY_AFTER_CONCURRENCY_LIMIT": ("retry_after_concurrency_limit", 5),
}

# The floors of the objectives, the field each fills and its default (the configuration's, section 17).
FLOORS = {
    "RHI_TARGET_TTFT_P95_SECONDS": ("target_ttft_p95_seconds", 5),
    "RHI_TARGET_STREAM_MEDIAN_TOKENS_PER_SECOND": ("target_stream_median_tokens_per_second", 15),
    "RHI_TARGET_STREAM_P10_TOKENS_PER_SECOND": ("target_stream_p10_tokens_per_second", 10),
}

# A value that must never be shown, drawn now. Its capitals and its underscore keep it from being a
# pool's name, and it is no number.
SECRET = "S_" + secrets.token_hex(16)


def without(*variables: str) -> dict[str, str]:
    return {name: value for name, value in FULL.items() if name not in variables}


def as_logged(error: BaseException) -> str:
    """The error as the task's log shows it: its message, and those of the errors it is chained to."""
    return "".join(traceback.format_exception(error))


def refusal(environ: dict[str, str]) -> SettingsError:
    with pytest.raises(SettingsError) as raised:
        read_settings(environ)
    return raised.value


def test_a_full_environment_gives_the_settings() -> None:
    settings = read_settings(FULL)

    mock, second = parse_pools(POOLS)
    assert settings == Settings(
        pools=(mock, second),
        default_pool=second,
        upstream_url="http://internal.example.com:8000",
        target_groups=TARGET_GROUPS,
        pool_secrets=POOL_SECRETS,
        keys_table="keys-of-the-test",
        usage_table="usage-of-the-test",
        heartbeat_table="heartbeats-of-the-test",
        pool_state_table="pool-state-of-the-test",
        scaler_function="scaler-of-the-test",
        course_end=datetime.date(2027, 1, 15),
        body_limit_bytes=1048576,
        longest_request_seconds=900,
        keepalive_seconds=7,
        retry_after_warming=31,
        retry_after_capacity_unavailable=61,
        retry_after_concurrency_limit=6,
    )
    # The pools are the pool configuration's, with its defaults.
    assert (mock.hold_seconds, second.hold_seconds) == (420, 60)
    assert settings.retry_after == {Cause.WARMING: 31, Cause.CAPACITY_UNAVAILABLE: 61, Cause.CONCURRENCY_LIMIT: 6}
    # A variable the gateway does not read changes nothing.
    assert read_settings({**FULL, "RHI_SOMETHING_ELSE": "1", "PATH": "/usr/bin"}) == settings


def test_the_pools_are_read_as_the_configuration_loaded_them() -> None:
    # What the deployment writes: each pool's entry as the configuration gives it back (`rhi.config.pool_entry`).
    vllm = {
        **MOCK,
        "engine": "vllm",
        "model": "example/model",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "instance_types": ["g7e.2xlarge", "g7e.4xlarge"],
        # Above the default price limit: the configuration's own limit was applied when it loaded.
        "prices": {"g7e.2xlarge": 3.5, "g7e.4xlarge": 7.25},
        "gpu_hours_per_day": 12,
        "measured_start_seconds": 240,
        "expected": {"stream_tokens_per_second": [40, 60.5], "cache_tokens": [0, 200000]},
        # The pool's request profile and its effort mapping reach the gateway with the rest.
        "request_profile": ["second_entry", "first_entry"],
        "reasoning_effort": {"medium": "medium", "high": "xhigh"},
        # And so does the scope of its cache salt.
        "cache_salt_scope": "class",
    }
    loaded = parse_pools({"mock": MOCK, "coursework": vllm}, on_demand_price_limit=10)
    written = json.dumps({pool.name: config.pool_entry(pool) for pool in loaded})

    names = [pool.name for pool in loaded]
    by_pool = {
        "RHI_TARGET_GROUPS": json.dumps(dict.fromkeys(names, TARGET_GROUPS["mock"])),
        "RHI_POOL_SECRETS": json.dumps(dict.fromkeys(names, POOL_SECRETS["mock"])),
    }
    settings = read_settings({**FULL, "RHI_POOLS": written, "RHI_DEFAULT_POOL": "coursework", **by_pool})

    assert settings.pools == loaded
    assert settings.default_pool == loaded[1]
    assert settings.pools[1].request_profile == ("second_entry", "first_entry")
    assert settings.pools[1].reasoning_effort == {"medium": "medium", "high": "xhigh"}
    assert (settings.pools[0].request_profile, settings.pools[0].reasoning_effort) == ((), None)
    assert [pool.cache_salt_scope for pool in settings.pools] == ["student", "class"]


def test_an_environment_with_no_pool_gives_settings_with_no_pool_and_no_default_pool() -> None:
    empty = {"RHI_POOLS": "{}", "RHI_TARGET_GROUPS": "{}", "RHI_POOL_SECRETS": "{}"}
    settings = read_settings({**without("RHI_DEFAULT_POOL"), **empty})

    assert settings.pools == ()
    assert settings.default_pool is None
    assert (settings.target_groups, settings.pool_secrets) == ({}, {})
    # With pools, the default pool is as optional.
    assert read_settings(without("RHI_DEFAULT_POOL")).default_pool is None


@pytest.mark.parametrize("variable", sorted(NUMERIC))
def test_a_numeric_variable_left_unset_gives_its_default(variable: str) -> None:
    field, default = NUMERIC[variable]

    assert getattr(read_settings(without(variable)), field) == default
    # Every other one keeps the value it was given.
    kept = read_settings(without(variable))
    assert all(getattr(kept, name) == int(FULL[other]) for other, (name, _) in NUMERIC.items() if other != variable)


def test_every_numeric_variable_left_unset_gives_the_defaults_of_the_design() -> None:
    settings = read_settings(without(*NUMERIC))

    assert {field: getattr(settings, field) for field, _ in NUMERIC.values()} == dict(NUMERIC.values())
    assert settings.retry_after == {cause: cause.retry_after for cause in Cause if cause.retry_after is not None}


def test_the_floors_are_boundaries_of_the_histograms_they_are_read_in() -> None:
    # Left out, the configuration's defaults, which are boundaries of the gateway's own already.
    default = read_settings(FULL)
    assert {field: getattr(default, field) for field, _ in FLOORS.values()} == dict(FLOORS.values())
    assert (default.wait_buckets, default.speed_buckets) == (WAIT_BUCKETS, SPEED_BUCKETS)
    # Revised between two boundaries, each is one, beside the gateway's own.
    revised = read_settings(
        {**FULL, **dict(zip(FLOORS, ("6", "16.5", "10"), strict=True))}
    )
    assert (revised.target_ttft_p95_seconds, revised.target_stream_median_tokens_per_second) == (6, 16.5)
    assert revised.wait_buckets == tuple(sorted({*WAIT_BUCKETS, 6}))
    assert revised.speed_buckets == tuple(sorted({*SPEED_BUCKETS, 16.5}))


@pytest.mark.parametrize("variable", REQUIRED)
def test_a_required_variable_left_out_raises_an_error_that_names_it(variable: str) -> None:
    error = refusal(without(variable))

    assert error.variable == variable
    assert variable in str(error)
    assert "is not set" in str(error)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        pytest.param("RHI_POOLS", SECRET, id="pools-not-json"),
        pytest.param("RHI_POOLS", json.dumps([SECRET]), id="pools-not-an-object"),
        pytest.param("RHI_POOLS", json.dumps({SECRET: MOCK}), id="pools-a-pool-name"),
        pytest.param("RHI_POOLS", json.dumps({**POOLS, "mock": SECRET}), id="pools-an-entry"),
        pytest.param("RHI_POOLS", json.dumps({**POOLS, "mock": {**MOCK, SECRET: 1}}), id="pools-an-unknown-field"),
        pytest.param("RHI_POOLS", json.dumps({**POOLS, "mock": {**MOCK, "engine": SECRET}}), id="pools-a-field"),
        pytest.param("RHI_DEFAULT_POOL", SECRET, id="default-pool-names-no-pool"),
        pytest.param("RHI_DEFAULT_POOL", "", id="default-pool-empty"),
        pytest.param("RHI_UPSTREAM_URL", SECRET, id="url-no-scheme"),
        pytest.param("RHI_UPSTREAM_URL", f"ftp://{SECRET}.example.com", id="url-another-scheme"),
        pytest.param("RHI_UPSTREAM_URL", f"http://user:{SECRET}@internal.example.com", id="url-a-password"),
        pytest.param("RHI_UPSTREAM_URL", f"http://internal.example.com:{SECRET}", id="url-a-port"),
        pytest.param("RHI_UPSTREAM_URL", f"http://internal.example.com/?key={SECRET}", id="url-a-query"),
        pytest.param("RHI_UPSTREAM_URL", f"http://[{SECRET}", id="url-not-a-url"),
        *(
            pytest.param(variable, value, id=f"{name}-{case}")
            for variable, name in (("RHI_TARGET_GROUPS", "target-groups"), ("RHI_POOL_SECRETS", "pool-secrets"))
            for case, value in (
                ("not-json", SECRET),
                ("not-an-object", json.dumps(SECRET)),
                ("another-pool", json.dumps({**TARGET_GROUPS, SECRET: "arn"})),
                ("a-pool-left-out", json.dumps({"mock": SECRET})),
                ("no-pool", "{}"),
                ("not-text", json.dumps({**TARGET_GROUPS, "mock": {"arn": SECRET}})),
                ("empty", json.dumps({**TARGET_GROUPS, "mock": ""})),
            )
        ),
        *(
            pytest.param(variable, json.dumps({**arns, "mock": value}), id=f"{variable}-{case}")
            for variable, arns, others in (
                ("RHI_TARGET_GROUPS", TARGET_GROUPS, POOL_SECRETS),
                ("RHI_POOL_SECRETS", POOL_SECRETS, TARGET_GROUPS),
            )
            for case, value in (
                ("a-name", SECRET),
                ("a-path", f"{SECRET}/resource-name"),
                ("an-arn-of-the-other-kind", others["mock"]),
                ("an-arn-of-no-account", arns["mock"].replace(ACCOUNT, SECRET)),
                ("an-arn-then-text", f"{arns['mock']} {SECRET}"),
            )
        ),
        *(
            pytest.param(variable, value, id=f"{variable}-{case}")
            for variable in REQUIRED[4:]
            for case, value in (
                ("empty", ""),
                ("a-space", f"{SECRET} {SECRET}"),
                ("a-line", f"{SECRET}\n"),
                ("a-path", f"{SECRET}/resource-name"),
                ("an-arn", POOL_SECRETS["mock"] + SECRET),
            )
        ),
        *(pytest.param(variable, "ab", id=f"{variable}-too-short") for variable in REQUIRED[4:8]),
        pytest.param("RHI_SCALER_FUNCTION", SECRET * 2, id="RHI_SCALER_FUNCTION-too-long"),
        pytest.param("RHI_SCALER_FUNCTION", f"{SECRET}.name", id="RHI_SCALER_FUNCTION-a-dot"),
        *(
            pytest.param("RHI_COURSE_END", value, id=f"RHI_COURSE_END-{case}")
            for case, value in (
                ("no-such-day", "2027-02-30"),
                ("one-digit-month", "2027-1-15"),
                ("another-order", "15/01/2027"),
                ("a-time", "2027-01-15T00:00:00"),
                ("then-text", f"2027-01-15{SECRET}"),
            )
        ),
        *(
            pytest.param(variable, value, id=f"{variable}-{case}")
            for variable in FLOORS
            for case, value in (
                ("text", SECRET),
                ("empty", ""),
                ("zero", "0"),
                ("zero-point-zero", "0.0"),
                ("negative", "-5"),
                ("an-exponent", "1e3"),
                ("a-bare-point", "5."),
                ("spaces", " 10 "),
                ("digits-then-text", f"10{SECRET}"),
            )
        ),
        *(
            pytest.param(variable, value, id=f"{variable}-{case}")
            for variable in NUMERIC
            for case, value in (
                ("text", SECRET),
                ("empty", ""),
                ("zero", "0"),
                ("negative", "-5"),
                ("a-fraction", "1.5"),
                ("an-exponent", "1e3"),
                ("spaces", " 10 "),
                ("digits-then-text", f"10{SECRET}"),
                ("too-long", "9" * 40),
            )
        ),
    ],
)
def test_a_malformed_variable_raises_an_error_that_names_it_and_never_its_value(variable: str, value: str) -> None:
    error = refusal({**FULL, variable: value})

    assert error.variable == variable
    assert str(error).startswith(variable + " ")
    # Neither the message nor an error chained to it repeats the value.
    assert SECRET not in as_logged(error)


def test_the_names_and_arns_a_deployment_can_give_are_accepted() -> None:
    # Refusing a value AWS itself gives would keep the gateway from starting: the forms are AWS's, no narrower.
    partition = f"arn:aws-us-gov:{{}}:us-gov-west-1:{ACCOUNT}:"
    target_group = partition.format("elasticloadbalancing") + "targetgroup/Stage-Pools-MOCK1/0123456789abcdef"
    secret = partition.format("secretsmanager") + "secret:stage/pools/Mock_secret+=.@-AbCdEf"
    given = {
        "RHI_TARGET_GROUPS": json.dumps(dict.fromkeys(POOLS, target_group)),
        "RHI_POOL_SECRETS": json.dumps(dict.fromkeys(POOLS, secret)),
        "RHI_KEYS_TABLE": "Stage-Tables.Keys_1A2B3C",
        "RHI_USAGE_TABLE": "abc",
        "RHI_HEARTBEAT_TABLE": "t" * 255,
        "RHI_POOL_STATE_TABLE": "Stage_pool-state",
        "RHI_SCALER_FUNCTION": "Stage-Scaler_Function1A2B3C",
    }

    settings = read_settings({**FULL, **given})

    assert settings.target_groups == dict.fromkeys(POOLS, target_group)
    assert settings.pool_secrets == dict.fromkeys(POOLS, secret)
    tables = (settings.keys_table, settings.usage_table, settings.heartbeat_table, settings.pool_state_table)
    assert (*tables, settings.scaler_function) == tuple(list(given.values())[2:])
    assert read_settings({**FULL, "RHI_SCALER_FUNCTION": "f" * 64}).scaler_function == "f" * 64


def test_a_refused_pool_field_is_named_from_the_fixed_list_only() -> None:
    # A pool's own field helps whoever reads the log, and is no part of a value.
    known = refusal({**FULL, "RHI_POOLS": json.dumps({**POOLS, "mock": {**MOCK, "largest_output": 0}})})
    assert "'largest_output'" in str(known)
    # The pool's name is part of the value: it is not repeated.
    assert "mock" not in str(known)


def test_the_tables_of_pools_must_agree_with_an_empty_pools_table_too() -> None:
    empty = {**without("RHI_DEFAULT_POOL"), "RHI_POOLS": "{}", "RHI_TARGET_GROUPS": "{}", "RHI_POOL_SECRETS": "{}"}

    # With no pool, a default pool names no pool, and a target group or a secret is of no pool.
    assert refusal({**empty, "RHI_DEFAULT_POOL": "mock"}).variable == "RHI_DEFAULT_POOL"
    assert refusal({**empty, "RHI_TARGET_GROUPS": FULL["RHI_TARGET_GROUPS"]}).variable == "RHI_TARGET_GROUPS"
    assert refusal({**empty, "RHI_POOL_SECRETS": FULL["RHI_POOL_SECRETS"]}).variable == "RHI_POOL_SECRETS"


def test_the_course_end_is_the_date_the_deployment_wrote() -> None:
    assert read_settings({**FULL, "RHI_COURSE_END": "2027-01-15"}).course_end == datetime.date(2027, 1, 15)
