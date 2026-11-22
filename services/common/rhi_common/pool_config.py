"""A pool's entry in the configuration: its fields, its limits and its defaults.

A pool is one model on one family of instance types (system design, section 4.2). It is declared
as one entry of the configuration's `pools` table and becomes one Auto Scaling group, one target
group and one dashboard row. The constructs, the scaler, the gateway, the lifecycle agent and the
dashboards all read the same entry, so its shape is decided here and nowhere else. The module
needs the standard library only, and knows nothing of where the configuration comes from:
`parse_pools` takes the table as the source gave it.

The entry's key is the pool's name, the model name students choose it by (section 4.2): it holds
only lower-case letters, digits and hyphens. The rule also keeps the session switch's partition key
free (section 5.6; `SESSION_PARTITION` in `rhi_common.records`): the switch is an item of the
pool-state table beside the pools' own items, which sit under the pool's name, and its key starts
with `_`, which no pool's name may hold. A rule that admitted it would let a pool's records and the
switch share one partition.

Seven fields have a default of section 17, which gives them to every pool: the two holds, the idle
time, the drain and the three settings of student affinity, below. `target_concurrency` is a
pool's own value, so every pool gives it. A mock pool serves canned responses from no model
repository: it may leave out `model` and `revision`.

`gpu_hours_per_day` is a retired field (ADR 0003, decision 2): a pool has none, but an entry may
still hold it, as a whole number not below 0, which is read into nothing, so that an entry written
before its removal still loads. Any other value is refused. A later change removes the tolerance
once the deployed entries no longer hold it.

Each pool prices every instance type it lists, in dollars an hour (sections 5.4 and 7.2): the
first listed type is the one it runs, the others are its fallbacks, the next sizes of the same GPU,
which cost more. A fallback is a price the pool can run at, so every listed type is held to the
on-demand price limit: a pool priced above it may only wake on schedule (sections 11.3 and 17).
The limit is the caller's, 5 dollars an hour when none is given.

A pool may give `expected`, the values the inference dashboard draws beside the measured series
(sections 4.7 and 7.3): each of speed per stream, time to first token and cache capacity is
optional, and is a low and a high bound.

A pool may set `trust_remote_code`, which lets its engine run the code its model repository
holds (section 4.4; evidence E28). That is off for every pool that does not ask: a pool that asks
is a reviewed exception, and records why in `trust_remote_code_reason`, without which it is
refused. Left out, the first is false and the second is None.

A pool served by vLLM may give its serving settings, which its release carries into the engine's
flags (sections 4.2 and 4.3; `rhi.pools.vllm_command`). Each is optional, and left out it leaves
the engine as vLLM runs by itself:

- `tool_call_parser` and `reasoning_parser`, vLLM's names for the parsers of the model's tool
  calls and of its reasoning. Left out, each is None: no parser.
- `kv_cache_bits`, the precision of the engine's cache, 8 or 16 bits (section 4.7). Left out, it
  is 16, vLLM's own for the coursework model.
- `prefix_caching`, whether the engine keeps what it computed for a transcript (sections 4.7 and
  5.5). Left out, it is true, vLLM's own default.
- `step_budget_tokens`, the most tokens the engine computes in one step (section 4.7). Left out,
  it is None: vLLM's own budget.
- `histogram_buckets`, the boundaries, in seconds, of the bucket families of `BUCKET_FAMILIES`
  (section 7.2): a table that holds any of the three, each a list of increasing numbers above 0.
  Left out, it is None, and a family left out of it is None: vLLM's own boundaries.
- `draft_head_tokens`, which turns the model's draft head on and is the number of tokens it
  proposes (section 4.7). Left out, it is None: no draft head.
- `loader`, how the engine reads the model's weights (section 4.4): `stream`, with the Run:ai
  streamer straight from S3, or `download`, a copy of the model to the instance's disk before vLLM
  starts, which then loads it with its default loader (evidence E11; spike S4 compares the two).
  Left out, it is `stream`.

The mock engine reads none of them.

A pool may give the gateway its request profile (sections 6.1 and 6.2, step 4), with two fields.
Both are optional, and the design gives no default beyond leaving them out:

- `request_profile`, the names of the profile's entries, in the order the gateway applies them to
  a request's filtered body. Each is the name of a module of `rhi_gateway.profile`, so it holds
  lower-case letters, digits and `_` and starts with a letter; none is named twice. Whether the
  gateway has an entry of that name is the gateway's to say, at its start. Left out, it is empty:
  the pool has no profile.
- `reasoning_effort`, a table from each value of reasoning effort a request may send to the value
  the engine receives. It is configuration because it is also a lever on speed (section 4.7): the
  profile's entry for reasoning effort reads it. Left out, it is None: no mapping.

Every pool has the three settings of student affinity (section 5.5), each with its default of
section 17:

- `stickiness_seconds`, how long the pool's target group keeps a student on the instance that
  served their last request, renewed by every request. From 1 second to 7 days, which is what the
  load balancer accepts (evidence E13). Left out, it is 3600.
- `rebalance_share`, the share of the pool's students that one rebalance number moves, from 0 to
  1. Left out, it is 0.1: about one student in ten.
- `rebalance_interval_seconds`, the least time between two rebalance numbers, of at least 1
  second. Left out, it is 60.

A pool may give `cache_salt_scope`, whose cached prefixes a request on the pool may reuse (sections
4.7, 6.2 and 6.6): the gateway writes a cache salt into every request, and the engine reuses a
prefix only under the salt it was computed for.

- `student`: a salt for each student. A prefix is reused only by the student who sent it, so no
  student can find out whether another has sent a given text. Left out, the field is `student`.
- `class`: one salt for the pool, the same for every student. A class that starts together shares
  whatever part of its opening is identical, and one student can then test whether another has sent
  a given text.

A mock pool may give `start_delay_seconds`, a whole number of seconds during which its engine's
health route answers 503 after the engine starts, so that the gateway holds a request that long
(sections 4.1 and 5.3; spike S2 in section 12). The design gives no such setting: left out, it is
0, the mock ready at once. Its release passes it to the mock as the flag `--start-delay-seconds`
(`rhi.pools.release`). Only a pool whose engine is mock may give it, even at 0.

A refusal is a `PoolConfigError` that names the pool and the field, never a value: build logs
show it.

**Adding a field.** The configuration is written once and loaded on every deployment by a loader
that fails closed. A field added after the first pool was written therefore has a default or is
optional: one that every pool had to give would fail the next deployment. `prices` is required
because it came before the first pool reached the deployed configuration.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

ENGINES = ("mock", "vllm")
WAKES = ("on-demand", "scheduled-only")

# The defaults of section 17.
DEFAULT_HOLD_SECONDS = 420
DEFAULT_HOLD_SECONDS_NON_STREAMING = 240
DEFAULT_IDLE_MINUTES = 20
DEFAULT_DRAIN_SECONDS = 300
# Student affinity (section 5.5): the stickiness of a pool's target group, the share of its students
# one rebalance number moves, and the least time between two numbers.
DEFAULT_STICKINESS_SECONDS = 3600
DEFAULT_REBALANCE_SHARE = 0.1
DEFAULT_REBALANCE_INTERVAL_SECONDS = 60
# The longest stickiness the load balancer accepts: 7 days (evidence E13).
LONGEST_STICKINESS_SECONDS = 7 * 24 * 3600
# Dollars an hour: a pool with an instance type priced above it may not wake on demand (11.3, 17).
DEFAULT_ON_DEMAND_PRICE_LIMIT = 5.0

# Whose cached prefixes a request may reuse: its student's alone, or those of the pool's whole
# class (sections 4.7 and 6.6). A pool that gives no scope has the first.
CACHE_SALT_SCOPES = ("student", "class")
DEFAULT_CACHE_SALT_SCOPE = "student"

# The start delay of a mock pool that gives none: its engine is ready at once.
DEFAULT_START_DELAY_SECONDS = 0

# How a vllm pool's engine reads the model's weights: streamed from S3, or copied to the instance's
# disk first (section 4.4). A pool that gives no loader streams.
LOADERS = ("stream", "download")
DEFAULT_LOADER = "stream"

# The keys of `expected`, each a low and a high bound (section 4.7).
EXPECTED_KEYS = ("stream_tokens_per_second", "time_to_first_token_seconds", "cache_tokens")

# The precisions of the engine's cache, in bits, and the one of a pool that gives none (4.7).
KV_CACHE_BITS = (8, 16)
DEFAULT_KV_CACHE_BITS = 16
# The keys of `histogram_buckets`: vLLM's names for the bucket families of the three times the
# dashboards read (section 7.2; evidence E3).
BUCKET_FAMILIES = ("request_latency", "time_to_first_token", "inter_token_latency")

# Students choose a pool by model name, and Claude Code requires a served name without a slash
# (section 4.2): the pool's name is that model name.
_NAME = re.compile(r"[a-z0-9-]+", re.ASCII)
# A commit of the model repository, in full: a branch or a tag would move under a deployed pool.
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
# A parser's name, which becomes an argument of the engine: it starts with a letter or a digit, so
# the engine never reads it as an option.
_PARSER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
# An entry of a request profile, which the gateway looks up as a module of its profile package.
_PROFILE_ENTRY = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)


def group_name(pool: str) -> str:
    """The name of the pool's Auto Scaling group. The pools stack gives the group this name, and the cost
    dashboard reads the group's metrics by it, the hours of a group deleted and created again included."""
    return f"rhi-pool-{pool}"


