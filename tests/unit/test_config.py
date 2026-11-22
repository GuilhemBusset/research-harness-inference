"""The configuration loader (src/rhi/config.py): its two sources, each way it fails closed, the
table of defaults, the example file, and the set of keys only the owner can supply."""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import tomllib
import traceback
from pathlib import Path

import pytest

from rhi import config
from rhi_common.pool_config import ExpectedValues

EXAMPLE = config.REPO_ROOT / "config.example.toml"

# The keys of the table in the issue that introduced the loader: the values the owner writes to
# SSM once. A key added later has a default or is optional, so this set does not grow.
OWNERS_KEYS = {
    "domain",
    "hosted_zone_id",
    "admin_email",
    "instructor_phone",
    "github_owner",
    "github_repo",
    "login_client_id",
    "monthly_budget_usd",
    "time_zone",
    "course_end",
    "session_windows",
    "pools",
}

# Part of every bad value below, so a test can tell that an error does not repeat the value.
MARK = "7701"

# The gateway's task count and its numeric settings, each at the default of section 17 (the task
# count: sections 2 and 11.2).
GATEWAY_KEYS = {
    "gateway_tasks": 2,
    "request_body_limit_bytes": 8388608,
    "longest_request_seconds": 1800,
    "keepalive_seconds": 10,
    "retry_after_warming_seconds": 30,
    "retry_after_capacity_unavailable_seconds": 60,
    "retry_after_concurrency_limit_seconds": 5,
}

# The provisional targets of section 7.3, each at the default of section 17.
TARGET_KEYS = {
    "target_ttft_p95_seconds": 5,
    "target_stream_median_tokens_per_second": 15,
    "target_stream_p10_tokens_per_second": 10,
    "cached_share_flag_percent": 85,
}


# The pools' tables in the example: from the first pool's header to the end of the file.
POOL_TABLES = re.compile(r"^\[pools\..*", re.MULTILINE | re.DOTALL)


