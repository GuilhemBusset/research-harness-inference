"""The app's configuration, read through one loader that fails closed.

Nothing that identifies the account is in git (system design, section 10), so the values only the
owner can supply come from a source the environment variable `RHI_CONFIG` names:

- `example`: the committed `config.example.toml`, which every test and every agent uses;
- `file`: a TOML file git does not track, `config.local.toml` at the repository root or the file
  `RHI_CONFIG_FILE` names. The pipeline's synth step writes it from SSM, outside its checkout;
- `ssm`: the parameters the owner wrote to SSM, read with the caller's credentials and region.

An unset `RHI_CONFIG`, any other source, a source that cannot be read, an unknown key, a missing
key that has no default and a value of the wrong type all raise `ConfigError`. Nothing falls back
to the example. An error names a key, a parameter or a source and never a value: build logs show
it.

**The configuration in SSM.** One String parameter per key, named `SSM_PREFIX` followed by the
key, and one per entry of the pools table, named `SSM_POOLS_PREFIX` followed by the entry: an
empty table has none. A string is stored as it is, a number in digits, a boolean as `true` or
`false` (`True` and `False` are read too), a date as YYYY-MM-DD, an empty list as `[]` and a
table as a TOML inline table (`{ issuer = 100 }`). The prefix holds the configuration and
nothing else. The source reads the prefix, in one read by path, and nothing outside it, and refuses a parameter under it that names
no key, like an unknown key. No value is a credential (system design, section 10), so none is a
SecureString. The module names no region and no account.

`write_ssm` and `write_file` write a configuration back out, in the form the ssm source and the
file source read: each source loads what its writer wrote as the same object. The AWS SDK is
imported by the ssm source and by `write_ssm` only, when one of them runs.

The region and the account are not configuration. They come from the deploying credentials
(assumption A4), so no key holds either.

**Adding a key.** The keys below are the ones the owner writes once, and the only required ones:
`tests/unit/test_config.py` holds that set. A key added later has an entry in `DEFAULTS` or is
named in `OPTIONAL`, so that a merge never needs a value the owner's deployed configuration lacks.
It is added to `Config`, to `KEYS` and to `config.example.toml` together.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tomllib
import zoneinfo
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_FILE = "config.example.toml"
LOCAL_FILE = "config.local.toml"

# Where the configuration is in SSM: one parameter per key under the first, one per entry of the
# pools table under the second. Other parameters of the system live beside the first, never below.
SSM_PREFIX = "/rhi/config/"
SSM_POOLS_PREFIX = SSM_PREFIX + "pools/"


class ConfigError(Exception):
    """The configuration was refused. The message names a key or a source, never a value."""


@dataclass(frozen=True)
class RateLimits:
    """The web ACL's rate limits, in requests per 5 minutes per source address (system design,
    sections 6.5 and 17): every request, the issuer's under `/auth/`, Grafana's under `/grafana/`."""

    per_address: int
    issuer: int
    grafana: int


@dataclass(frozen=True)
class Config:
    """The typed configuration. One field per key of `KEYS`, under the same name."""

    # The owner's domain and the ID of its hosted zone: no lookup may find the zone.
    domain: str
    hosted_zone_id: str
    # Cognito's one admin user, and the subscriber of the owner's SNS topics.
    admin_email: str
    # The class-day notice also goes to the instructor's phone.
    instructor_phone: str
    # The pipeline's source.
    github_owner: str
    github_repo: str
    # The login app's client ID, which the issuer serves.
    login_client_id: str
    # The backstop that latches every pool.
    monthly_budget_usd: int
    # The schedule: an IANA time zone name, the course end date and the session windows.
    time_zone: str
    course_end: datetime.date
    session_windows: tuple[()]
    # One entry per pool.
    pools: tuple[()]
    # Whether the public load balancer has its certificate, its HTTPS listener and the domain's
    # alias record. Off until the owner's domain resolves through the hosted zone: before that,
    # the certificate's DNS validation never completes and the deployment fails.
    https_listener: bool
    # The web ACL's three rate limits, read at synthesis only.
    rate_limits: RateLimits


def _as_stored(text: str) -> object:
    return text


@dataclass(frozen=True)
class Key:
    """How one key is read: `parse` returns the typed value or raises ValueError, and `expected`
    is what the error then says the value must be. `from_text` turns the text of the key's SSM
    parameter into the value a TOML file would hold, for `parse` to check; it returns a text that
    is not of the key's form as it is, which `parse` then refuses."""

    expected: str
    parse: Callable[[object], object]
    from_text: Callable[[str], object] = _as_stored


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value


