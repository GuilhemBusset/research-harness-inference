"""The gateway's settings, read from its environment once, at start (system design, sections 6.2 and 17).

The deployment fills these variables in, and nothing else configures the gateway:

| Variable | Holds | Default |
| --- | --- | --- |
| `RHI_POOLS` | The configuration's pools table, as JSON from each pool's name to its entry (`rhi_common.pool_config`); `{}` with no pool | required |
| `RHI_DEFAULT_POOL` | The name of the pool the `claude-` model names map to | none: left out when the configuration has no default pool |
| `RHI_UPSTREAM_URL` | The address of the internal load balancer's listener, an `http` or `https` URL | required |
| `RHI_TARGET_GROUPS` | Each pool's target group, as JSON from the pool's name to the group's ARN; `{}` with no pool | required |
| `RHI_POOL_SECRETS` | Each pool's secret, as JSON from the pool's name to the secret's ARN; `{}` with no pool | required |
| `RHI_KEYS_TABLE`, `RHI_USAGE_TABLE`, `RHI_HEARTBEAT_TABLE`, `RHI_POOL_STATE_TABLE` | The deployed tables' names | required |
| `RHI_SCALER_FUNCTION` | The scaler function's name | required |
| `RHI_BODY_LIMIT_BYTES` | The largest request body the filter reads, in bytes | 8388608 |
| `RHI_LONGEST_REQUEST_SECONDS` | The longest a request may run | 1800 |
| `RHI_KEEPALIVE_SECONDS` | The interval between the keep-alives of a held request | 10 |
| `RHI_RETRY_AFTER_WARMING`, `RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE`, `RHI_RETRY_AFTER_CONCURRENCY_LIMIT` | The seconds of `Retry-After` for the three causes that carry one | 30, 60, 5 |
| `RHI_TARGET_TTFT_P95_SECONDS`, `RHI_TARGET_STREAM_MEDIAN_TOKENS_PER_SECOND`, `RHI_TARGET_STREAM_P10_TOKENS_PER_SECOND` | The floors of the objectives of section 7.3 (seconds to a first token, tokens a second of a stream), which the gateway's histograms take among their boundaries | 5, 15, 10 |

The defaults are section 17's, and the configuration's (`src/rhi/config.py`) for the keys the
deployment writes into the six numeric variables and the three floors. A numeric variable is a
whole number above zero, in digits; a floor is a number above zero, in digits with at most one
decimal point.

**A floor is a boundary.** The objectives read the share of observations under a floor from the
bucket of that boundary (`rhi_dashboards.objectives`), and a histogram reads a share exactly only
at a boundary. So the waits are counted in `WAIT_BUCKETS` with the floor of the time to first token
among them, and the speed in `SPEED_BUCKETS` with the two floors of a stream
(`rhi_common.boundaries`): a floor the owner revises is read exactly, wherever it falls.

A table's name is one DynamoDB can hold (3 to 255 letters, digits, `_`, `-` and `.`), and the
function's name one Lambda can (1 to 64 letters, digits, `_` and `-`). A target group's ARN is an
ARN of a load balancing target group, and a secret's ARN an ARN of a Secrets Manager secret: one
given for the other is refused, like any text that is no ARN. A value of another form would only
fail later, at the first call that used it.

**No pool is a valid gateway.** The owner's configuration has an empty pools table until its first
pool is added, and a gateway that refused to start would keep its service, and every later
deployment, from a steady state. So `RHI_POOLS`, `RHI_TARGET_GROUPS` and `RHI_POOL_SECRETS` may
each be `{}`, and `RHI_DEFAULT_POOL` may be left out: every model name is then refused with
`request_refused`, a `claude-` name included.

The three tables of pools agree: the target groups and the secrets name exactly the pools of the
pools table, and the default pool, when given, is one of them. Whether an on-demand pool is priced
within the configuration's limit was decided when the configuration loaded, with a limit the
gateway is not given: it is not checked again here.

No variable holds a secret. `RHI_POOL_SECRETS` holds ARNs, and the gateway reads the values
through the Secrets Manager API at start (`rhi_gateway.main`).

**A refusal names the variable and never its value.** A required variable that is missing, or any
variable that is not of its form, raises `SettingsError` before anything listens. Its message is
the variable's name and a fixed sentence; for `RHI_POOLS` it may add the pool field at fault, from
the fixed list of a pool's fields. Nothing else of a value, a pool's name included, reaches the
message or an error chained to it: the task's log shows it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from rhi_common.boundaries import SPEED_BUCKETS, WAIT_BUCKETS, with_floors
from rhi_common.causes import Cause
from rhi_common.pool_config import FIELDS, Pool, PoolConfigError, parse_pools

POOLS = "RHI_POOLS"
DEFAULT_POOL = "RHI_DEFAULT_POOL"
UPSTREAM_URL = "RHI_UPSTREAM_URL"
TARGET_GROUPS = "RHI_TARGET_GROUPS"
POOL_SECRETS = "RHI_POOL_SECRETS"
KEYS_TABLE = "RHI_KEYS_TABLE"
USAGE_TABLE = "RHI_USAGE_TABLE"
HEARTBEAT_TABLE = "RHI_HEARTBEAT_TABLE"
POOL_STATE_TABLE = "RHI_POOL_STATE_TABLE"
SCALER_FUNCTION = "RHI_SCALER_FUNCTION"
BODY_LIMIT_BYTES = "RHI_BODY_LIMIT_BYTES"
LONGEST_REQUEST_SECONDS = "RHI_LONGEST_REQUEST_SECONDS"
KEEPALIVE_SECONDS = "RHI_KEEPALIVE_SECONDS"
RETRY_AFTER_WARMING = "RHI_RETRY_AFTER_WARMING"
RETRY_AFTER_CAPACITY_UNAVAILABLE = "RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE"
RETRY_AFTER_CONCURRENCY_LIMIT = "RHI_RETRY_AFTER_CONCURRENCY_LIMIT"
TARGET_TTFT_P95_SECONDS = "RHI_TARGET_TTFT_P95_SECONDS"
TARGET_STREAM_MEDIAN = "RHI_TARGET_STREAM_MEDIAN_TOKENS_PER_SECOND"
TARGET_STREAM_P10 = "RHI_TARGET_STREAM_P10_TOKENS_PER_SECOND"

# The variables the gateway does not start without.
REQUIRED = (
    POOLS,
    UPSTREAM_URL,
    TARGET_GROUPS,
    POOL_SECRETS,
    KEYS_TABLE,
    USAGE_TABLE,
    HEARTBEAT_TABLE,
    POOL_STATE_TABLE,
    SCALER_FUNCTION,
)

# The numeric variables and what each is when unset (section 17).
NUMERIC_DEFAULTS: Mapping[str, int] = MappingProxyType(
    {
        BODY_LIMIT_BYTES: 8_388_608,
        LONGEST_REQUEST_SECONDS: 1800,
        KEEPALIVE_SECONDS: 10,
        RETRY_AFTER_WARMING: 30,
        RETRY_AFTER_CAPACITY_UNAVAILABLE: 60,
        RETRY_AFTER_CONCURRENCY_LIMIT: 5,
    }
)

# The floors of the objectives and what each is when unset (the configuration's defaults, section 17).
FLOOR_DEFAULTS: Mapping[str, float] = MappingProxyType(
    {TARGET_TTFT_P95_SECONDS: 5.0, TARGET_STREAM_MEDIAN: 15.0, TARGET_STREAM_P10: 10.0}
)

# Every variable the reader reads.
VARIABLES = (*REQUIRED, DEFAULT_POOL, *NUMERIC_DEFAULTS, *FLOOR_DEFAULTS)

# ASCII digits only, and few enough that the number is one a table or a header can hold.
_DIGITS = re.compile(r"[0-9]{1,18}", re.ASCII)
# A floor: digits, with at most one decimal point and digits on both of its sides.
_DECIMAL = re.compile(r"[0-9]{1,32}(?:\.[0-9]{1,32})?", re.ASCII)
# The fields a refusal of the pools table may name: a pool's own, never anything a value held.
_POOL_FIELDS = FIELDS | {"name"}
# The names DynamoDB and Lambda accept, as their APIs document them.
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}", re.ASCII)
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
# An ARN's partition, service, region and account, then the resource of the one kind a variable holds.
_ARN = r"arn:aws(?:-[a-z]+)*:{service}:[a-z0-9-]+:[0-9]{{12}}:{resource}"
_TARGET_GROUP_ARN = re.compile(
    _ARN.format(service="elasticloadbalancing", resource=r"targetgroup/[A-Za-z0-9-]+/[0-9a-f]+"), re.ASCII
)
_SECRET_ARN = re.compile(_ARN.format(service="secretsmanager", resource=r"secret:[A-Za-z0-9/_+=.@-]+"), re.ASCII)


class SettingsError(ValueError):
    """The environment does not describe a gateway that may start. The message names a variable, never a value."""

    def __init__(self, variable: str, problem: str) -> None:
        super().__init__(f"{variable} {problem}")
        self.variable = variable


@dataclass(frozen=True)
class Settings:
    """What the gateway is started with. `default_pool` is one of `pools`, or None.

    `target_groups` and `pool_secrets` hold an ARN under the name of each pool of `pools`, and
    nothing else; `pool_secrets` holds no secret.
    """

    pools: tuple[Pool, ...]
    default_pool: Pool | None
    upstream_url: str
    target_groups: Mapping[str, str]
    pool_secrets: Mapping[str, str]
    keys_table: str
    usage_table: str
    heartbeat_table: str
    pool_state_table: str
    scaler_function: str
    body_limit_bytes: int = NUMERIC_DEFAULTS[BODY_LIMIT_BYTES]
    longest_request_seconds: int = NUMERIC_DEFAULTS[LONGEST_REQUEST_SECONDS]
    keepalive_seconds: int = NUMERIC_DEFAULTS[KEEPALIVE_SECONDS]
    retry_after_warming: int = NUMERIC_DEFAULTS[RETRY_AFTER_WARMING]
    retry_after_capacity_unavailable: int = NUMERIC_DEFAULTS[RETRY_AFTER_CAPACITY_UNAVAILABLE]
    retry_after_concurrency_limit: int = NUMERIC_DEFAULTS[RETRY_AFTER_CONCURRENCY_LIMIT]
    target_ttft_p95_seconds: float = FLOOR_DEFAULTS[TARGET_TTFT_P95_SECONDS]
    target_stream_median_tokens_per_second: float = FLOOR_DEFAULTS[TARGET_STREAM_MEDIAN]
    target_stream_p10_tokens_per_second: float = FLOOR_DEFAULTS[TARGET_STREAM_P10]

    @property
    def wait_buckets(self) -> tuple[float, ...]:
        """The boundaries of the two waits: the gateway's, with the floor of the time to first token."""
        return with_floors(WAIT_BUCKETS, self.target_ttft_p95_seconds)

    @property
    def speed_buckets(self) -> tuple[float, ...]:
        """The boundaries of a stream's speed: the gateway's, with the two floors of a stream."""
        return with_floors(
            SPEED_BUCKETS, self.target_stream_median_tokens_per_second, self.target_stream_p10_tokens_per_second
        )

    @property
    def retry_after(self) -> dict[Cause, int]:
        """The seconds of `Retry-After`, under each cause that carries the header."""
        return {
            Cause.WARMING: self.retry_after_warming,
            Cause.CAPACITY_UNAVAILABLE: self.retry_after_capacity_unavailable,
            Cause.CONCURRENCY_LIMIT: self.retry_after_concurrency_limit,
        }


