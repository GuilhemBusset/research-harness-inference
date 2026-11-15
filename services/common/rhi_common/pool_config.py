"""A pool's entry in the configuration: its fields, its limits and its defaults.

A pool is one model on one family of instance types (system design, section 4.2). It is declared
as one entry of the configuration's `pools` table and becomes one Auto Scaling group, one target
group and one dashboard row. The constructs, the scaler, the gateway, the lifecycle agent and the
dashboards all read the same entry, so its shape is decided here and nowhere else. The module
needs the standard library only, and knows nothing of where the configuration comes from:
`parse_pools` takes the table as the source gave it.

Only four fields have a default, the ones section 17 gives every pool: the two holds, the idle
time and the drain. `target_concurrency` and `gpu_hours_per_day` are a pool's own values, so
every pool gives the first and every vllm pool the second. A mock pool serves canned responses
from no model repository: it may leave out `model`, `revision` and `gpu_hours_per_day`.

A refusal is a `PoolConfigError` that names the pool and the field, never a value: build logs
show it.

**Adding a field.** The configuration is written once and loaded on every deployment by a loader
that fails closed. A field added after the first pool was written therefore has a default or is
optional: one that every pool had to give would fail the next deployment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

ENGINES = ("mock", "vllm")
WAKES = ("on-demand", "scheduled-only")

# The defaults of section 17. No other field has one.
DEFAULT_HOLD_SECONDS = 420
DEFAULT_HOLD_SECONDS_NON_STREAMING = 240
DEFAULT_IDLE_MINUTES = 20
DEFAULT_DRAIN_SECONDS = 300

# Students choose a pool by model name, and Claude Code requires a served name without a slash
# (section 4.2): the pool's name is that model name.
_NAME = re.compile(r"[a-z0-9-]+", re.ASCII)
# A commit of the model repository, in full: a branch or a tag would move under a deployed pool.
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)


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
class Pool:
    """One pool, typed. Its fields other than `name` are the fields an entry may hold."""

    # The key of the entry, and the model name students choose the pool by.
    name: str
    engine: str
    # The model repository and its pinned commit. A vllm pool has both.
    model: str | None
    revision: str | None
    instance_types: tuple[str, ...]
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
    # The scaler refuses to wake beyond this. A vllm pool has one.
    gpu_hours_per_day: int | None
    # None until a start has been measured (spike S1): the launch hook's deadline becomes twice
    # this (sections 4.3 and 17).
    measured_start_seconds: int | None


FIELDS = frozenset(field.name for field in fields(Pool)) - {"name"}


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


def _parse_pool(name: str, entry: object) -> Pool:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise PoolConfigError(str(name), "name", "must hold only lower-case letters, digits and hyphens")
    if not isinstance(entry, Mapping):
        raise PoolConfigError(name, None, "the entry must be a table")
    unknown = sorted(str(field) for field in entry if field not in FIELDS)
    if unknown:
        raise PoolConfigError(name, unknown[0], "is not a field of a pool")

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

    return Pool(
        name=name,
        engine=engine,
        model=model,
        revision=revision,
        instance_types=_instance_types(name, entry),
        max_instances=max_instances,
        warm_instances=warm_instances,
        wake=_choice(name, entry, "wake", WAKES),
        target_concurrency=_whole(name, entry, "target_concurrency", least=1),
        context_length=_whole(name, entry, "context_length", least=1),
        largest_output=_whole(name, entry, "largest_output", least=1),
        hold_seconds=_whole(name, entry, "hold_seconds", least=1, default=DEFAULT_HOLD_SECONDS),
        hold_seconds_non_streaming=_whole(
            name, entry, "hold_seconds_non_streaming", least=1, default=DEFAULT_HOLD_SECONDS_NON_STREAMING
        ),
        idle_minutes=_whole(name, entry, "idle_minutes", least=0, default=DEFAULT_IDLE_MINUTES),
        drain_seconds=_whole(name, entry, "drain_seconds", least=0, default=DEFAULT_DRAIN_SECONDS),
        gpu_hours_per_day=(
            _whole(name, entry, "gpu_hours_per_day", least=0)
            if served_by_vllm or "gpu_hours_per_day" in entry
            else None
        ),
        measured_start_seconds=(
            _whole(name, entry, "measured_start_seconds", least=1) if "measured_start_seconds" in entry else None
        ),
    )


def parse_pools(table: Mapping[str, object]) -> tuple[Pool, ...]:
    """The pools of the configuration's `pools` table, typed, in the table's order.

    `table` maps each pool's name to its entry, as TOML reads `[pools.<name>]`. The first entry
    that breaks a rule raises `PoolConfigError`.
    """
    return tuple(_parse_pool(name, entry) for name, entry in table.items())