def _whole_dollars(value: object) -> int:
    # A TOML boolean is a Python int: it is not a number of dollars.
    if type(value) is not int or value <= 0:
        raise ValueError
    return value


def _date(value: object) -> datetime.date:
    # A date with a time of day is a subclass of a date: it is refused too.
    if type(value) is not datetime.date:
        raise ValueError
    return value


def _time_zone(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError  # noqa: TRY004 - every refusal of a value is a ValueError, which `parse` turns into a ConfigError
    try:
        zoneinfo.ZoneInfo(value)
    except (LookupError, ValueError, OSError):
        raise ValueError from None  # zoneinfo's own error repeats the name it was given
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _no_entries(value: object) -> tuple[()]:
    # Entries get their shape, and their parser, in the issue that introduces them. Until then
    # an entry is refused, so that no source holds one a later, stricter parser could refuse.
    if not isinstance(value, list) or value:
        raise ValueError
    return ()


# What WAF accepts as the limit of a rate-based rule.
RATE_LIMIT_RANGE = range(10, 2_000_000_001)
# Section 17's rate limits.
_RATE_LIMIT_DEFAULTS: dict[str, int] = {"per_address": 2000, "issuer": 100, "grafana": 300}


def _rate_limits(value: object) -> RateLimits:
    # Each entry of the table is optional: the one left out takes its default.
    if not isinstance(value, dict) or set(value) - set(_RATE_LIMIT_DEFAULTS):
        raise ValueError
    limits = _RATE_LIMIT_DEFAULTS | value
    if any(type(limit) is not int or limit not in RATE_LIMIT_RANGE for limit in limits.values()):
        raise ValueError
    return RateLimits(**limits)


def _number_from_text(text: str) -> object:
    # ASCII digits only, and few enough that `int` accepts them.
    return int(text) if re.fullmatch(r"[0-9]{1,18}", text) else text


def _date_from_text(text: str) -> object:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return text
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return text


def _boolean_from_text(text: str) -> object:
    # TOML's spelling, which the writer uses, or Python's.
    return {"true": True, "True": True, "false": False, "False": False}.get(text, text)


def _list_from_text(text: str) -> object:
    return [] if text == "[]" else text


def _table_from_text(text: str) -> object:
    # A TOML inline table, as `write_ssm` writes it.
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        return text


_STRING = Key("a string that is not empty", _string)
_EMPTY_LIST = Key("an empty list: its entries have no shape yet", _no_entries, _list_from_text)

# Every key a source may hold.
KEYS: dict[str, Key] = {
    "domain": _STRING,
    "hosted_zone_id": _STRING,
    "admin_email": _STRING,
    "instructor_phone": _STRING,
    "github_owner": _STRING,
    "github_repo": _STRING,
    "login_client_id": _STRING,
    "monthly_budget_usd": Key("a whole number of US dollars above zero", _whole_dollars, _number_from_text),
    "time_zone": Key("an IANA time zone name that this host's time zone database knows", _time_zone),
    "course_end": Key("a date, written YYYY-MM-DD without quotes", _date, _date_from_text),
    "session_windows": _EMPTY_LIST,
    "pools": _EMPTY_LIST,
    "https_listener": Key("true or false, without quotes", _boolean, _boolean_from_text),
    "rate_limits": Key(
        "a table whose optional entries per_address, issuer and grafana are whole numbers from 10 to 2000000000",
        _rate_limits,
        _table_from_text,
    ),
}

# The one table of defaults: the value a key takes when a source leaves it out. An entry is the
# default the design states for that setting (most are in section 17) or, where the design gives
# none, the one the issue that added the key chose and stated. It is checked like a value from a
# source. The owner's keys have no entry.
DEFAULTS: dict[str, object] = {
    # The design gives no such key (issue E2-47): off keeps a deployed stage as it was, with no
    # certificate waiting on a domain that does not resolve yet.
    "https_listener": False,
    # Section 17: generous per address, since a campus shares one address; tighter on the issuer
    # and on Grafana.
    "rate_limits": dict(_RATE_LIMIT_DEFAULTS),
}

# Keys a source may leave out without a default: the field is then None.
OPTIONAL: frozenset[str] = frozenset()

# The keys whose values must never appear in the repository. The pipeline's leak check and the
# owner's list of local values look for exactly these. Numbers and dates are not among them: no
# search could find them without noise.
PRIVATE_KEYS: frozenset[str] = frozenset(
    {"domain", "hosted_zone_id", "admin_email", "instructor_phone", "login_client_id"}
)


def required_keys() -> frozenset[str]:
    """The keys every source must give: those with no default that are not optional."""
    return frozenset(KEYS) - DEFAULTS.keys() - OPTIONAL


def parse(data: Mapping[str, object], source: str) -> Config:
    """The typed configuration of what one source holds. `source` is its name, for the errors."""
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise ConfigError(f"the configuration source {source!r} holds an unknown key: {names}")
    values: dict[str, Any] = {}
    for name, key in KEYS.items():
        if name in data:
            origin = f"the configuration source {source!r}"
            raw = data[name]
        elif name in DEFAULTS:
            origin = "the table of defaults"
            raw = DEFAULTS[name]
        elif name in OPTIONAL:
            values[name] = None
            continue
        else:
            raise ConfigError(f"the configuration source {source!r} lacks the key {name!r}, which has no default")
        try:
            values[name] = key.parse(raw)
        except ValueError:
            raise ConfigError(f"the key {name!r} in {origin} must be {key.expected}") from None
    return Config(**values)


def _read(path: Path, source: str, where: str) -> dict[str, Any]:
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"the configuration source {source!r} cannot be read: {where}") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError(f"the configuration source {source!r} is not valid TOML: {where}") from None


