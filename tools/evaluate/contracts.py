"""The public format of an evaluation run: its manifest, its suite and its evidence records.

An evaluation measures what a pool serves and what agents finish on it
(`docs/design/agent-session-evaluation.md`, "Reproducing and comparing runs"). What a run leaves
for others to read, exchange and recompute a scorecard from is written in this format, and in
nothing else. The driver's own journal (its booking, what it delivered to GitHub) is private and
is not part of it.

**What may be written.** The repository is public, so the format has no field that holds free
text. Every value is one of:

- a label of an enumeration of this module. Hardware is one too: an instance type and a GPU model
  are labels of `InstanceType` and `GpuType`, by their public names, and nothing spelled like one;
- a bounded integer: an ordinal local to the run (a student, a host, a trial, a cell), a count, a
  seed, or a time in milliseconds since the start of the run;
- a bounded, finite number. No number of the format reaches twelve digits;
- a public digest (`sha256:` and 64 hexadecimal digits), a 40-digit commit or a three-part
  version, each held to its grammar;
- a task or instance ID, made of a family of `Family` and two-digit numbers (`TaskId`).

A name, an address, a host, a path, an account, a resource ID or a line of model text fits none
of them, so it is refused wherever it is put, and a refusal names the field and the rule, never
the value. A field's name carries its unit (`_ms`, `_tokens`, `_tokens_per_s`, `_mib`, `_points`);
a host sample carries the unit its metric fixes.

**Missing is written.** A value that was not observed is `null` under its key. A key left out is
refused, like a key the format does not have, so "no usage reported" and "zero tokens" are two
different records. Each count of a usage is missing on its own: a stream cut before its last
usage event keeps the input it reported and has no output count.

**Documents.** `Manifest`, `Suite`, `GraderOutcome`, `RequestRecord`, `TrialRecord`, `HostSample`
and `CellRecord` are the documents. Each is written with its `kind` and the `schema_version`, as
one line of canonical JSON (`dumps`): keys sorted, no white space, a measurement always as a
decimal. `content_hash` is the SHA-256 of those bytes. `loads` and `parse` read a document back
and hold it to the same rules as its constructor, and read only the version of this module.

**Identities.** A manifest has three, each a digest of one part of it (`Manifest.workload_id`,
`treatment_id`, `run_id`): what was planned, the serving settings that were requested, and the
run that measured the one on the other. Two treatments of one planned workload have the same
workload identity. What was observed is recorded apart from what was requested and is in none of
the three.

It needs the standard library only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from itertools import pairwise
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self, Union, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

SCHEMA_VERSION = 1


class ContractError(ValueError):
    """A value the format refuses. The message names the field and the rule, never the value."""


# --- How a field is held to the format -------------------------------------------------------


class _Spec:
    """The rule of one field. `check` takes what a constructor was given and returns what is
    stored; `read` does the same for a value decoded from JSON; `write` gives the JSON value."""

    def check(self, value: object, where: str) -> Any:
        raise NotImplementedError

    def read(self, value: object, where: str) -> Any:
        return self.check(value, where)

    def write(self, value: Any) -> object:
        return value


@dataclass(frozen=True)
class _Integer(_Spec):
    low: int
    high: int

    def check(self, value: object, where: str) -> int:
        # `True` is an `int` to Python and is not one here.
        if type(value) is not int or not self.low <= value <= self.high:
            raise ContractError(f"{where}: needs an integer from {self.low} to {self.high}")
        return value


@dataclass(frozen=True)
class _Number(_Spec):
    low: float
    high: float

    def check(self, value: object, where: str) -> float:
        # NaN compares false with everything and an infinity is outside every bound: neither passes.
        number = not isinstance(value, bool) and isinstance(value, (int, float))
        if not number or not self.low <= value <= self.high:
            raise ContractError(f"{where}: needs a finite number from {self.low:g} to {self.high:g}")
        # One stored form, so one serialization: 3 and 3.0 are written `3.0`, and -0.0 as `0.0`.
        return float(value) + 0.0


@dataclass(frozen=True)
class _Text(_Spec):
    pattern: str
    what: str

    def check(self, value: object, where: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(self.pattern, value):
            raise ContractError(f"{where}: needs {self.what}")
        return value


class _Boolean(_Spec):
    def check(self, value: object, where: str) -> bool:
        if type(value) is not bool:
            raise ContractError(f"{where}: needs true or false")
        return value


@dataclass(frozen=True)
class _Label(_Spec):
    labels: type[StrEnum]

    def check(self, value: object, where: str) -> StrEnum:
        if not isinstance(value, self.labels):
            raise ContractError(f"{where}: needs a label of {self.labels.__name__}")
        return value

    def read(self, value: object, where: str) -> StrEnum:
        if not isinstance(value, str) or value not in self.labels:
            raise ContractError(f"{where}: needs a label of {self.labels.__name__}")
        return self.labels(value)

    def write(self, value: StrEnum) -> object:
        return value.value


@dataclass(frozen=True)
class _Optional(_Spec):
    inner: _Spec

    def check(self, value: object, where: str) -> Any:
        return None if value is None else self.inner.check(value, where)

    def read(self, value: object, where: str) -> Any:
        return None if value is None else self.inner.read(value, where)

    def write(self, value: Any) -> object:
        return None if value is None else self.inner.write(value)


MAX_ITEMS = 100_000


@dataclass(frozen=True)
class _Array(_Spec):
    inner: _Spec

    def _each(self, value: object, where: str, one: Callable[[object, str], Any]) -> tuple[Any, ...]:
        if not isinstance(value, (tuple, list)) or len(value) > MAX_ITEMS:
            raise ContractError(f"{where}: needs a list of at most {MAX_ITEMS} items")
        return tuple(one(item, f"{where}[{index}]") for index, item in enumerate(value))

    def check(self, value: object, where: str) -> tuple[Any, ...]:
        return self._each(value, where, self.inner.check)

    def read(self, value: object, where: str) -> tuple[Any, ...]:
        return self._each(value, where, self.inner.read)

    def write(self, value: tuple[Any, ...]) -> object:
        return [self.inner.write(item) for item in value]


@dataclass(frozen=True)
class _Nested(_Spec):
    record: type[Record]

    def check(self, value: object, where: str) -> Record:
        if type(value) is not self.record:  # built, so already held to its own rules
            raise ContractError(f"{where}: needs a {self.record.__name__}")
        return value

    def read(self, value: object, where: str) -> Record:
        return self.record.from_json(value, where)

    def write(self, value: Record) -> object:
        return value.to_json()


def _spec_of(hint: Any) -> _Spec:
    """The rule an annotation stands for. An integer, a number and a string have no rule of their
    own: each is annotated with its bounds or its grammar (the aliases below)."""
    origin = get_origin(hint)
    if origin is Annotated:
        return next(extra for extra in get_args(hint)[1:] if isinstance(extra, _Spec))
    if origin in (Union, UnionType):
        (inner,) = (arg for arg in get_args(hint) if arg is not NoneType)
        return _Optional(_spec_of(inner))
    if origin is tuple:
        item, _ellipsis = get_args(hint)
        return _Array(_spec_of(item))
    if hint is bool:
        return _Boolean()
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return _Label(hint)
    if isinstance(hint, type) and issubclass(hint, Record):
        return _Nested(hint)
    raise TypeError(f"no rule for the annotation {hint!r}")


@cache
def _specs(record: type[Record]) -> dict[str, _Spec]:
    hints = get_type_hints(record, include_extras=True)
    return {field.name: _spec_of(hints[field.name]) for field in dataclasses.fields(record)}


@dataclass(frozen=True, kw_only=True)
class Record:
    """A value of the format. It is held to its annotations and to `check` when it is built, by
    hand or from JSON, so a record that exists is a valid one."""

    def __post_init__(self) -> None:
        for name, spec in _specs(type(self)).items():
            object.__setattr__(self, name, spec.check(getattr(self, name), f"{type(self).__name__}.{name}"))
        self.check()

    def check(self) -> None:
        """The rules that hold between fields."""

    def to_json(self) -> dict[str, object]:
        return {name: spec.write(getattr(self, name)) for name, spec in _specs(type(self)).items()}

    @classmethod
    def from_json(cls, value: object, where: str) -> Self:
        specs = _specs(cls)
        if not isinstance(value, dict):
            raise ContractError(f"{where}: needs an object")
        if missing := sorted(specs.keys() - value.keys()):
            raise ContractError(f"{where}: no {', '.join(missing)} (a missing value is written as null)")
        if len(value) > len(specs):
            raise ContractError(f"{where}: {len(value) - len(specs)} unexpected field(s)")
        return cls(**{name: spec.read(value[name], f"{where}.{name}") for name, spec in specs.items()})


class Document(Record):
    """A record written on its own, with its kind and the schema version."""

    KIND: ClassVar[str]


def _fail(condition: object, record: Record, rule: str) -> None:
    if condition:
        raise ContractError(f"{type(record).__name__}: {rule}")


def _in_order(keys: Iterable[Any]) -> bool:
    """Whether each key is listed once, in order: one way to write a set, so one hash."""
    return all(before < after for before, after in pairwise(keys))


# --- The bounded numbers and the grammars ----------------------------------------------------

MAX_TIME_MS = 7 * 24 * 3600 * 1000
# Below twelve digits, like every bound here: no number of the format can hold an account ID.
MAX_COUNT = 10**10

# A position local to the run: a student, a host, a cell, a trial, a request. Never a name.
Ordinal = Annotated[int, _Integer(0, 999_999)]
Count = Annotated[int, _Integer(0, MAX_COUNT)]
Positive = Annotated[int, _Integer(1, MAX_COUNT)]
# Milliseconds since the start of the run, on a clock that does not go back. Never a date.
Millis = Annotated[int, _Integer(0, MAX_TIME_MS)]
Seed = Annotated[int, _Integer(0, 2**32 - 1)]
Measurement = Annotated[float, _Number(0.0, float(MAX_COUNT))]
Share = Annotated[float, _Number(0.0, 1.0)]
Points = Annotated[float, _Number(-100.0, 100.0)]

Digest = Annotated[str, _Text(r"sha256:[0-9a-f]{64}", "a digest, `sha256:` and 64 hexadecimal digits")]
Commit = Annotated[str, _Text(r"[0-9a-f]{40}", "a commit, 40 hexadecimal digits")]
# Three parts exactly: an address has four.
Version = Annotated[str, _Text(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", "a version, MAJOR.MINOR.PATCH")]


class Family(StrEnum):
    """The task families of the course (the design's "Course quality and the grader")."""

    OPTIMIZATION = "optimization"
    DEBUGGING = "debugging"
    ANALYSIS = "analysis"
    RESEARCH = "research"


# The IDs task authors give. A task is its family and a two-digit number, `optimization.t01`; an
# instance is its task and a two-digit number, `optimization.t01.i03`. The number is given once
# and kept: it is not the position in the suite. Nothing else is a task or an instance ID, so
# neither field can hold a name, a host or the ID of a deployed resource.
_TASK = rf"({'|'.join(Family)})\.t[0-9]{{2}}"
TaskId = Annotated[str, _Text(_TASK, "a task ID, a family, `.t` and two digits")]
InstanceId = Annotated[str, _Text(_TASK + r"\.i[0-9]{2}", "an instance ID, a task ID, `.i` and two digits")]


# --- The vocabularies ------------------------------------------------------------------------


class Shape(StrEnum):
    HANDS_ON = "hands_on"
    SEVERAL_AGENTS = "several_agents"


class Harness(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    OPENCODE = "opencode"
    PI = "pi"
    SDK_TOOL_LOOP = "sdk_tool_loop"


class HarnessFeature(StrEnum):
    """What a task needs of a harness it is assigned to."""

    DELEGATION = "delegation"
    FOLLOW_UP = "follow_up"
    LOCAL_SEARCH = "local_search"
    PDF_READING = "pdf_reading"
    SKILLS = "skills"


class Route(StrEnum):
    MESSAGES = "messages"
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class Profile(StrEnum):
    SCREEN = "screen"
    QUALIFY = "qualify"


class Phase(StrEnum):
    WARM_UP = "warm_up"
    CAPACITY_SWEEP = "capacity_sweep"
    LIVE_LOW_LOAD = "live_low_load"
    LIVE_CLASS_LOAD = "live_class_load"
    CLASS_OPENING = "class_opening"


LIVE_PHASES = frozenset({Phase.LIVE_LOW_LOAD, Phase.LIVE_CLASS_LOAD})


class CachePolicy(StrEnum):
    FRESH_PREFIX_WARM_ENGINE = "fresh_prefix_warm_engine"
    EMPTY_ENGINE = "empty_engine"


class PathCategory(StrEnum):
    """Where the load driver stands, as a category: never its address."""

    IN_REGION = "in_region"
    CROSS_REGION = "cross_region"
    PUBLIC_INTERNET = "public_internet"


class InstanceType(StrEnum):
    """The instance types of the pools the system design declares (its section 4.2), by
    their public names. A grammar would take a host spelled like a type, so there is none: a pool
    on another type adds its label here, with its GPUs below."""

    G7E_2XLARGE = "g7e.2xlarge"
    G7E_24XLARGE = "g7e.24xlarge"
    P5EN_48XLARGE = "p5en.48xlarge"


class GpuType(StrEnum):
    """The GPU models of those instance types, by their public names."""

    RTX_PRO_6000 = "RTX PRO 6000"
    H200 = "H200"


# The GPUs of each instance type: the model, and how many a host has.
INSTANCE_GPUS = {
    InstanceType.G7E_2XLARGE: (GpuType.RTX_PRO_6000, 1),
    InstanceType.G7E_24XLARGE: (GpuType.RTX_PRO_6000, 4),
    InstanceType.P5EN_48XLARGE: (GpuType.H200, 8),
}


class Quantization(StrEnum):
    NONE = "none"
    BITS_8 = "bits_8"
    BITS_4 = "bits_4"


class KvDtype(StrEnum):
    BITS_8 = "bits_8"
    BITS_16 = "bits_16"


class SpeculativeDecoding(StrEnum):
    OFF = "off"
    NGRAM = "ngram"
    DRAFT_MODEL = "draft_model"


class Traffic(StrEnum):
    WARM_UP = "warm_up"
    BACKGROUND = "background"
    LIVE = "live"


class RequestOutcome(StrEnum):
    """How a request ended. Everything but `completed` is a failed request."""

    COMPLETED = "completed"
    TRUNCATED_STREAM = "truncated_stream"
    TRANSPORT_ERROR = "transport_error"
    HTTP_REFUSAL = "http_refusal"
    ERROR_EVENT = "error_event"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_USE = "tool_use"


class TrialOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INVALID = "invalid"


class FailureReason(StrEnum):
    """Why a completed attempt did not pass. The attempt stays in the denominator."""

    REJECTED = "rejected"
    TIMEOUT = "timeout"
    TURN_LIMIT = "turn_limit"
    TOKEN_LIMIT = "token_limit"
    MALFORMED_TOOL_CALL = "malformed_tool_call"
    ENDPOINT_FAILURE = "endpoint_failure"


class InvalidReason(StrEnum):
    """Why an attempt says nothing about the agent: the measurement broke, not the work."""

    GRADER_ERROR = "grader_error"
    DRIVER_ERROR = "driver_error"
    INTERRUPTED = "interrupted"


class HostMetric(StrEnum):
    OUTPUT_TOKENS_TOTAL = "output_tokens_total"
    INPUT_TOKENS_TOTAL = "input_tokens_total"
    CACHED_INPUT_TOKENS_TOTAL = "cached_input_tokens_total"
    # The input the engine computed itself. No difference of the two above gives it: an engine
    # can also take input from elsewhere than its own cache, and not every engine reports it.
    COMPUTED_INPUT_TOKENS_TOTAL = "computed_input_tokens_total"
    RUNNING_REQUESTS = "running_requests"
    WAITING_REQUESTS = "waiting_requests"
    PREEMPTIONS_TOTAL = "preemptions_total"
    GPU_UTILIZATION = "gpu_utilization"


class Unit(StrEnum):
    TOKENS = "tokens"
    REQUESTS = "requests"
    EVENTS = "events"
    RATIO = "ratio"


# The unit of each metric of a host: a sample states it, and no sample states another.
METRIC_UNITS = {
    HostMetric.OUTPUT_TOKENS_TOTAL: Unit.TOKENS,
    HostMetric.INPUT_TOKENS_TOTAL: Unit.TOKENS,
    HostMetric.CACHED_INPUT_TOKENS_TOTAL: Unit.TOKENS,
    HostMetric.COMPUTED_INPUT_TOKENS_TOTAL: Unit.TOKENS,
    HostMetric.RUNNING_REQUESTS: Unit.REQUESTS,
    HostMetric.WAITING_REQUESTS: Unit.REQUESTS,
    HostMetric.PREEMPTIONS_TOTAL: Unit.EVENTS,
    HostMetric.GPU_UTILIZATION: Unit.RATIO,
}


class CellStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class InvalidFlag(StrEnum):
    """What makes a cell invalid for qualification. Its observations are kept all the same."""

    DRIVER_SATURATION = "driver_saturation"
    EXTERNAL_TRAFFIC = "external_traffic"
    HOST_TURNOVER = "host_turnover"
    MISSING_EVIDENCE = "missing_evidence"
    UNRECONCILED_COUNTERS = "unreconciled_counters"


# --- The suite: what task authors write against ----------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Instance(Record):
    """One input of a task: its fixture and the grader that checks work on it, each as a digest."""

    instance_id: InstanceId
    fixture_digest: Digest
    grader_digest: Digest


@dataclass(frozen=True, kw_only=True)
class HarnessAssignment(Record):
    """A harness a task runs on, and the features of it the task needs (which one delegates)."""

    harness: Harness
    features: tuple[HarnessFeature, ...]

    def check(self) -> None:
        _fail(not _in_order(self.features), self, "features are listed once each, in order")


@dataclass(frozen=True, kw_only=True)
class Task(Record):
    """A task of the suite: its instances, and the harnesses it is required on."""

    task_id: TaskId
    # How many properties the grader requires of a result: a grade answers each, in this order.
    required_properties: Annotated[int, _Integer(1, 64)]
    instances: tuple[Instance, ...]
    harnesses: tuple[HarnessAssignment, ...]

    def check(self) -> None:
        _fail(not self.instances or not self.harnesses, self, "needs an instance and a harness")
        ids = [instance.instance_id for instance in self.instances]
        _fail(not _in_order(ids), self, "instances are listed once each, in the order of their IDs")
        _fail(any(not one.startswith(self.task_id + ".") for one in ids), self, "an instance ID starts with its task's")
        harnesses = [assignment.harness for assignment in self.harnesses]
        _fail(not _in_order(harnesses), self, "harnesses are listed once each, in order")

    @property
    def family(self) -> Family:
        return Family(self.task_id.split(".")[0])


@dataclass(frozen=True, kw_only=True)
class Suite(Document):
    """The tasks of a suite version. Its content hash is the `suite_digest` of a workload, so a
    changed fixture, grader or assignment is another workload."""

    KIND = "suite"
    version: Annotated[int, _Integer(1, 9_999)]
    tasks: tuple[Task, ...]

    def check(self) -> None:
        _fail(not self.tasks, self, "needs a task")
        ids = [task.task_id for task in self.tasks]
        _fail(not _in_order(ids), self, "tasks are listed once each, in the order of their IDs")


@dataclass(frozen=True, kw_only=True)
class Grade(Record):
    """What a grader found: one answer for each required property of the task, in the task's
    order, and whether the work stayed within the trial's limits."""

    properties: tuple[bool, ...]
    within_limits: bool

    def check(self) -> None:
        _fail(not 1 <= len(self.properties) <= 64, self, "needs 1 to 64 properties")

    @property
    def accepted(self) -> bool:
        """A success: every required property, within the limits."""
        return self.within_limits and all(self.properties)


@dataclass(frozen=True, kw_only=True)
class GraderOutcome(Document):
    """What a grader returns for one instance: a grade, accepted or not, or the reason there is
    none. A grader that could not grade says `invalid`; it never answers with a failing grade."""

    KIND = "grader_outcome"
    instance_id: InstanceId
    grade: Grade | None
    invalid: InvalidReason | None

    def check(self) -> None:
        _fail((self.grade is None) == (self.invalid is None), self, "is a grade or an invalid reason, one of the two")


# --- The manifest ----------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Limits(Record):
    """The caps declared for the run: for a trial, and for the drain of a cell."""

    trial_wall_time_ms: Millis
    trial_model_turns: Positive
    trial_generated_tokens: Positive
    drain_ms: Millis


@dataclass(frozen=True, kw_only=True)
class Sampling(Record):
    """The sample budgets a reading needs, and how often a host is sampled."""

    ttft_samples_min: Count
    stream_samples_min: Count
    bootstrap_draws: Count
    host_sample_interval_ms: Millis


@dataclass(frozen=True, kw_only=True)
class Objectives(Record):
    """The floors and ceilings the scorecard is read against, frozen before the run."""

    ttft_p95_ms_max: Measurement
    stream_speed_p50_tokens_per_s_min: Measurement
    stream_speed_p10_tokens_per_s_min: Measurement
    failed_share_max: Share
    success_share_min: Share
    paired_margin_points: Points


@dataclass(frozen=True, kw_only=True)
class HarnessPin(Record):
    """The version of a harness the plan runs, and the digest of the profile it runs with."""

    harness: Harness
    version: Version
    profile_digest: Digest


@dataclass(frozen=True, kw_only=True)
class CellIdentity(Record):
    """A cell the plan requires: one phase, at one shape and load, one repetition. A live cell is
    also one family of tasks, which its trials say: a booking can end between two families."""

    cell: Ordinal
    phase: Phase
    shape: Shape
    agent_slots: Annotated[int, _Integer(1, 1024)]
    repetition: Ordinal


@dataclass(frozen=True, kw_only=True)
class TrialIdentity(Record):
    """A trial the plan requires: one attempt of an instance on a harness, in a live cell. Every
    live cell of a plan has one at least; a capacity or warm-up cell has none."""

    trial: Ordinal
    cell: Ordinal
    instance_id: InstanceId
    harness: Harness
    attempt: Ordinal


@dataclass(frozen=True, kw_only=True)
class Workload(Record):
    """What was planned, whatever serves it. Two runs compare when this part is the same."""

    profile: Profile
    students: Annotated[int, _Integer(1, 1024)]
    suite_digest: Digest
    arrival_digest: Digest
    protocol_digest: Digest
    input_seed: Seed
    schedule_seed: Seed
    cache_policy: CachePolicy
    harnesses: tuple[HarnessPin, ...]
    limits: Limits
    sampling: Sampling
    objectives: Objectives
    cells: tuple[CellIdentity, ...]
    trials: tuple[TrialIdentity, ...]

    def check(self) -> None:
        pinned = [pin.harness for pin in self.harnesses]
        _fail(not _in_order(pinned), self, "harnesses are pinned once each, in order")
        _fail(not self.cells, self, "needs a cell")
        _fail(any(cell.cell != at for at, cell in enumerate(self.cells)), self, "a cell's ordinal is its position")
        # Live cells of one phase, shape, load and repetition are told apart by the families of
        # their trials: no two of them run the same family.
        families: dict[int, set[str]] = {}
        for trial in self.trials:
            families.setdefault(trial.cell, set()).add(trial.instance_id.split(".")[0])
        planned = [
            (dataclasses.replace(cell, cell=0), family)
            for cell in self.cells
            for family in sorted(families.get(cell.cell, ())) or [None]
        ]
        _fail(len(set(planned)) != len(planned), self, "a cell is planned once, a live cell once for a family")
        _fail(any(one.trial != at for at, one in enumerate(self.trials)), self, "a trial's ordinal is its position")
        for trial in self.trials:
            live = trial.cell < len(self.cells) and self.cells[trial.cell].phase in LIVE_PHASES
            _fail(not live, self, "a trial runs in a live cell of the plan")
            _fail(trial.harness not in pinned, self, "a trial's harness is pinned")
        tried = {trial.cell for trial in self.trials}
        idle = any(cell.phase in LIVE_PHASES and cell.cell not in tried for cell in self.cells)
        _fail(idle, self, "a live cell has a trial to run")
        attempts = {dataclasses.replace(trial, trial=0) for trial in self.trials}
        _fail(len(attempts) != len(self.trials), self, "a trial is planned once")


@dataclass(frozen=True, kw_only=True)
class Settings(Record):
    """The serving settings of a pool, as public values. A manifest holds them twice: as requested
    (the treatment) and as observed. `None` is "not declared" in the first and "not observed" in
    the second; neither is ever filled from the other."""

    host_count: Annotated[int, _Integer(1, 1024)] | None
    instance_type: InstanceType | None
    gpu_type: GpuType | None
    gpus_per_host: Annotated[int, _Integer(1, 64)] | None
    model_revision: Commit | None
    tokenizer_revision: Commit | None
    quantization: Quantization | None
    engine_image_digest: Digest | None
    vllm_version: Version | None
    context_limit_tokens: Positive | None
    kv_dtype: KvDtype | None
    prefix_caching: bool | None
    tensor_parallelism: Annotated[int, _Integer(1, 64)] | None
    step_budget_tokens: Positive | None
    speculative_decoding: SpeculativeDecoding | None
    renderer_digest: Digest | None
    gateway_profile_digest: Digest | None

    def check(self) -> None:
        if self.instance_type is not None:
            gpu_type, gpus = INSTANCE_GPUS[self.instance_type]
            _fail(self.gpu_type not in (None, gpu_type), self, "the GPU is the one of the instance type")
            _fail(self.gpus_per_host not in (None, gpus), self, "a host has the GPUs of its instance type")


@dataclass(frozen=True, kw_only=True)
class Run(Record):
    """What ran the measurement: the source, the locked tools and the load driver."""

    ordinal: Ordinal
    source_commit: Commit
    lock_digest: Digest
    harness_image_digest: Digest
    driver_image_digest: Digest
    driver_cpus: Annotated[int, _Integer(1, 1024)]
    driver_memory_mib: Positive
    path_category: PathCategory


def _identity(part: str, value: Record) -> str:
    return _sha256(_canonical({"identity": part, "schema_version": SCHEMA_VERSION, "of": value.to_json()}))


def workload_identity(workload: Workload) -> str:
    """The identity of a planned workload, from the workload alone: what `plan` prints before a
    run exists, and what the manifest of every run of that plan has as its `workload_id`."""
    return _identity("workload", workload)


@dataclass(frozen=True, kw_only=True)
class Manifest(Document):
    """What a run was: the plan, the settings requested for it, what ran it and what it observed
    of the pool that served it."""

    KIND = "manifest"
    workload: Workload
    requested: Settings
    run: Run
    observed: Settings

    @property
    def workload_id(self) -> str:
        """The same for every treatment and run of one plan."""
        return workload_identity(self.workload)

    @property
    def treatment_id(self) -> str:
        """The same for every workload and run the requested settings serve."""
        return _identity("treatment", self.requested)

    @property
    def run_id(self) -> str:
        """One run of one treatment on one workload. What it observed is not part of it."""
        of = {"workload": self.workload_id, "treatment": self.treatment_id, "run": self.run.to_json()}
        return _sha256(_canonical({"identity": "run", "schema_version": SCHEMA_VERSION, "of": of}))


# --- The evidence records --------------------------------------------------------------------


def _ascending(*times: int | None) -> bool:
    """Whether the times that were observed do not go back."""
    observed = [time for time in times if time is not None]
    return all(before <= after for before, after in pairwise(observed))


@dataclass(frozen=True, kw_only=True)
class Usage(Record):
    """The tokens a response reported. Zero is a report. A count that was not reported is `null`
    on its own, whatever the others are; no count at all is no usage, and is `usage: null`."""

    input_tokens: Count | None
    cached_input_tokens: Count | None
    output_tokens: Count | None

    def check(self) -> None:
        counts = (self.input_tokens, self.cached_input_tokens, self.output_tokens)
        _fail(all(count is None for count in counts), self, "with no count at all is written as a null usage")
        whole, cached = self.input_tokens, self.cached_input_tokens
        _fail(whole is not None and cached is not None and cached > whole, self, "cached input is part of the input")


@dataclass(frozen=True, kw_only=True)
class RequestRecord(Document):
    """One request the driver sent. `first_output_ms` is the first output-bearing event,
    reasoning included; `first_answer_ms` the first output that is not reasoning."""

    KIND = "request"
    request: Ordinal
    cell: Ordinal
    student: Ordinal
    conversation: Ordinal
    turn: Ordinal
    traffic: Traffic
    trial: Ordinal | None
    route: Route
    eligible_ms: Millis
    sent_ms: Millis
    first_output_ms: Millis | None
    first_answer_ms: Millis | None
    last_output_ms: Millis | None
    ended_ms: Millis
    outcome: RequestOutcome
    finish_reason: FinishReason | None
    usage: Usage | None

    def check(self) -> None:
        _fail(
            (self.traffic is Traffic.LIVE) != (self.trial is not None), self, "live traffic, and only it, has a trial"
        )
        times = (self.eligible_ms, self.sent_ms, self.first_output_ms, self.last_output_ms, self.ended_ms)
        answer = (self.first_output_ms, self.first_answer_ms, self.last_output_ms)
        _fail(not _ascending(*times) or not _ascending(*answer), self, "times do not go back")
        outputs = (self.first_output_ms is None, self.last_output_ms is None)
        _fail(outputs[0] != outputs[1], self, "a first output has a last output")
        _fail(outputs[0] and self.first_answer_ms is not None, self, "an answer is an output")

    @property
    def failed(self) -> bool:
        return self.outcome is not RequestOutcome.COMPLETED

    @property
    def usage_missing(self) -> bool:
        """Whether the response reported no count at all. One count can still be missing: it is
        `None` in `usage`."""
        return self.usage is None


@dataclass(frozen=True, kw_only=True)
class TrialRecord(Document):
    """One execution of a planned trial. Every execution is kept: one that replaces an invalid or
    interrupted one names it in `replaces`.

    `failed` is a completed attempt that did not pass, and counts. `invalid` is an attempt whose
    measurement broke: it does not count, and its cell is incomplete until it is replaced."""

    KIND = "trial"
    execution: Ordinal
    trial: Ordinal
    replaces: Ordinal | None
    student: Ordinal
    started_ms: Millis
    ended_ms: Millis
    outcome: TrialOutcome
    failure: FailureReason | None
    invalid: InvalidReason | None
    grade: Grade | None
    model_turns: Count | None
    retries: Count | None
    usage: Usage | None

    def check(self) -> None:
        _fail(self.ended_ms < self.started_ms, self, "times do not go back")
        _fail(self.replaces is not None and self.replaces >= self.execution, self, "replaces an earlier execution")
        _fail(
            (self.outcome is TrialOutcome.FAILED) != (self.failure is not None),
            self,
            "failed, and only it, has a failure",
        )
        _fail(
            (self.outcome is TrialOutcome.INVALID) != (self.invalid is not None),
            self,
            "invalid, and only it, has a reason",
        )
        accepted = None if self.grade is None else self.grade.accepted
        _fail(self.outcome is TrialOutcome.INVALID and accepted is not None, self, "an invalid attempt has no grade")
        _fail(
            (self.outcome is TrialOutcome.PASSED) != (accepted is True),
            self,
            "passed, and only it, has an accepted grade",
        )
        _fail((self.failure is FailureReason.REJECTED) and accepted is None, self, "a rejection has its grade")

    @property
    def counts_as_attempt(self) -> bool:
        """Whether the execution is in the denominator of its cell: a pass or a completed failure."""
        return self.outcome is not TrialOutcome.INVALID


@dataclass(frozen=True, kw_only=True)
class HostSample(Document):
    """One reading of one metric on one host. A scrape that gave nothing is `value: null`."""

    KIND = "host_sample"
    cell: Ordinal
    host: Ordinal
    at_ms: Millis
    metric: HostMetric
    unit: Unit
    value: Measurement | None

    def check(self) -> None:
        _fail(self.unit is not METRIC_UNITS[self.metric], self, "the unit is the one of the metric")
        _fail(self.unit is Unit.RATIO and self.value is not None and self.value > 1, self, "a ratio is at most 1")


@dataclass(frozen=True, kw_only=True)
class CellRecord(Document):
    """What a cell of the plan came to: its window, its conversations and its flags. A flagged
    cell is invalid for qualification and keeps every observation. So does a cell that skipped an
    opening (a planned conversation it did not start) or left a start with no outcome (a started
    conversation neither completed nor abandoned when the drain ended)."""

    KIND = "cell"
    cell: Ordinal
    started_ms: Millis
    admissions_ended_ms: Millis
    ended_ms: Millis
    status: CellStatus
    invalid_flags: tuple[InvalidFlag, ...]
    conversations_planned: Count
    conversations_started: Count
    conversations_completed: Count
    conversations_abandoned: Count
    hosts_observed: Count | None
    driver_lag_p95_ms: Measurement | None

    def check(self) -> None:
        _fail(not _ascending(self.started_ms, self.admissions_ended_ms, self.ended_ms), self, "times do not go back")
        _fail(not _in_order(self.invalid_flags), self, "flags are listed once each, in order")
        ended = self.conversations_completed + self.conversations_abandoned
        counted = ended <= self.conversations_started <= self.conversations_planned
        _fail(not counted, self, "conversations end once started, and start once planned")

    @property
    def conversations_skipped(self) -> int:
        return self.conversations_planned - self.conversations_started

    @property
    def conversations_outstanding(self) -> int:
        """The conversations that started and neither completed nor were abandoned by the end of
        the drain: starts with no outcome."""
        return self.conversations_started - self.conversations_completed - self.conversations_abandoned

    @property
    def valid_for_qualification(self) -> bool:
        """Whether the record lets the cell count: complete, every planned conversation started,
        every started one completed or abandoned, and no flag. The driver's lag is kept as
        measured: the record does not hold the tolerance, and whoever writes it flags a lag over
        the tolerance as `driver_saturation`."""
        accounted = not self.conversations_skipped and not self.conversations_outstanding
        return self.status is CellStatus.COMPLETE and not self.invalid_flags and accounted


# --- Writing and reading ---------------------------------------------------------------------

KINDS: dict[str, type[Document]] = {
    document.KIND: document
    for document in (Manifest, Suite, GraderOutcome, RequestRecord, TrialRecord, HostSample, CellRecord)
}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def dumps(document: Document) -> str:
    """The document as one line of canonical JSON: the same bytes for the same document."""
    return _canonical({"kind": document.KIND, "schema_version": SCHEMA_VERSION, **document.to_json()})


def content_hash(document: Document) -> str:
    """The digest of the document's canonical bytes."""
    return _sha256(dumps(document))


def _refuse_constant(_name: str) -> object:
    raise ContractError("a number that is not finite")


def _one_key_each(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ContractError("a key written twice")
    return value


def parse(text: str | bytes) -> Document:
    """The document a text holds, whatever its kind, or a refusal."""
    try:
        value = json.loads(text, parse_constant=_refuse_constant, object_pairs_hook=_one_key_each)
    except ContractError:
        raise
    except (ValueError, RecursionError) as error:
        raise ContractError("not JSON") from error
    if not isinstance(value, dict):
        raise ContractError("needs an object")
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ContractError(f"schema_version: only version {SCHEMA_VERSION} is read")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ContractError("kind: needs the kind of a document")
    body = {key: item for key, item in value.items() if key not in ("kind", "schema_version")}
    return KINDS[kind].from_json(body, kind)


def loads[D: Document](text: str | bytes, expect: type[D]) -> D:
    """The document a text holds, which must be of the expected kind."""
    document = parse(text)
    if not isinstance(document, expect):
        raise ContractError(f"kind: needs a {expect.KIND}")
    return document