def example_with(**lines: str | None) -> str:
    """The example's text with the value of each named key replaced, or the key removed (None).
    The pools are tables, `[pools.<name>]`, at the end: `pools` removes them all, and a value given
    for it is written after the other keys."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for key, value in lines.items():
        line = "" if value is None else f"{key} = {value}\n"
        if key == "pools":
            text, count = POOL_TABLES.subn("", text)
            text = line + text
        else:
            text, count = re.subn(rf"^{key} = .*\n", line, text, flags=re.MULTILINE)
        assert count == 1, key
    return text


def load_text(folder: Path, text: str) -> config.Config:
    """What the file source gives for a file of this text."""
    path = folder / "source.toml"
    path.write_text(text, encoding="utf-8")
    return config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(path)})


def as_logged(error: BaseException) -> str:
    """The error as a build log shows it: its message, and those of the errors it is chained to."""
    return "".join(traceback.format_exception(error))


def refusal(folder: Path, text: str) -> str:
    """The message of the error the file source raises for a file of this text."""
    with pytest.raises(config.ConfigError) as raised:
        load_text(folder, text)
    assert MARK not in as_logged(raised.value)  # the underlying error could carry a value
    return str(raised.value)


def test_the_two_sources_give_the_same_typed_object(tmp_path: Path) -> None:
    from_example = config.load({"RHI_CONFIG": "example"})
    from_file = load_text(tmp_path, EXAMPLE.read_text(encoding="utf-8"))

    assert from_file == from_example
    for field in dataclasses.fields(config.Config):
        assert type(getattr(from_file, field.name)) is type(getattr(from_example, field.name)), field.name
    assert from_example.domain == "example.com"
    assert type(from_example.monthly_budget_usd) is int
    assert type(from_example.course_end) is datetime.date
    assert from_example.session_windows == ()
    assert [pool.name for pool in from_example.pools] == ["coursework", "mock"]
    # The file source reads its own file, not the example.
    assert load_text(tmp_path, example_with(github_owner='"another-owner"')).github_owner == "another-owner"


def test_the_file_source_defaults_to_config_local_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    (tmp_path / "config.example.toml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    # Nothing falls back to the example, which is there and readable.
    with pytest.raises(config.ConfigError, match=r"'file' cannot be read: config\.local\.toml"):
        config.load({"RHI_CONFIG": "file"})

    (tmp_path / "config.local.toml").write_text(example_with(github_owner='"local-owner"'), encoding="utf-8")
    assert config.load({"RHI_CONFIG": "file"}).github_owner == "local-owner"


@pytest.mark.parametrize(
    "environ", [{}, {"RHI_CONFIG": ""}, {"RHI_CONFIG": "parameter-store"}, {"RHI_CONFIG": f"source{MARK}"}]
)
def test_an_unset_or_unknown_source_raises(environ: dict[str, str]) -> None:
    with pytest.raises(config.ConfigError, match="RHI_CONFIG") as raised:
        config.load(environ)
    assert MARK not in str(raised.value)


def test_the_source_comes_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RHI_CONFIG", raising=False)
    with pytest.raises(config.ConfigError, match="RHI_CONFIG is not set"):
        config.load()
    monkeypatch.setenv("RHI_CONFIG", "example")
    assert config.load() == config.load({"RHI_CONFIG": "example"})


@pytest.mark.parametrize(
    "content",
    [None, f'domain = "unterminated{MARK}'.encode(), f'domain = "{MARK}\xff"'.encode("latin-1")],
    ids=["missing", "not TOML", "not UTF-8"],
)
def test_an_unreadable_source_raises(tmp_path: Path, content: bytes | None) -> None:
    path = tmp_path / f"folder{MARK}" / "source.toml"
    if content is not None:
        path.parent.mkdir()
        path.write_bytes(content)

    with pytest.raises(config.ConfigError, match="'file'") as raised:
        config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(path)})
    assert "RHI_CONFIG_FILE" in str(raised.value)
    assert MARK not in as_logged(raised.value)  # neither the file's content nor its path


@pytest.mark.parametrize("key", ["account", "region", "surprise"])
def test_an_unknown_key_raises(tmp_path: Path, key: str) -> None:
    # No key holds an account or a region: a source that gives one is refused like any unknown key.
    assert key not in config.KEYS
    message = refusal(tmp_path, f'{key} = "value{MARK}"\n' + example_with())
    assert repr(key) in message
    assert "'file'" in message
    assert MARK not in message


@pytest.mark.parametrize("key", sorted(OWNERS_KEYS))
def test_a_missing_key_raises(tmp_path: Path, key: str) -> None:
    message = refusal(tmp_path, example_with(**{key: None}))
    assert repr(key) in message
    assert "'file'" in message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # A table is the wrong type for every key.
        *((key, f'{{ entry = "{MARK}" }}') for key in sorted(OWNERS_KEYS)),
        ("domain", MARK),
        ("domain", '""'),
        ("monthly_budget_usd", f'"{MARK}"'),
        ("monthly_budget_usd", f"{MARK}.5"),
        ("monthly_budget_usd", f"-{MARK}"),
        ("monthly_budget_usd", "true"),
        ("time_zone", MARK),
        ("time_zone", f'"Nowhere/City{MARK}"'),
        ("time_zone", f'"../zone{MARK}"'),
        ("course_end", f'"{MARK}-01-01"'),
        ("course_end", f"{MARK}-01-01T10:00:00"),
        ("session_windows", f'"{MARK}"'),
        ("pools", f'"{MARK}"'),
        ("pools", f"[{MARK}]"),
        ("allowed_instance_types", f'"{MARK}"'),
        ("allowed_instance_types", f"[{MARK}]"),
        ("on_demand_price_limit", f'"{MARK}"'),
        ("on_demand_price_limit", f"-{MARK}"),
        ("on_demand_price_limit", "0"),
        ("on_demand_price_limit", "true"),
        ("default_pool", MARK),
        ("https_listener", f'"{MARK}"'),
        ("https_listener", '"true"'),
        ("https_listener", "1"),
        *(("cost_anomaly_detection", value) for value in (f'"{MARK}"', '"true"', "1")),
        *(("parked", value) for value in (f'"{MARK}"', '"true"', "1")),
        *(("outside_sessions_alarm_hours", value) for value in (f'"{MARK}"', "0", "1.5", f"-{MARK}", "true")),
        *(("gateway_error_alarm_minutes", value) for value in (f'"{MARK}"', "0", "1.5", f"-{MARK}", "true")),
        *(("gateway_error_alarm_percent", value) for value in (f'"{MARK}"', "0", f"100.{MARK}", f"-{MARK}", "true")),
        *(
            (key, value)
            for key in ("issuer_reserved_concurrency", "key_issuances_per_day", *GATEWAY_KEYS)
            for value in (f'"{MARK}"', f"{MARK}.5", f"-{MARK}", "0", "true")
        ),
        *((key, value) for key in TARGET_KEYS for value in (f'"{MARK}"', "0", f"-{MARK}.5", "true", "nan")),
        ("cached_share_flag_percent", f"100.{MARK}"),
        *(("pipeline_events_retention_days", value) for value in ("0", "91", f'"{MARK}"', "90.5", "true")),
        *(("nightly_stop", value) for value in ('"2am"', '"2:00"', '"24:00"', '"02:60"', '"02:00:00"', "2", f'"{MARK}"')),
    ],
)
def test_a_value_of_the_wrong_type_raises(tmp_path: Path, key: str, value: str) -> None:
    message = refusal(tmp_path, example_with(**{key: value}))
    assert repr(key) in message
    assert "'file'" in message
    assert MARK not in message


def test_a_default_fills_a_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = example_with(monthly_budget_usd=None)

    assert "'monthly_budget_usd'" in refusal(tmp_path, text)  # no entry in the table: the key is missing

    monkeypatch.setitem(config.DEFAULTS, "monthly_budget_usd", 4242)
    assert load_text(tmp_path, text).monthly_budget_usd == 4242
    # A source that gives the key keeps its own value.
    assert load_text(tmp_path, example_with(monthly_budget_usd="17")).monthly_budget_usd == 17

    # A default is checked like a value from a source, and the error says where the value was.
    monkeypatch.setitem(config.DEFAULTS, "monthly_budget_usd", f"wrong{MARK}")
    message = refusal(tmp_path, text)
    assert "'monthly_budget_usd'" in message
    assert "table of defaults" in message
    assert MARK not in message


def test_https_listener_reads_false_when_left_out(tmp_path: Path) -> None:
    # The owner's configuration leaves the key out until the domain resolves (issue E2-47).
    assert config.DEFAULTS["https_listener"] is False
    assert load_text(tmp_path, example_with(https_listener=None)).https_listener is False
    # The example turns the listener on, so that synthesis sees it.
    assert config.load({"RHI_CONFIG": "example"}).https_listener is True
    assert load_text(tmp_path, example_with(https_listener="false")).https_listener is False


def test_cost_anomaly_detection_reads_false_when_left_out(tmp_path: Path) -> None:
    # An account may already hold the one monitor of the services dimension (issue E4-36).
    assert load_text(tmp_path, example_with(cost_anomaly_detection=None)).cost_anomaly_detection is False
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["cost_anomaly_detection"] is False
    assert load_text(tmp_path, example_with(cost_anomaly_detection="true")).cost_anomaly_detection is True


def test_outside_sessions_alarm_hours_reads_1_when_left_out(tmp_path: Path) -> None:
    # Section 17: an hour, so that a configuration written before the key loads unchanged.
    assert load_text(tmp_path, example_with(outside_sessions_alarm_hours=None)).outside_sessions_alarm_hours == 1
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["outside_sessions_alarm_hours"] == 1
    assert load_text(tmp_path, example_with(outside_sessions_alarm_hours="3")).outside_sessions_alarm_hours == 3


def test_the_gateway_error_alarm_reads_5_percent_over_5_minutes_when_left_out(tmp_path: Path) -> None:
    # Section 17, so that a configuration written before the keys loads unchanged.
    left_out = load_text(tmp_path, example_with(gateway_error_alarm_percent=None, gateway_error_alarm_minutes=None))
    assert (left_out.gateway_error_alarm_percent, left_out.gateway_error_alarm_minutes) == (5, 5)
    example = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert (example["gateway_error_alarm_percent"], example["gateway_error_alarm_minutes"]) == (5, 5)
    given = load_text(tmp_path, example_with(gateway_error_alarm_percent="2.5", gateway_error_alarm_minutes="10"))
    assert (given.gateway_error_alarm_percent, given.gateway_error_alarm_minutes) == (2.5, 10)


def test_parked_reads_false_when_left_out(tmp_path: Path) -> None:
    # A configuration written before the key loads unchanged, unparked (issue E4-17).
    left_out = load_text(tmp_path, example_with(parked=None))
    assert left_out.parked is False
    assert left_out == load_text(tmp_path, example_with(parked="false"))
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["parked"] is False
    assert load_text(tmp_path, example_with(parked="true")).parked is True


def test_the_issuer_s_two_keys_read_5_when_left_out(tmp_path: Path) -> None:
    # Section 17: the issuer's reserved concurrency and a student's issuances a day are both 5.
    left_out = load_text(tmp_path, example_with(issuer_reserved_concurrency=None, key_issuances_per_day=None))
    assert (left_out.issuer_reserved_concurrency, left_out.key_issuances_per_day) == (5, 5)
    # The example gives each, at its default; a source that gives another value keeps it.
    example = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert (example["issuer_reserved_concurrency"], example["key_issuances_per_day"]) == (5, 5)
    given = load_text(tmp_path, example_with(issuer_reserved_concurrency="2", key_issuances_per_day="3"))
    assert (given.issuer_reserved_concurrency, given.key_issuances_per_day) == (2, 3)


def test_nightly_stop_reads_02_00_when_left_out(tmp_path: Path) -> None:
    # Section 17: the nightly stop is at 02:00; the example gives that, and a given time is kept.
    assert load_text(tmp_path, example_with(nightly_stop=None)).nightly_stop == "02:00"
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["nightly_stop"] == "02:00"
    assert load_text(tmp_path, example_with(nightly_stop='"23:30"')).nightly_stop == "23:30"


def test_the_gateway_s_seven_keys_read_their_defaults_when_left_out(tmp_path: Path) -> None:
    # The owner's deployed configuration has none of them: a source that leaves them out loads.
    left_out = load_text(tmp_path, example_with(**dict.fromkeys(GATEWAY_KEYS)))
    assert {key: getattr(left_out, key) for key in GATEWAY_KEYS} == GATEWAY_KEYS
    assert GATEWAY_KEYS.keys() & config.required_keys() == set()
    # The example gives each, at its default; a source that gives another value keeps it.
    example = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert {key: example.get(key) for key in GATEWAY_KEYS} == GATEWAY_KEYS
    given = load_text(tmp_path, example_with(**{key: str(value + 1) for key, value in GATEWAY_KEYS.items()}))
    assert {key: getattr(given, key) for key in GATEWAY_KEYS} == {key: value + 1 for key, value in GATEWAY_KEYS.items()}


def test_the_provisional_targets_read_the_defaults_of_section_17_when_left_out(tmp_path: Path) -> None:
    # A configuration written before the targets were keys loads, with section 17's values.
    left_out = load_text(tmp_path, example_with(**dict.fromkeys(TARGET_KEYS)))
    assert {key: getattr(left_out, key) for key in TARGET_KEYS} == TARGET_KEYS
    assert TARGET_KEYS.keys() & config.required_keys() == set()
    # The example gives each, at its default; a source that gives another value keeps it.
    example = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert {key: example.get(key) for key in TARGET_KEYS} == TARGET_KEYS
    moved = {
        "target_ttft_p95_seconds": 3.5,
        "target_stream_median_tokens_per_second": 20,
        "target_stream_p10_tokens_per_second": 12.5,
        "cached_share_flag_percent": 90,
    }
    given = load_text(tmp_path, example_with(**{key: str(value) for key, value in moved.items()}))
    assert {key: getattr(given, key) for key in TARGET_KEYS} == moved


def test_pipeline_events_retention_days_reads_90_when_left_out(tmp_path: Path) -> None:
    # A configuration written before the key loads, with the period of the stage's log groups.
    assert load_text(tmp_path, example_with(pipeline_events_retention_days=None)).pipeline_events_retention_days == 90
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["pipeline_events_retention_days"] == 90
    assert load_text(tmp_path, example_with(pipeline_events_retention_days="30")).pipeline_events_retention_days == 30


def test_an_optional_key_may_be_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    later = dataclasses.make_dataclass("Later", [("later_key", str | None)], bases=(config.Config,), frozen=True)
    monkeypatch.setattr(config, "Config", later)
    monkeypatch.setitem(config.KEYS, "later_key", config.KEYS["domain"])

    assert "'later_key'" in refusal(tmp_path, example_with())

    monkeypatch.setattr(config, "OPTIONAL", config.OPTIONAL | {"later_key"})
    assert getattr(load_text(tmp_path, example_with()), "later_key", "unset") is None


# Keys the example sets away from their default on purpose: the listener is off by default and on
# in the example, so that every synthesis and policy test sees it (issue E2-47).
NOT_THE_DEFAULT_IN_THE_EXAMPLE = {"https_listener": True}


def example_problems(text: str) -> list[str]:
    """What is wrong with this text as the example configuration."""
    data = tomllib.loads(text)
    problems = [f"missing key: {key}" for key in sorted(set(config.KEYS) - set(data))]
    problems += [f"unknown key: {key}" for key in sorted(set(data) - set(config.KEYS))]
    expected = config.DEFAULTS | NOT_THE_DEFAULT_IN_THE_EXAMPLE
    problems += [f"not the default: {key}" for key, value in expected.items() if key in data and data[key] != value]
    hosts = [str(data.get("domain", "example.com")), *(host.rstrip(".") for host in re.findall(r"@([\w.-]+)", text))]
    problems += [
        f"outside example.com: {host}" for host in hosts if host != "example.com" and not host.endswith(".example.com")
    ]
    accounts = re.findall(r"(?<!\d)\d{12}(?!\d)", text)
    problems += [f"not the placeholder account: {number}" for number in accounts if number != "123456789012"]
    return problems


def test_the_example_gives_every_key_a_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    assert example_problems(example_with()) == []
    assert "admin@example.com" in example_with()  # the check of addresses has something to read

    assert example_problems(example_with(domain=None)) == ["missing key: domain"]
    assert example_problems(example_with(domain='"example.org"')) == ["outside example.com: example.org"]
    assert example_problems(example_with(domain='"notexample.com"')) == ["outside example.com: notexample.com"]
    assert example_problems(example_with(admin_email='"admin@example.org"')) == ["outside example.com: example.org"]
    assert example_problems(example_with() + "# account 111122223333\n") == ["not the placeholder account: 111122223333"]
    assert example_problems(example_with() + "# account 123456789012\n") == []

    budget = tomllib.loads(example_with())["monthly_budget_usd"]
    monkeypatch.setitem(config.DEFAULTS, "monthly_budget_usd", budget)
    assert example_problems(example_with()) == []
    monkeypatch.setitem(config.DEFAULTS, "monthly_budget_usd", budget + 1)
    assert example_problems(example_with()) == ["not the default: monthly_budget_usd"]


def test_only_the_owners_keys_are_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.required_keys() == OWNERS_KEYS
    assert {field.name for field in dataclasses.fields(config.Config)} == set(config.KEYS)

    # A valid pool entry is accepted, as a table or inline; a session window is not, in either spelling.
    assert [pool.name for pool in load_text(tmp_path, example_with()).pools] == ["coursework", "mock"]
    assert [pool.name for pool in load_text(tmp_path, with_the_mock_pool_alone()).pools] == ["mock"]
    for text in (example_with(session_windows=f'[{{ name = "entry{MARK}" }}]'),
                 example_with(session_windows=None) + f'[[session_windows]]\nname = "entry{MARK}"\n'):
        message = refusal(tmp_path, text)
        assert repr("session_windows") in message
        assert MARK not in message

    # The set is derived: a key added with no default, and not optional, would be required.
    monkeypatch.setitem(config.KEYS, "later_key", config.KEYS["domain"])
    assert config.required_keys() == OWNERS_KEYS | {"later_key"}
    monkeypatch.setitem(config.DEFAULTS, "later_key", "a default")
    assert config.required_keys() == OWNERS_KEYS
    monkeypatch.delitem(config.DEFAULTS, "later_key")
    monkeypatch.setattr(config, "OPTIONAL", config.OPTIONAL | {"later_key"})
    assert config.required_keys() == OWNERS_KEYS


def test_the_module_names_the_private_keys() -> None:
    assert config.PRIVATE_KEYS == {"domain", "hosted_zone_id", "admin_email", "instructor_phone", "login_client_id"}
    assert set(config.KEYS) >= config.PRIVATE_KEYS


def test_rate_limits_read_the_defaults_of_section_17_when_left_out(tmp_path: Path) -> None:
    defaults = config.RateLimits(per_address=2000, issuer=100, grafana=300)
    assert load_text(tmp_path, example_with(rate_limits=None)).rate_limits == defaults
    assert config.load({"RHI_CONFIG": "example"}).rate_limits == defaults
    # Each entry is optional on its own.
    partial = load_text(tmp_path, example_with(rate_limits="{ issuer = 50 }")).rate_limits
    assert partial == config.RateLimits(per_address=2000, issuer=50, grafana=300)
    assert load_text(tmp_path, example_with(rate_limits="{}")).rate_limits == defaults


@pytest.mark.parametrize(
    "value",
    [f'"{MARK}"', MARK, f"{{ issuer = {MARK}.5 }}", "{ grafana = true }", "{ issuer = 9 }",
     f"{{ per_address = 2{MARK}000000 }}", f"{{ surprise{MARK} = 100 }}", f'{{ issuer = "{MARK}" }}'],
)
def test_rate_limits_refuse_what_waf_would_not_take(tmp_path: Path, value: str) -> None:
    message = refusal(tmp_path, example_with(rate_limits=value))
    assert "'rate_limits'" in message
    assert MARK not in message


# The mock pool's lines, as the example gives them (sections 4.2 and 17, evidence E15).
MOCK_LINES = (
    ("engine", '"mock"'),
    ("instance_types", '["t4g.small"]'),
    ("prices", '{ "t4g.small" = 0.02 }'),
    ("wake", '"on-demand"'),
    ("max_instances", "2"),
    ("warm_instances", "1"),
    ("target_concurrency", "8"),
    ("context_length", "262144"),
    ("largest_output", "32000"),
)


def with_a_second_pool(text: str) -> str:
    """The text with one more pool, `second`, the mock's copy."""
    return text + "\n[pools.second]\n" + "".join(f"{field} = {value}\n" for field, value in MOCK_LINES)