class PoolConfigError(ValueError):
    """A pool's entry was refused. The message names the pool and the field, never a value.

    `field` is `name` for a refused pool name, and None when the entry itself is not a table.
    """

    def __init__(self, pool: str, field: str | None, problem: str) -> None:
        where = f"pool {pool!r}" if field is None else f"pool {pool!r}, field {field!r}"
        super().__init__(f"{where}: {problem}")
        self.pool = pool
        self.field = field


@dataclass(frozen=True)
class ExpectedValues:
    """What a pool is expected to measure, each a (low, high) pair, or None when it gives no estimate."""

    # Tokens a second, per stream, at the pool's target concurrency.
    stream_tokens_per_second: tuple[float, float] | None
    # Seconds to the first token of a turn whose transcript is cached.
    time_to_first_token_seconds: tuple[float, float] | None
    # Tokens of cache.
    cache_tokens: tuple[float, float] | None


@dataclass(frozen=True)
class HistogramBuckets:
    """The boundaries a pool gives a bucket family, in seconds and increasing, or None for vLLM's own."""

    # The whole request, and each of its phases.
    request_latency: tuple[float, ...] | None
    time_to_first_token: tuple[float, ...] | None
    # The time between two tokens of a stream.
    inter_token_latency: tuple[float, ...] | None


