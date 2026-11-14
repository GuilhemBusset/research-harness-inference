"""The app's configuration, read through one loader that fails closed.

Nothing that identifies the account is in git (system design, section 10), so the values only the
owner can supply come from a source the environment variable `RHI_CONFIG` names:

- `example`: the committed `config.example.toml`, which every test and every agent uses;
- `file`: a TOML file git does not track, `config.local.toml` at the repository root or the file
  `RHI_CONFIG_FILE` names. The pipeline's synth step writes it from SSM, outside its checkout.

An unset `RHI_CONFIG`, any other source, a source that cannot be read, an unknown key, a missing
key that has no default and a value of the wrong type all raise `ConfigError`. Nothing falls back
to the example. An error names a key or a source and never a value: build logs show it.

The region and the account are not configuration. They come from the deploying credentials
(assumption A4), so no key holds either.

**Adding a key.** The keys below are the ones the owner writes once, and the only required ones:
`tests/unit/test_config.py` holds that set. A key added later has an entry in `DEFAULTS` or is
named in `OPTIONAL`, so that a merge never needs a value the owner's deployed configuration lacks.
It is added to `Config`, to `KEYS` and to `config.example.toml` together.
"""

from __future__ import annotations

import datetime
import os
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


class ConfigError(Exception):
    """The configuration was refused. The message names a key or a source, never a value."""


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


@dataclass(frozen=True)
class Key:
    """How one key is read: `parse` returns the typed value or raises ValueError, and `expected`
    is what the error then says the value must be."""

    expected: str
    parse: Callable[[object], object]


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


def _no_entries(value: object) -> tuple[()]:
    # Entries get their shape, and their parser, in the issue that introduces them. Until then
    # an entry is refused, so that no source holds one a later, stricter parser could refuse.
    if not isinstance(value, list) or value:
        raise ValueError
    return ()


_STRING = Key("a string that is not empty", _string)
_EMPTY_LIST = Key("an empty list: its entries have no shape yet", _no_entries)

# Every key a source may hold.
KEYS: dict[str, Key] = {
    "domain": _STRING,
    "hosted_zone_id": _STRING,
    "admin_email": _STRING,
    "instructor_phone": _STRING,
    "github_owner": _STRING,
    "github_repo": _STRING,
    "login_client_id": _STRING,
    "monthly_budget_usd": Key("a whole number of US dollars above zero", _whole_dollars),
    "time_zone": Key("an IANA time zone name that this host's time zone database knows", _time_zone),
    "course_end": Key("a date, written YYYY-MM-DD without quotes", _date),
    "session_windows": _EMPTY_LIST,
    "pools": _EMPTY_LIST,
}

# The one table of defaults: the value a key takes when a source leaves it out. An entry is the
# default the design states for that setting (most are in section 17) or, where the design gives
# none, the one the issue that added the key chose and stated. It is checked like a value from a
# source. The owner's keys have no entry.
DEFAULTS: dict[str, object] = {}

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
        raise ConfigError("RHI_CONFIG is not set: it names the configuration source, example or file")
    if source == "example":
        return parse(_read(REPO_ROOT / EXAMPLE_FILE, source, EXAMPLE_FILE), source)
    if source == "file":
        named = env.get("RHI_CONFIG_FILE")
        if named is None:
            return parse(_read(REPO_ROOT / LOCAL_FILE, source, f"{LOCAL_FILE} at the repository root"), source)
        return parse(_read(Path(named), source, "the file RHI_CONFIG_FILE names"), source)
    raise ConfigError("RHI_CONFIG names an unknown configuration source: it must be example or file")