def with_the_mock_pool_alone(**lines: str | None) -> str:
    """The example's text with the mock as its only pool, written inline, and `example_with`'s changes.
    The default pool is the mock unless `default_pool` is given."""
    inline = "{ mock = { " + ", ".join(f"{field} = {value}" for field, value in MOCK_LINES) + " } }"
    return example_with(**{"pools": inline, "default_pool": '"mock"', **lines})


def test_the_example_holds_the_mock_pool() -> None:
    loaded = config.load({"RHI_CONFIG": "example"})
    mock = next(pool for pool in loaded.pools if pool.name == "mock")
    assert (mock.name, mock.engine, mock.instance_types) == ("mock", "mock", ("t4g.small",))
    assert mock.prices == {"t4g.small": 0.02}
    assert (mock.wake, mock.max_instances, mock.warm_instances, mock.target_concurrency) == ("on-demand", 2, 1, 8)
    assert (mock.context_length, mock.largest_output) == (262144, 32000)
    assert (mock.expected, mock.measured_start_seconds) == (None, None)
    assert (loaded.allowed_instance_types, loaded.on_demand_price_limit) == (("t4g.small", "g7e.2xlarge"), 5)
    # The mock pool is the one the lines above give, parsed.
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["pools"]["mock"] == {
        field: tomllib.loads(f"v = {value}")["v"] for field, value in MOCK_LINES
    }