@dataclass(frozen=True)
class Pool:
    """One pool, typed. Its fields other than `name` are the fields an entry may hold."""

    # The key of the entry, and the model name students choose the pool by.
    name: str
    engine: str
    # The model repository and its pinned commit. A vllm pool has both.
    model: str | None
    revision: str | None
    # The type it runs first, then its fallbacks.
    instance_types: tuple[str, ...]
    # Dollars an hour for each instance type, in the order of `instance_types`.
    prices: dict[str, float]
    max_instances: int
    # Prepared instances, running or stopped, that the group keeps: from 0 to `max_instances`.
    warm_instances: int
    wake: str
    target_concurrency: int
    # In tokens: the limits the gateway's body filter clamps to (section 17).
    context_length: int
    largest_output: int
    # How long a call is held while the pool wakes: a stream, then a non-streaming call (5.3).
    hold_seconds: int
    hold_seconds_non_streaming: int
    idle_minutes: int
    drain_seconds: int
    # None until a start has been measured (spike S1): the launch hook's deadline becomes twice
    # this (sections 4.3 and 17).
    measured_start_seconds: int | None
    # None when the pool gives no estimate (the mock).
    expected: ExpectedValues | None
    # Whether the engine may run the code the model repository holds, and why: an exception to
    # section 4.4, recorded with the pool. A pool that sets the first gives the second.
    trust_remote_code: bool
    trust_remote_code_reason: str | None
    # The serving settings of a vllm pool, each as vLLM runs by itself when the pool leaves it out
    # (sections 4.2 and 4.7). vLLM's names for the two parsers, or None for no parser.
    tool_call_parser: str | None
    reasoning_parser: str | None
    # The precision of the engine's cache, in bits: 8 or 16.
    kv_cache_bits: int
    prefix_caching: bool
    # The most tokens of one engine step, or None for vLLM's own budget.
    step_budget_tokens: int | None
    # None when the pool replaces no family's boundaries (section 7.2).
    histogram_buckets: HistogramBuckets | None
    # The tokens the model's draft head proposes, or None when the head is off.
    draft_head_tokens: int | None
    # How the engine reads the weights: `stream` from S3, or `download` to the disk first.
    loader: str
    # The gateway's request profile for the pool: the names of its entries, in the order they
    # apply (section 6.2, step 4). Empty when the pool has none.
    request_profile: tuple[str, ...]
    # The value of reasoning effort the engine receives for each value a request may send, or
    # None when the pool maps none.
    reasoning_effort: dict[str, str] | None
    # Student affinity (section 5.5). How long the pool's target group keeps a student on one
    # instance, renewed by every request: from 1 second to 7 days.
    stickiness_seconds: int
    # The share of the pool's students one rebalance number moves, from 0 to 1.
    rebalance_share: float
    # The least time between two rebalance numbers.
    rebalance_interval_seconds: int
    # Whose cached prefixes a request on the pool may reuse: `student` or `class` (section 4.7).
    cache_salt_scope: str
    # Seconds after its start during which a mock engine's health route answers 503 (section 4.1).
    # 0 for every pool that does not give it, and only a mock pool may give it.
    start_delay_seconds: int