def load(environ: Mapping[str, str] | None = None) -> Config:
    """The configuration from the source `RHI_CONFIG` names, read from `environ` (the process's
    environment unless one is given)."""
    env = os.environ if environ is None else environ
    source = env.get("RHI_CONFIG")
    if source is None:
        raise ConfigError("RHI_CONFIG is not set: it names the configuration source, example, file or ssm")
    if source == "example":
        return parse(_read(REPO_ROOT / EXAMPLE_FILE, source, EXAMPLE_FILE), source)
    if source == "file":
        named = env.get("RHI_CONFIG_FILE")
        if named is None:
            return parse(_read(REPO_ROOT / LOCAL_FILE, source, f"{LOCAL_FILE} at the repository root"), source)
        return parse(_read(Path(named), source, "the file RHI_CONFIG_FILE names"), source)
    if source == "ssm":
        return read_ssm()
    raise ConfigError("RHI_CONFIG names an unknown configuration source: it must be example, file or ssm")


def _ssm(client: Any) -> tuple[Any, tuple[type[Exception], ...]]:
    """The SSM client, the caller's or one made with the caller's own credentials and region, and
    the errors a call to it raises. Their messages are AWS's and can hold a value or the account:
    every caller replaces them."""
    # Imported here: only the ssm source and its writer load the SDK.
    import boto3
    import botocore.exceptions

    errors = (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError)
    try:
        return (boto3.client("ssm") if client is None else client), errors
    except errors:
        raise ConfigError("no SSM client can be made: the caller's AWS configuration must give a region") from None