def test_the_example_coursework_pool() -> None:
    # The first GPU pool, and the default pool (system design, sections 4.2 and 17).
    loaded = config.load({"RHI_CONFIG": "example"})
    coursework = next(pool for pool in loaded.pools if pool.name == "coursework")
    manifest = json.loads(
        (config.REPO_ROOT / "models/manifests/qwen--qwen3.8-27b-fp8.json").read_text(encoding="utf-8")
    )
    assert (coursework.engine, coursework.model) == ("vllm", "Qwen/Qwen3.8-27B-FP8")
    assert (coursework.model, coursework.revision) == (manifest["repository"], manifest["revision"])
    # One 96 GB GPU, with no fallback type, at its price of evidence E5.
    assert (coursework.instance_types, coursework.prices) == (("g7e.2xlarge",), {"g7e.2xlarge": 3.36})
    assert "g7e.2xlarge" in loaded.allowed_instance_types
    assert (coursework.max_instances, coursework.warm_instances, coursework.wake) == (2, 2, "on-demand")
    assert (coursework.target_concurrency, coursework.gpu_hours_per_day) == (8, 16)
    assert (coursework.hold_seconds, coursework.idle_minutes, coursework.drain_seconds) == (420, 20, 300)
    assert (coursework.context_length, coursework.largest_output) == (262144, 32000)
    # The estimates of section 4.7, and no measured start until spike S1 has one.
    assert coursework.expected == ExpectedValues(
        stream_tokens_per_second=(15, 22), time_to_first_token_seconds=(0.3, 1.5), cache_tokens=(1_100_000, 1_800_000)
    )
    assert coursework.measured_start_seconds is None
    # The serving settings of section 4.2, and no exception for model code.
    assert (coursework.kv_cache_bits, coursework.prefix_caching, coursework.step_budget_tokens) == (8, True, 8192)
    assert (coursework.tool_call_parser, coursework.reasoning_parser) == ("qwen3_coder", "qwen3")
    assert coursework.trust_remote_code is False
    # The request profile of section 6.2, step 4, and the effort mapping of section 17.
    assert coursework.request_profile == ("reasoning_effort", "system_in_place", "document_as_text")
    assert coursework.reasoning_effort == {
        "low": "low",
        "medium": "medium",
        "high": "xhigh",
        "xhigh": "xhigh",
        "max": "xhigh",
    }
    # vLLM's own boundaries put 0.025 alone between 22 and 50 ms, which makes two buckets of that
    # range (section 7.2; evidence E3). Two boundaries or more inside it make three or more.
    assert coursework.histogram_buckets is not None
    boundaries = coursework.histogram_buckets.inter_token_latency or ()
    assert len([boundary for boundary in boundaries if 0.022 < boundary < 0.05]) >= 2
    # The gateway sends `claude-*` model names to the default pool (section 6.2, step 4).
    assert loaded.default_pool == "coursework"