def _required(environ: Mapping[str, str], variable: str) -> str:
    value = environ.get(variable)
    if value is None:
        raise SettingsError(variable, "is not set: the gateway does not start without it")
    return value


def _table(environ: Mapping[str, str], variable: str) -> str:
    value = _required(environ, variable)
    if _TABLE_NAME.fullmatch(value) is None:
        raise SettingsError(variable, "must be a table's name: 3 to 255 letters, digits, '_', '-' and '.'")
    return value


def _function(environ: Mapping[str, str], variable: str) -> str:
    value = _required(environ, variable)
    if _FUNCTION_NAME.fullmatch(value) is None:
        raise SettingsError(variable, "must be a function's name: 1 to 64 letters, digits, '_' and '-'")
    return value


def _url(environ: Mapping[str, str], variable: str) -> str:
    value = _required(environ, variable)
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.port != 0  # reading the port refuses one that is not a number
            and not (parts.username or parts.password or parts.query or parts.fragment)
            and value.isprintable()
            and not any(character.isspace() for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise SettingsError(variable, "must be an http or https URL with a host, and no query, fragment or user")
    return value


def _json_object(environ: Mapping[str, str], variable: str, expected: str) -> dict[str, object]:
    text = _required(environ, variable)
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        # The parser's own error quotes the text it was given.
        value = None
    if not isinstance(value, dict):
        raise SettingsError(variable, f"must be {expected}")
    return value


def _pools(environ: Mapping[str, str]) -> tuple[Pool, ...]:
    expected = "a JSON object of the configuration's pools, from each pool's name to its entry"
    table = _json_object(environ, POOLS, expected)
    try:
        # The price limit is the configuration's, checked when the stage loaded it.
        return parse_pools(table, on_demand_price_limit=math.inf)
    except PoolConfigError as error:
        # The parser's message names the pool, which is part of the value: only a field of the fixed list is kept.
        field = f" (the pool field {error.field!r} is refused)" if error.field in _POOL_FIELDS else ""
        raise SettingsError(POOLS, f"must be {expected}{field}") from None


def _by_pool(
    environ: Mapping[str, str], variable: str, pools: tuple[Pool, ...], what: str, arn: re.Pattern[str]
) -> Mapping[str, str]:
    expected = f"a JSON object from each pool's name to its {what}, for the pools of {POOLS} and no other"
    table = _json_object(environ, variable, expected)
    if set(table) != {pool.name for pool in pools}:
        raise SettingsError(variable, f"must be {expected}")
    given: dict[str, str] = {}
    for pool in pools:
        value = table[pool.name]
        if not isinstance(value, str) or arn.fullmatch(value) is None:
            raise SettingsError(variable, f"must be {expected}")
        given[pool.name] = value
    return MappingProxyType(given)


def _default_pool(environ: Mapping[str, str], pools: tuple[Pool, ...]) -> Pool | None:
    name = environ.get(DEFAULT_POOL)
    if name is None:
        return None
    for pool in pools:
        if pool.name == name:
            return pool
    raise SettingsError(DEFAULT_POOL, f"must name a pool of {POOLS}, or be left out")


def _number(environ: Mapping[str, str], variable: str) -> int:
    text = environ.get(variable)
    if text is None:
        return NUMERIC_DEFAULTS[variable]
    if _DIGITS.fullmatch(text) is None or int(text) <= 0:
        raise SettingsError(variable, "must be a whole number above zero, in digits")
    return int(text)


def _floor(environ: Mapping[str, str], variable: str) -> float:
    text = environ.get(variable)
    if text is None:
        return FLOOR_DEFAULTS[variable]
    if _DECIMAL.fullmatch(text) is None or float(text) <= 0:
        raise SettingsError(variable, "must be a number above zero, in digits with at most one decimal point")
    return float(text)


def read_settings(environ: Mapping[str, str]) -> Settings:
    """The settings an environment gives, or `SettingsError` for the first variable that is refused.

    `environ` is the process's environment (`os.environ`), or any mapping of the same variables: the
    deployment's test gives the environment of the task it synthesized. Nothing else is read.
    """
    pools = _pools(environ)
    return Settings(
        pools=pools,
        default_pool=_default_pool(environ, pools),
        upstream_url=_url(environ, UPSTREAM_URL),
        target_groups=_by_pool(environ, TARGET_GROUPS, pools, "target group's ARN", _TARGET_GROUP_ARN),
        pool_secrets=_by_pool(environ, POOL_SECRETS, pools, "secret's ARN", _SECRET_ARN),
        keys_table=_table(environ, KEYS_TABLE),
        usage_table=_table(environ, USAGE_TABLE),
        heartbeat_table=_table(environ, HEARTBEAT_TABLE),
        pool_state_table=_table(environ, POOL_STATE_TABLE),
        scaler_function=_function(environ, SCALER_FUNCTION),
        body_limit_bytes=_number(environ, BODY_LIMIT_BYTES),
        longest_request_seconds=_number(environ, LONGEST_REQUEST_SECONDS),
        keepalive_seconds=_number(environ, KEEPALIVE_SECONDS),
        retry_after_warming=_number(environ, RETRY_AFTER_WARMING),
        retry_after_capacity_unavailable=_number(environ, RETRY_AFTER_CAPACITY_UNAVAILABLE),
        retry_after_concurrency_limit=_number(environ, RETRY_AFTER_CONCURRENCY_LIMIT),
        target_ttft_p95_seconds=_floor(environ, TARGET_TTFT_P95_SECONDS),
        target_stream_median_tokens_per_second=_floor(environ, TARGET_STREAM_MEDIAN),
        target_stream_p10_tokens_per_second=_floor(environ, TARGET_STREAM_P10),
    )
