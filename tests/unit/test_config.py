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


def example_with(**lines: str | None) -> str:
    """The example's text with the value of each named key replaced, or the key removed (None)."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for key, value in lines.items():
        line = "" if value is None else f"{key} = {value}\n"
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
    assert from_example.pools == ()
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


def test_an_optional_key_may_be_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    later = dataclasses.make_dataclass("Later", [("later_key", str | None)], bases=(config.Config,), frozen=True)
    monkeypatch.setattr(config, "Config", later)
    monkeypatch.setitem(config.KEYS, "later_key", config.KEYS["domain"])

    assert "'later_key'" in refusal(tmp_path, example_with())

    monkeypatch.setattr(config, "OPTIONAL", frozenset({"later_key"}))
    assert getattr(load_text(tmp_path, example_with()), "later_key", "unset") is None


def example_problems(text: str) -> list[str]:
    """What is wrong with this text as the example configuration."""
    data = tomllib.loads(text)
    problems = [f"missing key: {key}" for key in sorted(set(config.KEYS) - set(data))]
    problems += [f"unknown key: {key}" for key in sorted(set(data) - set(config.KEYS))]
    problems += [f"not the default: {key}" for key, value in config.DEFAULTS.items() if key in data and data[key] != value]
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

    # The two lists are keys, and neither accepts an entry yet, in either TOML spelling.
    for key in ("session_windows", "pools"):
        for text in (example_with(**{key: f'[{{ name = "entry{MARK}" }}]'}),
                     example_with(**{key: None}) + f'[[{key}]]\nname = "entry{MARK}"\n'):
            message = refusal(tmp_path, text)
            assert repr(key) in message
            assert MARK not in message

    # The set is derived: a key added with no default, and not optional, would be required.
    monkeypatch.setitem(config.KEYS, "later_key", config.KEYS["domain"])
    assert config.required_keys() == OWNERS_KEYS | {"later_key"}
    monkeypatch.setitem(config.DEFAULTS, "later_key", "a default")
    assert config.required_keys() == OWNERS_KEYS
    monkeypatch.delitem(config.DEFAULTS, "later_key")
    monkeypatch.setattr(config, "OPTIONAL", frozenset({"later_key"}))
    assert config.required_keys() == OWNERS_KEYS


def test_the_module_names_the_private_keys() -> None:
    assert config.PRIVATE_KEYS == {"domain", "hosted_zone_id", "admin_email", "instructor_phone", "login_client_id"}
    assert set(config.KEYS) >= config.PRIVATE_KEYS