FIELDS = frozenset(field.name for field in fields(Pool)) - {"name"}
# The fields an entry may still hold though a pool has none: each is checked, then dropped.
RETIRED_FIELDS = frozenset({"gpu_hours_per_day"})


def _text(pool: str, entry: Mapping[str, object], field: str) -> str:
    if field not in entry:
        raise PoolConfigError(pool, field, "is missing")
    value = entry[field]
    if not isinstance(value, str) or value == "":
        raise PoolConfigError(pool, field, "must be a string that is not empty")
    return value


def _choice(pool: str, entry: Mapping[str, object], field: str, choices: tuple[str, ...]) -> str:
    value = _text(pool, entry, field)
    if value not in choices:
        raise PoolConfigError(pool, field, f"must be one of: {', '.join(choices)}")
    return value


def _whole(pool: str, entry: Mapping[str, object], field: str, *, least: int, default: int | None = None) -> int:
    """A whole number of at least `least`. A field left out takes its default, or is refused."""
    if field not in entry:
        if default is None:
            raise PoolConfigError(pool, field, "is missing")
        return default
    value = entry[field]
    # A boolean is an `int` to Python, and not a number to whoever wrote the entry.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PoolConfigError(pool, field, "must be a whole number")
    if value < least:
        raise PoolConfigError(pool, field, f"must be at least {least}")
    return value


def _instance_types(pool: str, entry: Mapping[str, object]) -> tuple[str, ...]:
    field = "instance_types"
    if field not in entry:
        raise PoolConfigError(pool, field, "is missing")
    value = entry[field]
    if not isinstance(value, (list, tuple)) or not value:
        raise PoolConfigError(pool, field, "must be a list of instance types that is not empty")
    if not all(isinstance(item, str) and item != "" for item in value):
        raise PoolConfigError(pool, field, "must hold only strings that are not empty")
    return tuple(value)


