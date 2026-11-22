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
key, and one per entry of the pools table, named `SSM_POOLS_PREFIX` followed by the pool's name:
an empty table has none. A string is stored as it is, a number in digits, a boolean as `true` or
`false` (`True` and `False` are read too), a date as YYYY-MM-DD, a list as a JSON array (an empty
one as `[]`), a pool's entry as a JSON object and the rate limits as a TOML inline table
(`{ issuer = 100 }`). The prefix holds the configuration and nothing else. The source reads the
prefix, in one read by path, and nothing outside it, and refuses a parameter under it that names
no key, like an unknown key. No value is a credential (system design, section 10), so none is a
SecureString. The module names no region and no account.

`write_ssm` and `write_file` write a configuration back out, in the form the ssm source and the
file source read: each source loads what its writer wrote as the same object. The AWS SDK is
imported by the ssm source and by `write_ssm` only, when one of them runs.

**The pools.** The `pools` table maps each pool's name to its entry (`[pools.<name>]` in TOML),
which the pool parser of `rhi_common.pool_config` validates when the configuration loads, with the
configuration's `on_demand_price_limit`. Two keys are then read against the pools:
`allowed_instance_types`, left out, is every instance type the pools list, so that it never needs
a value the deployed configuration lacks; given, a pool that lists another type is refused.
`default_pool`, the pool the gateway maps `claude-*` model names to (sections 5.4 and 6.2), left
out, is the only pool when there is one and none when there is none; with two pools or more it
must be given, and given, it must name a pool.

The region and the account are not configuration. They come from the deploying credentials
(assumption A4), so no key holds either.

**Adding a key.** The keys below are the ones the owner writes once, and the only required ones:
`tests/unit/test_config.py` holds that set. A key added later has an entry in `DEFAULTS` or is
named in `OPTIONAL`, so that a merge never needs a value the owner's deployed configuration lacks.
It is added to `Config`, to `KEYS` and to `config.example.toml` together.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
import os
import re
import tomllib
import zoneinfo
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rhi_common.pool_config import DEFAULT_ON_DEMAND_PRICE_LIMIT, Pool, PoolConfigError, parse_pools

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
    # The monthly amount of the cost budget, which alerts and stops nothing.
    monthly_budget_usd: int
    # The budget's thresholds, in whole percentages of the monthly amount, each a notification on
    # actual cost to the alerts topic (`rhi.guardrails.budget`; sections 11.3 and 17). Only
    # synthesis reads them.
    budget_alert_percentages: tuple[int, ...]
    # The schedule: an IANA time zone name, the course end date and the session windows.
    time_zone: str
    course_end: datetime.date
    session_windows: tuple[()]
    # One entry per pool, parsed by `rhi_common.pool_config`, in the table's order.
    pools: tuple[Pool, ...]
    # The instance types a pool may list: a hard cap (section 11.3).
    allowed_instance_types: tuple[str, ...]
    # Dollars an hour: a pool with an instance type priced above it may not wake on demand (17).
    on_demand_price_limit: float
    # The pool of the `claude-*` model names, or None when no pool is configured (5.4, 6.2).
    default_pool: str | None
    # Whether the public load balancer has its certificate, its HTTPS listener and the domain's
    # alias record. Off until the owner's domain resolves through the hosted zone: before that,
    # the certificate's DNS validation never completes and the deployment fails.
    https_listener: bool
    # The issuer's reserved concurrency, and the most keys a student may be issued over one UTC
    # day, which reaches the issuer as RHI_ISSUANCES_PER_DAY (sections 6.4 and 17).
    issuer_reserved_concurrency: int
    key_issuances_per_day: int
    # The time of day, HH:MM in `time_zone`, at which each pool's group is set to no instance
    # (sections 5.1 and 17). Only synthesis reads it: it reaches no service.
    nightly_stop: str
    # How many gateway tasks the service runs (sections 2 and 11.2). Only the deployment reads it.
    gateway_tasks: int
    # The gateway's numeric settings (section 17), each of which the deployment writes into the
    # variable `services/gateway/rhi_gateway/settings.py` reads it from: the largest request body
    # in bytes, the longest request and the keep-alive interval of a hold in seconds, and the
    # seconds of `Retry-After` for the three causes that carry one.
    request_body_limit_bytes: int
    longest_request_seconds: int
    keepalive_seconds: int
    retry_after_warming_seconds: int
    retry_after_capacity_unavailable_seconds: int
    retry_after_concurrency_limit_seconds: int
    # The web ACL's three rate limits, read at synthesis only.
    rate_limits: RateLimits
    # The provisional targets of section 7.3, held here until spike S5 has measured the pool: the
    # 95th percentile of the time to first token in seconds, the median and the slowest tenth of a
    # stream's tokens a second, and the cached share of input, in percent, below which the
    # dashboard flags a pool. The dashboards read them at synthesis and spike S5's verdict when it
    # loads the configuration; the gateway is given the three floors, which its histograms take
    # among their boundaries (`rhi_gateway.settings`), so that each is read exactly.
    target_ttft_p95_seconds: float
    target_stream_median_tokens_per_second: float
    target_stream_p10_tokens_per_second: float
    cached_share_flag_percent: float
    # The days the pipeline's log group keeps its state changes (`rhi.pipeline.events`), a period
    # CloudWatch Logs takes. Only synthesis of the pipeline stack reads it.
    pipeline_events_retention_days: int
    # Whether the guardrails stack holds a Cost Anomaly Detection monitor of the services dimension
    # and its subscription (`rhi.guardrails.anomaly`). Off until the owner has removed the monitor of
    # that dimension that Cost Explorer may already have made: an account holds only one, and a
    # second fails the deployment. Only synthesis reads it.
    cost_anomaly_detection: bool
    # The whole hours a pool may be in service while no session is on before its alarm fires
    # (`rhi.guardrails.alarms_fleet`; sections 11.3 and 17). Only synthesis reads it.
    outside_sessions_alarm_hours: int
    # The share of the gateway's requests, in percent, that may fail for its own fault over the whole
    # minutes of the period before its alarm fires (`rhi.guardrails.alarms_gateway`; sections 7.3
    # and 17). Only synthesis reads them.
    gateway_error_alarm_percent: float
    gateway_error_alarm_minutes: int
    # Whether the stage is parked: the whole endpoint switched off, with no load balancer, NAT
    # gateway, Fargate service, pool group or scaler, and only data and the pipeline left (system
    # design, sections 5.6 and 11.2). A deployment flag and nothing else: a pool the admin stops is
    # a record (section 11.3). Only synthesis reads it.
    parked: bool


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