def test_the_three_pool_keys_default_from_the_only_pool(tmp_path: Path) -> None:
    text = with_the_mock_pool_alone(allowed_instance_types=None, on_demand_price_limit=None, default_pool=None)
    loaded = load_text(tmp_path, text)
    assert loaded.default_pool == "mock"
    assert loaded.allowed_instance_types == ("t4g.small",)
    assert loaded.on_demand_price_limit == 5
    assert config.DEFAULTS["on_demand_price_limit"] == 5


def test_an_empty_pools_table_needs_no_new_key(tmp_path: Path) -> None:
    # The deployed configuration until the owner adds the mock pool; `[]` is its earlier spelling.
    for pools in ("{}", "[]"):
        text = example_with(pools=pools, allowed_instance_types=None, on_demand_price_limit=None, default_pool=None)
        loaded = load_text(tmp_path, text)
        assert (loaded.pools, loaded.allowed_instance_types, loaded.default_pool) == ((), (), None)
        assert loaded.on_demand_price_limit == 5


def test_the_keys_an_owner_gives_are_kept(tmp_path: Path) -> None:
    wider = load_text(tmp_path, example_with(allowed_instance_types='["t4g.small", "g7e.2xlarge", "g7e.4xlarge"]'))
    assert wider.allowed_instance_types == ("t4g.small", "g7e.2xlarge", "g7e.4xlarge")
    more = load_text(tmp_path, with_a_second_pool(example_with(default_pool='"second"')))
    assert ([pool.name for pool in more.pools], more.default_pool) == (["coursework", "mock", "second"], "second")
    assert load_text(tmp_path, example_with(on_demand_price_limit="4.5")).on_demand_price_limit == 4.5


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(example_with(default_pool=f'"pool{MARK}"'), "default_pool", id="default-names-no-pool"),
        pytest.param(example_with(pools="{}", default_pool=f'"pool{MARK}"'), "default_pool", id="default-with-no-pool"),
        pytest.param(example_with(default_pool=None), "default_pool", id="two-pools-no-default"),
        pytest.param(
            example_with(allowed_instance_types=f'["t{MARK}.small"]'), "allowed_instance_types", id="not-allowed"
        ),
        pytest.param(example_with(allowed_instance_types="[]"), "allowed_instance_types", id="none-allowed"),
        pytest.param(example_with(on_demand_price_limit="0.01"), "on_demand_price_limit", id="priced-above-limit"),
    ],
)
def test_a_key_read_against_the_pools_is_refused_by_name(tmp_path: Path, text: str, key: str) -> None:
    message = refusal(tmp_path, text)
    assert repr(key) in message
    assert MARK not in message