def _number(value: object) -> float | None:
    """The value as a finite number, or None when it is not one (a boolean, text, NaN, infinity)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _prices(pool: str, entry: Mapping[str, object], instance_types: tuple[str, ...]) -> dict[str, float]:
    field = "prices"
    if field not in entry:
        raise PoolConfigError(pool, field, "is missing")
    value = entry[field]
    if not isinstance(value, Mapping):
        raise PoolConfigError(pool, field, "must be a table of instance types and their prices")
    if any(kind not in instance_types for kind in value):
        raise PoolConfigError(pool, field, "prices an instance type the pool does not list")
    prices: dict[str, float] = {}
    for kind in instance_types:
        if kind not in value:
            raise PoolConfigError(pool, field, "must price every listed instance type")
        price = _number(value[kind])
        if price is None:
            raise PoolConfigError(pool, field, "must hold numbers, in dollars an hour")
        if price < 0:
            raise PoolConfigError(pool, field, "must hold prices of at least 0")
        prices[kind] = price
    return prices


def _bounds(pool: str, value: object) -> tuple[float, float]:
    field = "expected"
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PoolConfigError(pool, field, "must give each quantity as a low and a high bound")
    low, high = _number(value[0]), _number(value[1])
    if low is None or high is None:
        raise PoolConfigError(pool, field, "must give bounds that are numbers")
    if low < 0 or high <= 0:
        raise PoolConfigError(pool, field, "must give a low bound of at least 0 and a high bound above 0")
    if low > high:
        raise PoolConfigError(pool, field, "must give a low bound at most its high bound")
    return (low, high)


def _expected(pool: str, entry: Mapping[str, object]) -> ExpectedValues | None:
    field = "expected"
    if field not in entry:
        return None
    value = entry[field]
    if not isinstance(value, Mapping):
        raise PoolConfigError(pool, field, f"must be a table of: {', '.join(EXPECTED_KEYS)}")
    if any(key not in EXPECTED_KEYS for key in value):
        raise PoolConfigError(pool, field, f"may only hold: {', '.join(EXPECTED_KEYS)}")
    given = {key: _bounds(pool, value[key]) if key in value else None for key in EXPECTED_KEYS}
    return ExpectedValues(**given)


def _trust_remote_code(pool: str, entry: Mapping[str, object]) -> tuple[bool, str | None]:
    """Whether the pool trusts its model repository's code, and the reason it records for it."""
    trusted = entry.get("trust_remote_code", False)
    if not isinstance(trusted, bool):
        raise PoolConfigError(pool, "trust_remote_code", "must be true or false")
    field = "trust_remote_code_reason"
    reason = _text(pool, entry, field) if field in entry else None
    if trusted and reason is None:
        raise PoolConfigError(pool, field, "is missing: a pool that sets trust_remote_code says why")
    return trusted, reason


def _parser(pool: str, entry: Mapping[str, object], field: str) -> str | None:
    """The name of a parser, or None when the pool names none."""
    if field not in entry:
        return None
    if _PARSER.fullmatch(_text(pool, entry, field)) is None:
        raise PoolConfigError(
            pool, field, "must be a parser's name: letters, digits, '_', '.' and '-', and no '-' or '.' first"
        )
    return str(entry[field])


def _kv_cache_bits(pool: str, entry: Mapping[str, object]) -> int:
    value = entry.get("kv_cache_bits", DEFAULT_KV_CACHE_BITS)
    # True is 1 to Python and 8.0 is 8: neither is a number of bits to whoever wrote the entry.
    if type(value) is not int or value not in KV_CACHE_BITS:
        raise PoolConfigError(pool, "kv_cache_bits", "must be 8 or 16")
    return value


def _prefix_caching(pool: str, entry: Mapping[str, object]) -> bool:
    value = entry.get("prefix_caching", True)
    if not isinstance(value, bool):
        raise PoolConfigError(pool, "prefix_caching", "must be true or false")
    return value


def _boundaries(pool: str, value: object) -> tuple[float, ...]:
    """The boundaries of one bucket family, as vLLM accepts them: numbers above 0, each above the last."""
    field = "histogram_buckets"
    if not isinstance(value, (list, tuple)) or not value:
        raise PoolConfigError(pool, field, "must give each family a list of boundaries that is not empty")
    boundaries: list[float] = []
    for item in value:
        boundary = _number(item)
        if boundary is None or boundary <= 0:
            raise PoolConfigError(pool, field, "must hold numbers above 0, in seconds")
        boundaries.append(boundary)
    if any(low >= high for low, high in itertools.pairwise(boundaries)):
        raise PoolConfigError(pool, field, "must give each family's boundaries in increasing order")
    return tuple(boundaries)