# What one AWS::Budgets::Budget can hold: at most 10 notifications, each, as a percentage of the
# budgeted amount, at most 1,000,000 (the CloudFormation resource reference of the notification).
MOST_BUDGET_ALERTS = 10
HIGHEST_BUDGET_ALERT_PERCENTAGE = 1_000_000


def _percentages(value: object) -> tuple[int, ...]:
    # From one to MOST_BUDGET_ALERTS, each a whole number above 0 and at most
    # HIGHEST_BUDGET_ALERT_PERCENTAGE (a TOML boolean is a Python int: it is not one), and no two the
    # same: the budget has one notification at each.
    if not isinstance(value, (list, tuple)) or not 0 < len(value) <= MOST_BUDGET_ALERTS:
        raise ValueError
    if any(type(item) is not int or not 0 < item <= HIGHEST_BUDGET_ALERT_PERCENTAGE for item in value):
        raise ValueError
    if len(set(value)) != len(value):
        raise ValueError
    return tuple(value)


def _whole_number(value: object) -> int:
    # A TOML boolean is a Python int: it is not a count.
    if type(value) is not int or value <= 0:
        raise ValueError
    return value


# The retention periods, in days, that CloudWatch Logs takes for a log group.
LOG_RETENTION_DAYS = frozenset(
    {1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653}
)


def _log_retention_days(value: object) -> int:
    # A TOML boolean is a Python int: it is not a number of days.
    if type(value) is not int or value not in LOG_RETENTION_DAYS:
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


def _time_of_day(value: object) -> str:
    # Hours and minutes, each of two digits, on a 24-hour clock.
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError
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


def _pool_table(value: object) -> Mapping[str, object]:
    # An empty list is read as an empty table: the spelling of the table before pools had a shape.
    if isinstance(value, list) and not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError  # noqa: TRY004 - every refusal of a value is a ValueError, which `parse` turns into a ConfigError
    return value


def _pools(value: object) -> tuple[Pool, ...]:
    # The shape of every entry. The price limit is another key's: `parse` checks it once both are read.
    return parse_pools(_pool_table(value), on_demand_price_limit=math.inf)