def read_ssm(client: Any = None) -> Config:
    """The configuration the parameters under `SSM_PREFIX` hold: the source `ssm`. `client` is an
    SSM client of the AWS SDK; without one, the caller's credentials and region make it."""
    client, errors = _ssm(client)
    stored: list[Mapping[str, Any]] = []
    request: dict[str, Any] = {"Path": SSM_PREFIX, "Recursive": True}
    try:
        while True:
            page = client.get_parameters_by_path(**request)
            stored += page["Parameters"]
            if not page.get("NextToken"):
                break
            request["NextToken"] = page["NextToken"]
    except errors:
        raise ConfigError(f"the configuration source 'ssm' cannot be read: the parameters under {SSM_PREFIX}") from None

    data: dict[str, object] = {}
    entries: list[object] = []
    stray: list[str] = []
    not_strings: list[str] = []
    for parameter in sorted(stored, key=lambda parameter: parameter["Name"]):
        name: str = parameter["Name"]
        key = name.removeprefix(SSM_PREFIX)
        entry = name.removeprefix(SSM_POOLS_PREFIX)
        is_entry = name.startswith(SSM_POOLS_PREFIX) and entry != "" and "/" not in entry
        # The pools table is a key with no parameter of its own.
        is_key = name.startswith(SSM_PREFIX) and key in KEYS and key != "pools"
        if not is_entry and not is_key:
            stray.append(name)
        elif parameter.get("Type") != "String":
            not_strings.append(name)
        elif is_entry:
            entries.append(parameter["Value"])
        else:
            data[key] = KEYS[key].from_text(parameter["Value"])
    for names, problem in ((stray, "that names no key"), (not_strings, "that is not of type String")):
        if names:
            listed = ", ".join(repr(name) for name in names)
            raise ConfigError(f"the configuration source 'ssm' holds a parameter {problem}: {listed}")
    # The pools table is its entries. `parse` refuses one until entries have a shape.
    data["pools"] = entries
    return parse(data, "ssm")


def _ssm_text(value: object) -> str:
    """The value as the text of its SSM parameter, which `Key.from_text` reads."""
    if isinstance(value, str):
        value.encode()  # a string that is not text is a ValueError here, not in the middle of the writing
        return value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is datetime.date:
        return value.isoformat()
    if isinstance(value, tuple) and not value:
        return "[]"
    if isinstance(value, RateLimits):
        # A TOML inline table, in SSM as in a file.
        return "{ " + ", ".join(f"{name} = {_ssm_text(limit)}" for name, limit in vars(value).items()) + " }"
    raise ValueError


def _toml_text(value: object) -> str:
    """The value as TOML writes it."""
    if isinstance(value, str):
        # A JSON string is a TOML basic string, but for U+007F, which TOML has escaped.
        return json.dumps(_ssm_text(value), ensure_ascii=False).replace("\x7f", "\\u007f")
    return _ssm_text(value)


def _written(
    config: Config, write: Callable[[object], str], read: Callable[[Key, str], object], where: str
) -> dict[str, str]:
    """Each key with its value as `write` writes it. A key whose written value `read` and the
    key's parser do not give back is refused: a writer writes only what its source will load."""
    texts: dict[str, str] = {}
    for name, key in KEYS.items():
        value = getattr(config, name, None)
        if value is None and name in OPTIONAL:
            continue  # the source leaves the key out
        try:
            text = write(value)
            same = key.parse(read(key, text)) == value
        except (ValueError, tomllib.TOMLDecodeError):
            same = False
        if not same:
            raise ConfigError(f"the key {name!r} cannot be written to {where}: it must be {key.expected}")
        texts[name] = text
    return texts


def write_ssm(config: Config, client: Any = None) -> None:
    """Writes the configuration to the parameters the source `ssm` reads, over those that exist.
    It deletes none. `client` is as for `read_ssm`. Nothing is written when a key is refused; a
    parameter SSM refuses stops the writing there."""
    texts = _written(config, _ssm_text, lambda key, text: key.from_text(text), "SSM")
    # The pools table has no parameter of its own: each entry is one, and the table has none.
    del texts["pools"]
    client, errors = _ssm(client)
    for key, text in texts.items():
        name = SSM_PREFIX + key
        try:
            client.put_parameter(Name=name, Value=text, Type="String", Overwrite=True)
        except errors:
            raise ConfigError(f"the parameter {name!r} cannot be written") from None


def write_file(config: Config, path: Path) -> None:
    """Writes the configuration to a TOML file the source `file` reads, over the file if it exists."""
    texts = _written(config, _toml_text, lambda _key, text: tomllib.loads(f"value = {text}")["value"], "a file")
    try:
        path.write_text("".join(f"{key} = {text}\n" for key, text in texts.items()), encoding="utf-8")
    except OSError:
        raise ConfigError("the configuration cannot be written to the file") from None