def _histogram_buckets(pool: str, entry: Mapping[str, object]) -> HistogramBuckets | None:
    field = "histogram_buckets"
    if field not in entry:
        return None
    value = entry[field]
    if not isinstance(value, Mapping):
        raise PoolConfigError(pool, field, f"must be a table of: {', '.join(BUCKET_FAMILIES)}")
    if any(family not in BUCKET_FAMILIES for family in value):
        raise PoolConfigError(pool, field, f"may only hold: {', '.join(BUCKET_FAMILIES)}")
    given = {family: _boundaries(pool, value[family]) if family in value else None for family in BUCKET_FAMILIES}
    return HistogramBuckets(**given)


def _request_profile(pool: str, entry: Mapping[str, object]) -> tuple[str, ...]:
    """The names of the profile's entries, in the order written. A pool that gives none has no entry."""
    field = "request_profile"
    value = entry.get(field, ())
    if not isinstance(value, (list, tuple)):
        raise PoolConfigError(pool, field, "must be a list of the names of the profile's entries")
    if not all(isinstance(item, str) and _PROFILE_ENTRY.fullmatch(item) for item in value):
        raise PoolConfigError(
            pool, field, "must hold names of lower-case letters, digits and '_' that start with a letter"
        )
    if len(set(value)) != len(value):
        raise PoolConfigError(pool, field, "names an entry twice")
    return tuple(value)


def _reasoning_effort(pool: str, entry: Mapping[str, object]) -> dict[str, str] | None:
    """The value the engine receives for each value a request may send, or None when the pool maps none."""
    field = "reasoning_effort"
    if field not in entry:
        return None
    value = entry[field]
    if not isinstance(value, Mapping):
        raise PoolConfigError(pool, field, "must be a table from a value a request sends to the value the engine gets")
    if not all(isinstance(text, str) and text != "" for pair in value.items() for text in pair):
        raise PoolConfigError(pool, field, "must hold only strings that are not empty")
    return dict(value)


def _stickiness_seconds(pool: str, entry: Mapping[str, object]) -> int:
    field = "stickiness_seconds"
    value = _whole(pool, entry, field, least=1, default=DEFAULT_STICKINESS_SECONDS)
    if value > LONGEST_STICKINESS_SECONDS:
        raise PoolConfigError(pool, field, f"must be at most {LONGEST_STICKINESS_SECONDS}, which is 7 days")
    return value


def _rebalance_share(pool: str, entry: Mapping[str, object]) -> float:
    field = "rebalance_share"
    share = _number(entry.get(field, DEFAULT_REBALANCE_SHARE))
    if share is None or not 0 <= share <= 1:
        raise PoolConfigError(pool, field, "must be a number from 0 to 1")
    return share


def _start_delay_seconds(pool: str, entry: Mapping[str, object], engine: str) -> int:
    field = "start_delay_seconds"
    if field in entry and engine != "mock":
        raise PoolConfigError(pool, field, "may only be given by a pool whose engine is mock")
    return _whole(pool, entry, field, least=0, default=DEFAULT_START_DELAY_SECONDS)