def _instance_types(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError
    return tuple(value)


def _dollars_an_hour(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError
    return float(value)


_WHOLE_PERCENT = 100


def _number_above_zero(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError
    return float(value)


def _percent(value: object) -> float:
    # A share is at most the whole.
    number = _number_above_zero(value)
    if number > _WHOLE_PERCENT:
        raise ValueError
    return number


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


def _price_from_text(text: str) -> object:
    return float(text) if re.fullmatch(r"[0-9]{1,18}(\.[0-9]{1,18})?", text) else text


def _table_from_text(text: str) -> object:
    # A TOML inline table, as `write_ssm` writes it.
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        return text


def _json_from_text(text: str) -> object:
    # A text that is not JSON is returned as it is, for the key's parser to refuse.
    try:
        return json.loads(text)
    except ValueError:
        return text


_STRING = Key("a string that is not empty", _string)
_EMPTY_LIST = Key("an empty list: its entries have no shape yet", _no_entries, _list_from_text)
_WHOLE_NUMBER = Key("a whole number above zero", _whole_number, _number_from_text)
_NUMBER_ABOVE_ZERO = Key("a number above zero", _number_above_zero, _price_from_text)

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
    "budget_alert_percentages": Key(
        "a list of 1 to 10 whole percentages from 1 to 1000000, none of them repeated, such as [50, 80, 100]",
        _percentages,
        _json_from_text,
    ),
    "time_zone": Key("an IANA time zone name that this host's time zone database knows", _time_zone),
    "course_end": Key("a date, written YYYY-MM-DD without quotes", _date, _date_from_text),
    "session_windows": _EMPTY_LIST,
    "pools": Key("a table of pools, one entry per pool, each as section 4.2 gives it", _pools, _json_from_text),
    "https_listener": Key("true or false, without quotes", _boolean, _boolean_from_text),
    "allowed_instance_types": Key("a list of instance types, each a string", _instance_types, _json_from_text),
    "on_demand_price_limit": Key("a number of US dollars an hour above zero", _dollars_an_hour, _price_from_text),
    "default_pool": _STRING,
    "issuer_reserved_concurrency": _WHOLE_NUMBER,
    "key_issuances_per_day": _WHOLE_NUMBER,
    "nightly_stop": Key('a time of day in hours and minutes, written "HH:MM" on a 24-hour clock', _time_of_day),
    "gateway_tasks": _WHOLE_NUMBER,
    "request_body_limit_bytes": _WHOLE_NUMBER,
    "longest_request_seconds": _WHOLE_NUMBER,
    "keepalive_seconds": _WHOLE_NUMBER,
    "retry_after_warming_seconds": _WHOLE_NUMBER,
    "retry_after_capacity_unavailable_seconds": _WHOLE_NUMBER,
    "retry_after_concurrency_limit_seconds": _WHOLE_NUMBER,
    "rate_limits": Key(
        "a table whose optional entries per_address, issuer and grafana are whole numbers from 10 to 2000000000",
        _rate_limits,
        _table_from_text,
    ),
    "target_ttft_p95_seconds": _NUMBER_ABOVE_ZERO,
    "target_stream_median_tokens_per_second": _NUMBER_ABOVE_ZERO,
    "target_stream_p10_tokens_per_second": _NUMBER_ABOVE_ZERO,
    "cached_share_flag_percent": Key("a percentage above 0 and at most 100", _percent, _price_from_text),
    "pipeline_events_retention_days": Key(
        "a whole number of days that CloudWatch Logs takes as a retention period, such as 30, 90 or 365",
        _log_retention_days,
        _number_from_text,
    ),
    "cost_anomaly_detection": Key("true or false, without quotes", _boolean, _boolean_from_text),
    "outside_sessions_alarm_hours": _WHOLE_NUMBER,
    "gateway_error_alarm_percent": Key("a percentage above 0 and at most 100", _percent, _price_from_text),
    "gateway_error_alarm_minutes": _WHOLE_NUMBER,
    "parked": Key("true or false, without quotes", _boolean, _boolean_from_text),
}

# The one table of defaults: the value a key takes when a source leaves it out. An entry is the
# default the design states for that setting (most are in section 17) or, where the design gives
# none, the one the issue that added the key chose and stated. It is checked like a value from a
# source. The owner's keys have no entry.
DEFAULTS: dict[str, object] = {
    # Section 17: the budget alerts at 50%, 80% and 100% of the monthly amount. A configuration
    # written before the key (issue E4-44) loads unchanged, with these.
    "budget_alert_percentages": [50, 80, 100],
    # The design gives no such key (issue E2-47): off keeps a deployed stage as it was, with no
    # certificate waiting on a domain that does not resolve yet.
    "https_listener": False,
    # Section 17: 5 dollars an hour.
    "on_demand_price_limit": DEFAULT_ON_DEMAND_PRICE_LIMIT,
    # Section 17: the issuer's reserved concurrency is 5, and so are a student's issuances a day.
    "issuer_reserved_concurrency": 5,
    "key_issuances_per_day": 5,
    # Section 17: the nightly stop is at 02:00 in the configured time zone.
    "nightly_stop": "02:00",
    # Sections 2 and 11.2: the gateway runs two tasks.
    "gateway_tasks": 2,
    # Section 17: a request body of 8 MB at most, a request of 30 minutes at most, a keep-alive
    # every 10 seconds of a hold, and a `Retry-After` of 30 seconds for `warming`, 60 for
    # `capacity_unavailable` and 5 for `concurrency_limit`. The gateway's reader of its environment
    # (`rhi_gateway.settings`) has the same defaults.
    "request_body_limit_bytes": 8_388_608,
    "longest_request_seconds": 1800,
    "keepalive_seconds": 10,
    "retry_after_warming_seconds": 30,
    "retry_after_capacity_unavailable_seconds": 60,
    "retry_after_concurrency_limit_seconds": 5,
    # Section 17: generous per address, since a campus shares one address; tighter on the issuer
    # and on Grafana.
    "rate_limits": dict(_RATE_LIMIT_DEFAULTS),
    # Sections 7.3 and 17, provisional until spike S5: a first token within 5 seconds at the 95th
    # percentile, a median stream of 15 tokens a second and a slowest tenth of 10, and a pool
    # flagged below a cached share of input of 85%.
    "target_ttft_p95_seconds": 5,
    "target_stream_median_tokens_per_second": 15,
    "target_stream_p10_tokens_per_second": 10,
    "cached_share_flag_percent": 85,
    # The design gives no period for the pipeline's events, which hold no personal data (issue
    # E4-30): 90 days, the period of every log group of the stage (section 6.6).
    "pipeline_events_retention_days": 90,
    # The design gives no such key (issue E4-36): off keeps a deployed stage deployable in an
    # account that already holds Cost Explorer's monitor of the services dimension.
    "cost_anomaly_detection": False,
    # Section 17: a pool in service while no session is on raises its alarm after 1 hour.
    "outside_sessions_alarm_hours": 1,
    # Section 17: the gateway's alarm fires when more than 5% of its requests fail for its own fault
    # over 5 minutes.
    "gateway_error_alarm_percent": 5,
    "gateway_error_alarm_minutes": 5,
    # The design gives no default (issue E4-17): unparked, so that a configuration written before
    # the key loads unchanged and builds the stage as it was.
    "parked": False,
}

# Keys a source may leave out without a default: the field is then None. `parse` fills the two
# below from the pools (the module's docstring, "The pools").
OPTIONAL: frozenset[str] = frozenset({"allowed_instance_types", "default_pool"})

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
    # Where each key's value came from, and the value as it came, for the keys read against the pools.
    origins: dict[str, str] = {}
    raws: dict[str, object] = {}
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
        origins[name], raws[name] = origin, raw
        try:
            values[name] = key.parse(raw)
        except ValueError as error:
            # The pool parser's message names the pool and the field, never a value.
            detail = f" ({error})" if isinstance(error, PoolConfigError) else ""
            raise ConfigError(f"the key {name!r} in {origin} must be {key.expected}{detail}") from None
    values |= _read_against_the_pools(values, origins, raws["pools"], source)
    return Config(**values)


def _read_against_the_pools(
    values: Mapping[str, Any], origins: Mapping[str, str], table: object, source: str
) -> dict[str, Any]:
    """The pools parsed again with the configuration's price limit, and the two optional keys
    that are read against them, checked or filled."""
    try:
        pools = parse_pools(_pool_table(table), on_demand_price_limit=values["on_demand_price_limit"])
    except PoolConfigError as error:
        raise ConfigError(
            f"the key 'pools' in {origins['pools']} must hold no pool that wakes on demand above the key "
            f"'on_demand_price_limit' in {origins['on_demand_price_limit']} ({error})"
        ) from None
    allowed = values["allowed_instance_types"]
    if allowed is None:
        allowed = tuple(dict.fromkeys(kind for pool in pools for kind in pool.instance_types))
    for pool in pools:
        if any(kind not in allowed for kind in pool.instance_types):
            raise ConfigError(
                f"the pool {pool.name!r} lists an instance type that the key 'allowed_instance_types' "
                f"in {origins['allowed_instance_types']} does not allow"
            )
    names = [pool.name for pool in pools]
    default = values["default_pool"]
    if default is None and len(names) > 1:
        raise ConfigError(
            f"the configuration source {source!r} lacks the key 'default_pool': "
            "with two pools or more, it must name one"
        )
    if default is None:
        default = names[0] if names else None
    elif default not in names:
        raise ConfigError(f"the key 'default_pool' in {origins['default_pool']} must name a configured pool")
    return {"pools": pools, "allowed_instance_types": allowed, "default_pool": default}


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
    except botocore.exceptions.NoRegionError:
        raise ConfigError("no SSM client can be made: the caller's AWS configuration must give a region") from None
    except errors as error:
        # The class names the cause (a missing dependency, a profile that cannot be read); its
        # text is the SDK's and is not repeated.
        raise ConfigError(f"no SSM client can be made: the AWS SDK raised {type(error).__name__}") from None


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
    entries: dict[str, object] = {}
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
            entries[entry] = _json_from_text(parameter["Value"])
        else:
            data[key] = KEYS[key].from_text(parameter["Value"])
    for names, problem in ((stray, "that names no key"), (not_strings, "that is not of type String")):
        if names:
            listed = ", ".join(repr(name) for name in names)
            raise ConfigError(f"the configuration source 'ssm' holds a parameter {problem}: {listed}")
    # The pools table is its entries, each under the pool's name.
    data["pools"] = entries
    return parse(data, "ssm")


def pool_entry(pool: Pool) -> dict[str, object]:
    """A pool's entry, as the pool parser reads it: every field the pool holds, but its name.

    A start delay of 0 is left out: only a mock pool may give the field, and 0 is its default.
    """
    entry: dict[str, object] = {}
    for field in dataclasses.fields(Pool):
        value = getattr(pool, field.name)
        if field.name == "name" or value is None or (field.name == "start_delay_seconds" and value == 0):
            continue
        if dataclasses.is_dataclass(value):
            value = {key: list(bounds) for key, bounds in dataclasses.asdict(value).items() if bounds is not None}
        entry[field.name] = list(value) if isinstance(value, tuple) else value
    return entry


def _pool_entries(value: object) -> dict[str, dict[str, object]] | None:
    """The table of a tuple of pools, or None when the value is not one."""
    if not isinstance(value, tuple) or not value or not all(isinstance(pool, Pool) for pool in value):
        return None
    return {pool.name: pool_entry(pool) for pool in value}


def _json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    text.encode()  # a string that is not text is a ValueError here, not in the middle of the writing
    return text


def _ssm_text(value: object) -> str:
    """The value as the text of its SSM parameter, which `Key.from_text` reads."""
    if isinstance(value, str):
        value.encode()  # a string that is not text is a ValueError here, not in the middle of the writing
        return value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        # A whole number of dollars as the owner types it: 5, not 5.0.
        return str(int(value)) if value.is_integer() and abs(value) < 1e18 else repr(value)
    if type(value) is datetime.date:
        return value.isoformat()
    if isinstance(value, tuple) and not value:
        return "[]"
    if isinstance(value, RateLimits):
        # A TOML inline table, in SSM as in a file.
        return "{ " + ", ".join(f"{name} = {_ssm_text(limit)}" for name, limit in vars(value).items()) + " }"
    table = _pool_entries(value)
    if table is not None:
        return _json(table)
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return _json(list(value))
    if isinstance(value, tuple) and all(type(item) is int for item in value):
        return _json(list(value))
    raise ValueError


def _toml_value(value: object) -> str:
    """A value of a pool's entry or of a list, as TOML writes it inline."""
    if isinstance(value, str):
        # A JSON string is a TOML basic string, but for U+007F, which TOML has escaped.
        return _json(value).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_value(str(key))} = {_toml_value(item)}" for key, item in value.items()) + " }"
    return _ssm_text(value)


def _toml_text(value: object) -> str:
    """The value as TOML writes it."""
    table = _pool_entries(value)
    return _toml_value(value if table is None else table)


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
    # The pools table has no parameter of its own: each entry is one, under the pool's name.
    del texts["pools"]
    texts |= {f"pools/{pool.name}": _json(pool_entry(pool)) for pool in config.pools}
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
