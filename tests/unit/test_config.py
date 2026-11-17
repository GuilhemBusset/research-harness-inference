"""The configuration loader (src/rhi/config.py): its two sources, each way it fails closed, the
table of defaults, the example file, and the set of keys only the owner can supply."""

from __future__ import annotations

import dataclasses
import datetime
import re
import tomllib
import traceback
from pathlib import Path

import pytest

from rhi import config

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


# A pool's table in the example: its header and its lines up to the next header or the end.
POOL_TABLE = re.compile(r"^\[pools\.[^\]\n]+\]\n(?:[^\[\n].*\n|\n)*", re.MULTILINE)


def example_with(**lines: str | None) -> str:
    """The example's text with the value of each named key replaced, or the key removed (None).
    The pools are tables, `[pools.<name>]`, at the end: `pools` removes them, and a value given
    for it is written after the other keys."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for key, value in lines.items():
        line = "" if value is None else f"{key} = {value}\n"
        if key == "pools":
            text, count = POOL_TABLE.subn("", text)
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
    assert [pool.name for pool in from_example.pools] == ["mock"]
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
        *(
            (key, value)
            for key in ("issuer_reserved_concurrency", "key_issuances_per_day")
            for value in (f'"{MARK}"', f"{MARK}.5", f"-{MARK}", "0", "true")
        ),
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


def test_the_issuer_s_two_keys_read_5_when_left_out(tmp_path: Path) -> None:
    # Section 17: the issuer's reserved concurrency and a student's issuances a day are both 5.
    left_out = load_text(tmp_path, example_with(issuer_reserved_concurrency=None, key_issuances_per_day=None))
    assert (left_out.issuer_reserved_concurrency, left_out.key_issuances_per_day) == (5, 5)
    # The example gives each, at its default; a source that gives another value keeps it.
    example = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert (example["issuer_reserved_concurrency"], example["key_issuances_per_day"]) == (5, 5)
    given = load_text(tmp_path, example_with(issuer_reserved_concurrency="2", key_issuances_per_day="3"))
    assert (given.issuer_reserved_concurrency, given.key_issuances_per_day) == (2, 3)


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
    inline = "{ mock = { " + ", ".join(f"{field} = {value}" for field, value in MOCK_LINES) + " } }"
    for text in (example_with(), example_with(pools=inline)):
        assert [pool.name for pool in load_text(tmp_path, text).pools] == ["mock"]
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
    """The text with a second pool, `second`, the mock's copy."""
    return text + "\n[pools.second]\n" + "".join(f"{field} = {value}\n" for field, value in MOCK_LINES)


def test_the_example_holds_the_mock_pool() -> None:
    loaded = config.load({"RHI_CONFIG": "example"})
    (mock,) = loaded.pools
    assert (mock.name, mock.engine, mock.instance_types) == ("mock", "mock", ("t4g.small",))
    assert mock.prices == {"t4g.small": 0.02}
    assert (mock.wake, mock.max_instances, mock.warm_instances, mock.target_concurrency) == ("on-demand", 2, 1, 8)
    assert (mock.context_length, mock.largest_output) == (262144, 32000)
    assert (mock.expected, mock.measured_start_seconds) == (None, None)
    assert (loaded.allowed_instance_types, loaded.on_demand_price_limit) == (("t4g.small",), 5)
    assert loaded.default_pool == "mock"
    # The pools are those the example's lines give, parsed.
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["pools"] == {
        "mock": {field: tomllib.loads(f"v = {value}")["v"] for field, value in MOCK_LINES}
    }


def test_the_three_pool_keys_default_from_the_only_pool(tmp_path: Path) -> None:
    text = example_with(allowed_instance_types=None, on_demand_price_limit=None, default_pool=None)
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
    wider = load_text(tmp_path, example_with(allowed_instance_types='["t4g.small", "g7e.2xlarge"]'))
    assert wider.allowed_instance_types == ("t4g.small", "g7e.2xlarge")
    two = load_text(tmp_path, with_a_second_pool(example_with(default_pool='"second"')))
    assert ([pool.name for pool in two.pools], two.default_pool) == (["mock", "second"], "second")
    assert load_text(tmp_path, example_with(on_demand_price_limit="0.5")).on_demand_price_limit == 0.5


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(example_with(default_pool=f'"pool{MARK}"'), "default_pool", id="default-names-no-pool"),
        pytest.param(example_with(pools="{}", default_pool=f'"pool{MARK}"'), "default_pool", id="default-with-no-pool"),
        pytest.param(with_a_second_pool(example_with(default_pool=None)), "default_pool", id="two-pools-no-default"),
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