def _parse_pool(name: str, entry: object, on_demand_price_limit: float) -> Pool:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise PoolConfigError(str(name), "name", "must hold only lower-case letters, digits and hyphens")
    if not isinstance(entry, Mapping):
        raise PoolConfigError(name, None, "the entry must be a table")
    unknown = sorted(str(field) for field in entry if field not in FIELDS | RETIRED_FIELDS)
    if unknown:
        raise PoolConfigError(name, unknown[0], "is not a field of a pool")
    if "gpu_hours_per_day" in entry:
        _whole(name, entry, "gpu_hours_per_day", least=0)  # checked, and read into nothing

    engine = _choice(name, entry, "engine", ENGINES)
    served_by_vllm = engine == "vllm"
    model = _text(name, entry, "model") if served_by_vllm or "model" in entry else None
    revision = _text(name, entry, "revision") if served_by_vllm or "revision" in entry else None
    if served_by_vllm and (revision is None or _COMMIT.fullmatch(revision) is None):
        raise PoolConfigError(name, "revision", "must be a commit hash of 40 lower-case hexadecimal characters")

    max_instances = _whole(name, entry, "max_instances", least=1)
    warm_instances = _whole(name, entry, "warm_instances", least=0)
    if warm_instances > max_instances:
        raise PoolConfigError(name, "warm_instances", "must be at most max_instances")

    instance_types = _instance_types(name, entry)
    prices = _prices(name, entry, instance_types)
    wake = _choice(name, entry, "wake", WAKES)
    if wake == "on-demand" and any(price > on_demand_price_limit for price in prices.values()):
        raise PoolConfigError(name, "wake", "must be scheduled-only: an instance type is priced above the limit")
    trust_remote_code, trust_remote_code_reason = _trust_remote_code(name, entry)

    return Pool(
        name=name,
        engine=engine,
        model=model,
        revision=revision,
        instance_types=instance_types,
        prices=prices,
        max_instances=max_instances,
        warm_instances=warm_instances,
        wake=wake,
        target_concurrency=_whole(name, entry, "target_concurrency", least=1),
        context_length=_whole(name, entry, "context_length", least=1),
        largest_output=_whole(name, entry, "largest_output", least=1),
        hold_seconds=_whole(name, entry, "hold_seconds", least=1, default=DEFAULT_HOLD_SECONDS),
        hold_seconds_non_streaming=_whole(
            name, entry, "hold_seconds_non_streaming", least=1, default=DEFAULT_HOLD_SECONDS_NON_STREAMING
        ),
        idle_minutes=_whole(name, entry, "idle_minutes", least=0, default=DEFAULT_IDLE_MINUTES),
        drain_seconds=_whole(name, entry, "drain_seconds", least=0, default=DEFAULT_DRAIN_SECONDS),
        measured_start_seconds=(
            _whole(name, entry, "measured_start_seconds", least=1) if "measured_start_seconds" in entry else None
        ),
        expected=_expected(name, entry),
        trust_remote_code=trust_remote_code,
        trust_remote_code_reason=trust_remote_code_reason,
        tool_call_parser=_parser(name, entry, "tool_call_parser"),
        reasoning_parser=_parser(name, entry, "reasoning_parser"),
        kv_cache_bits=_kv_cache_bits(name, entry),
        prefix_caching=_prefix_caching(name, entry),
        step_budget_tokens=(
            _whole(name, entry, "step_budget_tokens", least=1) if "step_budget_tokens" in entry else None
        ),
        histogram_buckets=_histogram_buckets(name, entry),
        draft_head_tokens=(
            _whole(name, entry, "draft_head_tokens", least=1) if "draft_head_tokens" in entry else None
        ),
        loader=_choice(name, entry, "loader", LOADERS) if "loader" in entry else DEFAULT_LOADER,
        request_profile=_request_profile(name, entry),
        reasoning_effort=_reasoning_effort(name, entry),
        stickiness_seconds=_stickiness_seconds(name, entry),
        rebalance_share=_rebalance_share(name, entry),
        rebalance_interval_seconds=_whole(
            name, entry, "rebalance_interval_seconds", least=1, default=DEFAULT_REBALANCE_INTERVAL_SECONDS
        ),
        cache_salt_scope=(
            _choice(name, entry, "cache_salt_scope", CACHE_SALT_SCOPES)
            if "cache_salt_scope" in entry
            else DEFAULT_CACHE_SALT_SCOPE
        ),
        start_delay_seconds=_start_delay_seconds(name, entry, engine),
    )


def parse_pools(
    table: Mapping[str, object], *, on_demand_price_limit: float = DEFAULT_ON_DEMAND_PRICE_LIMIT
) -> tuple[Pool, ...]:
    """The pools of the configuration's `pools` table, typed, in the table's order.

    `table` maps each pool's name to its entry, as TOML reads `[pools.<name>]`. A pool that wakes
    on demand may price no instance type above `on_demand_price_limit`, in dollars an hour. The
    first entry that breaks a rule raises `PoolConfigError`.
    """
    return tuple(_parse_pool(name, entry, on_demand_price_limit) for name, entry in table.items())
