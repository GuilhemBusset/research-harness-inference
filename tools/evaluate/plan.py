"""Plans an evaluation offline: the ordered cells and trials of a profile, and the bookings that
hold them (docs/design/agent-session-evaluation.md, "Booking and running", step 1, and "Workload
and measurement protocol").

`expand` turns a suite (`suites/<name>.toml`), a profile and a class size into a `Plan`: a
`contracts.Workload`, which is what a run's manifest records, and for each of its cells what the
identity of a cell does not say (its roles, admissions, drain, identities, seeds, prefix namespace
and arrivals). It reads the suite, and the replay's course fixture for its digest, and nothing
else: no credentials, no network, no clock, and no random generator, so the same suite and seeds
give the same plan on any machine.

**The workload.** The hash `plan` prints is `contracts.workload_identity` of that workload, the
`workload_id` of every manifest built on it. The arrivals and the rest of what is planned for a
cell are in it through the workload's `arrival_digest` and `protocol_digest`. So is the course
fixture the replay builds its requests from (`REPLAY_FIXTURE`), by its digest in the protocol: a
changed fixture is another workload. It pins the harnesses its trials run on and no other: a
screen, which runs the anchor only, is the same workload whatever version another harness is at. The number of hosts is a treatment and is not in
it: one and two hosts are planned the same workload.

**The order.** The warm-up first, then one round for each repetition: the capacity sweep, the live
tasks at low load, the live tasks at class size, the class opening. The cells of a round start a
part of the way down their list that grows with the repetition, so no cell always follows the same
one. The design asks to batch a qualification "by family and replicate": a round is a replicate,
and a live cell holds the trials of one family at one load, so a booking can end between two
families. The largest cell of a qualification is then the family with the most instance/harness
pairs.

**What is planned once.** A sweep cell at the class size that has the agent slots of a diagnostic
cell is one cell with both roles and the longer admissions. With one seat no other seat can load a
live task, so its live cells are the low-load ones and carry both loads.

**Seeds and the cache policy.** Repetition `r` has an input seed and a schedule seed derived from
the plan's two, and a prefix namespace of its own, which its sweep and live cells share. The
warm-up and each class opening have their own namespace, so an opening is a fresh prefix on a warm
engine, and neither is part of a steady-state summary.

**Arrivals.** The slots of a replay are eligible when their cell starts, as in spike S5. The
conversations of a class opening start at offsets inside the suite's window, derived from the
repetition's schedule seed. A live trial starts when the one before it has finished and has the
trial's wall time as its deadline.

**Batches.** `schedule` packs the cells, in order and whole, into bookings of a given window. A
booking spends the suite's estimates on the wait for capacity, its own warm-up and the cleanup; a
cell takes its admissions, its drain and the wait for its telemetry. A cell that fits no booking
of that window is refused (`Refusal.CELL_OUTSIDE_WINDOW`), and nothing is scheduled.

A refusal is one member of `Refusal`, a fixed vocabulary. It needs the standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evaluate import contracts
from evaluate.contracts import (
    CachePolicy,
    CellIdentity,
    ContractError,
    Family,
    Harness,
    HarnessAssignment,
    HarnessFeature,
    HarnessPin,
    Instance,
    Limits,
    Objectives,
    Phase,
    Profile,
    Route,
    Sampling,
    Shape,
    Suite,
    Task,
    TrialIdentity,
    Workload,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

SUITES = Path(__file__).resolve().parent / "suites"
# The course fixture the replay draws its tool turns from (`traces.COURSE`), by its bytes.
REPLAY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay" / "course.json"
DEFAULT_SUITE = "course-v1"
HOUR_MS = 3_600_000
# Several-agents doubles the slots of a class, and a cell has 1024 at most.
MAX_STUDENTS = 512

_SUITE_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# The sections of a suite that describe how the workload is driven: they are in its
# `protocol_digest`, with the profile that was planned and what was planned for each cell.
_PROTOCOL = ("anchor", "shapes", "replay", "warm_up", "sweep", "opening", "driver")


class Refusal(StrEnum):
    """Why nothing was planned."""

    UNKNOWN_SUITE = "unknown-suite"
    INVALID_SUITE = "invalid-suite"
    INVALID_REQUEST = "invalid-request"
    CELL_OUTSIDE_WINDOW = "cell-outside-window"


class PlanError(Exception):
    """A plan that is refused, with its reason."""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Role(StrEnum):
    """What a cell is for. A cell planned once for two purposes has both roles."""

    WARM_UP = "warm_up"
    DIAGNOSTIC = "diagnostic"
    CLASS_FLOOR = "class_floor"
    LOW_LOAD = "low_load"
    CLASS_LOAD = "class_load"
    OPENING = "opening"


LOADS = (Role.LOW_LOAD, Role.CLASS_LOAD)


@dataclass(frozen=True)
class ProfileSpec:
    """What a profile runs of the suite."""

    repetitions: int
    attempts: int
    loads: tuple[Role, ...]
    core_only: bool
    anchor_only: bool
    instances_per_task: int
    table: dict[str, Any]


@dataclass(frozen=True)
class Estimates:
    """What a booking is sized with. None of it is part of the workload."""

    capacity_wait_ms: int
    warm_up_ms: int
    telemetry_settle_ms: int
    cleanup_ms: int
    trial_workspace_mib: int
    cell_evidence_mib: int
    trial_evidence_mib: int


@dataclass(frozen=True)
class SuiteSpec:
    """A suite as the planner reads it."""

    name: str
    suite: Suite
    anchor: Harness
    core: frozenset[str]
    background: dict[str, Shape]
    pins: tuple[HarnessPin, ...]
    input_seed: int
    schedule_seed: int
    cache_policy: CachePolicy
    limits: Limits
    sampling: Sampling
    objectives: Objectives
    conversations: dict[Shape, int]
    warm_up: tuple[Shape, int]
    sweep_slots: tuple[int, ...]
    diagnostic_admissions_ms: int
    class_admissions_ms: int
    opening_window_ms: int
    profiles: dict[Profile, ProfileSpec]
    estimates: Estimates
    protocol: dict[str, Any]

    def pairs(self, profile: Profile) -> list[tuple[Task, str, Harness]]:
        """The instance/harness pairs the profile requires, each with its task."""
        chosen = self.profiles[profile]
        found = []
        for task in self.suite.tasks:
            if chosen.core_only and task.task_id not in self.core:
                continue
            instances = task.instances[: chosen.instances_per_task or None]
            assigned = [assignment.harness for assignment in task.harnesses]
            harnesses = [self.anchor] if chosen.anchor_only else assigned
            found += [(task, instance.instance_id, harness) for instance in instances for harness in harnesses]
        return found

    def families(self, profile: Profile) -> list[tuple[Family, Shape, list[tuple[str, Harness]]]]:
        """The families the profile runs, in the suite's order, each with the shape of its seats
        and the instance/harness pairs of one attempt: what one live cell holds."""
        found: dict[Family, tuple[Family, Shape, list[tuple[str, Harness]]]] = {}
        for task, instance, harness in self.pairs(profile):
            family = found.setdefault(task.family, (task.family, self.background[task.task_id], []))
            family[2].append((instance, harness))
        return list(found.values())


@dataclass(frozen=True)
class PlannedCell:
    """A cell of the plan: its identity in the workload, and what is planned for it."""

    identity: CellIdentity
    roles: tuple[Role, ...]
    # The canary identities the cell needs at once, and the slots of it a replay drives.
    identities: int
    replay_slots: int
    # How long the cell admits work. A live cell admits its trials one after the other, each for
    # its wall time at most; the warm-up runs until each host shows work, which only a booking
    # can estimate.
    admissions_ms: int
    drain_ms: int
    input_seed: int
    schedule_seed: int
    prefix_namespace: int
    # Whether its samples may enter a steady-state summary: not the warm-up's, not an opening's.
    steady_state: bool
    # When each replay conversation is eligible to start, from the start of the cell.
    arrivals_ms: tuple[int, ...]
    trials: tuple[int, ...]

    def planned(self) -> dict[str, object]:
        """What is planned for the cell beyond its identity and its arrivals."""
        return {
            "cell": self.identity.cell,
            "roles": list(self.roles),
            "identities": self.identities,
            "replay_slots": self.replay_slots,
            "admissions_ms": self.admissions_ms,
            "drain_ms": self.drain_ms,
            "input_seed": self.input_seed,
            "schedule_seed": self.schedule_seed,
            "prefix_namespace": self.prefix_namespace,
            "steady_state": self.steady_state,
        }


@dataclass(frozen=True)
class Plan:
    """An expanded profile: the workload a manifest records, and its cells in order."""

    spec: SuiteSpec
    workload: Workload
    cells: tuple[PlannedCell, ...]

    @property
    def workload_hash(self) -> str:
        return contracts.workload_identity(self.workload)

    def loads_of(self, trial: TrialIdentity) -> tuple[Role, ...]:
        return self.cells[trial.cell].roles

    @property
    def trials_by_load(self) -> dict[Role, int]:
        """The trials that count for each load. With one seat a trial counts for both."""
        return {load: sum(load in self.loads_of(trial) for trial in self.workload.trials) for load in LOADS}

    @property
    def canary_identities(self) -> int:
        """The distinct canary identities the plan needs: cells run one at a time, so the most
        any one cell needs."""
        return max(cell.identities for cell in self.cells)

    @property
    def agent_slots(self) -> int:
        """The most agents that run at once."""
        return max(cell.identity.agent_slots for cell in self.cells)

    def reserved_ms(self, cell: PlannedCell) -> int:
        """The time a booking keeps for the cell: its admissions (for the warm-up, the suite's
        estimate), its drain, and the wait for its telemetry."""
        estimates = self.spec.estimates
        active = estimates.warm_up_ms if cell.identity.phase is Phase.WARM_UP else cell.admissions_ms
        return active + cell.drain_ms + estimates.telemetry_settle_ms

    @property
    def scratch_mib(self) -> int:
        """The local space to keep free: the workspace of the one trial that runs at a time, and
        the evidence of every cell and trial."""
        estimates = self.spec.estimates
        trials = len(self.workload.trials)
        workspace = estimates.trial_workspace_mib if trials else 0
        return workspace + len(self.cells) * estimates.cell_evidence_mib + trials * estimates.trial_evidence_mib


@dataclass(frozen=True)
class Batch:
    """The cells one booking runs after its warm-up, and the shortest window that holds them."""

    batch: int
    cells: tuple[int, ...]
    minimum_window_ms: int


@dataclass(frozen=True)
class Schedule:
    """The bookings of a plan for one window."""

    window_ms: int
    # What every booking spends besides its cells: the capacity wait, its warm-up and the cleanup.
    overhead_ms: int
    # The shortest window that holds each cell of the plan in some booking.
    minimum_window_ms: int
    batches: tuple[Batch, ...]

    @property
    def booked_ms(self) -> int:
        """How long a host is booked for, when each booking is its batch's minimum window."""
        return sum(batch.minimum_window_ms for batch in self.batches)

    def host_hours(self, instances: int) -> float:
        """The hours the hosts are booked for."""
        return self.booked_ms * instances / HOUR_MS

    def price_usd(self, instances: int, usd_per_host_hour: float) -> float:
        """What the booked time costs at that rate, to the cent: from the booked time itself, not
        from host-hours already rounded."""
        return round(self.booked_ms * instances * usd_per_host_hour / HOUR_MS, 2)


# --- Reading a suite -------------------------------------------------------------------------


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    """The digest of a file's bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _derive(*parts: object) -> int:
    """A seed, or any 32-bit number, that depends on every part and on nothing else."""
    return int(_digest(list(parts))[7:15], 16)


def _whole(value: object, low: int = 0) -> int:
    if type(value) is not int or value < low:
        raise ValueError("needs a whole number")
    return value


def _profile(table: dict[str, Any]) -> ProfileSpec:
    choices = {"tasks": ("core", "all"), "harnesses": ("anchor", "assigned")}
    if any(table[key] not in allowed for key, allowed in choices.items()):
        raise ValueError("an unknown choice of tasks or harnesses")
    loads = tuple(Role(load) for load in table["loads"])
    if not loads or any(load not in LOADS for load in loads) or len(set(loads)) != len(loads):
        raise ValueError("needs one or both loads, once each")
    return ProfileSpec(
        repetitions=_whole(table["repetitions"], 1),
        attempts=_whole(table["attempts"], 1),
        loads=loads,
        core_only=table["tasks"] == "core",
        anchor_only=table["harnesses"] == "anchor",
        instances_per_task=_whole(table["instances_per_task"]),
        table=table,
    )


def _read(name: str, raw: dict[str, Any]) -> SuiteSpec:
    supported = {
        Harness(key): {HarnessFeature(one) for one in table["features"]} for key, table in raw["harnesses"].items()
    }
    pins = []
    for harness in sorted(supported):
        table = raw["harnesses"][harness]
        Route(table["route"])
        pins.append(HarnessPin(harness=harness, version=table["version"], profile_digest=_digest(table)))
    tasks, background, core = [], {}, set()
    for table in raw["tasks"]:
        features = tuple(HarnessFeature(one) for one in table["features"])
        harnesses = [Harness(one) for one in table["harnesses"]]
        if any(not supported[harness].issuperset(features) for harness in harnesses):
            raise ValueError("a task is assigned to a harness without a feature it needs")
        task = Task(
            task_id=table["id"],
            required_properties=table["required_properties"],
            instances=tuple(
                Instance(
                    instance_id=one["id"], fixture_digest=one["fixture_digest"], grader_digest=one["grader_digest"]
                )
                for one in table["instances"]
            ),
            harnesses=tuple(HarnessAssignment(harness=harness, features=features) for harness in harnesses),
        )
        tasks.append(task)
        background[task.task_id] = Shape(table["background"])
        if table["core"]:
            core.add(task.task_id)
    anchor = Harness(raw["anchor"])
    if any(anchor not in [assignment.harness for assignment in task.harnesses] for task in tasks):
        raise ValueError("the anchor harness runs every task")
    if len({(task.family, background[task.task_id]) for task in tasks}) != len({task.family for task in tasks}):
        raise ValueError("the tasks of a family have one background shape")
    sweep = raw["sweep"]
    return SuiteSpec(
        name=name,
        suite=Suite(version=raw["version"], tasks=tuple(tasks)),
        anchor=anchor,
        core=frozenset(core),
        background=background,
        pins=tuple(pins),
        input_seed=raw["seeds"]["input"],
        schedule_seed=raw["seeds"]["schedule"],
        cache_policy=CachePolicy(raw["cache"]["policy"]),
        limits=Limits(**raw["limits"]),
        sampling=Sampling(**raw["sampling"]),
        objectives=Objectives(**raw["objectives"]),
        conversations={shape: _whole(raw["shapes"][shape], 1) for shape in Shape},
        warm_up=(Shape(raw["warm_up"]["shape"]), _whole(raw["warm_up"]["agent_slots"], 1)),
        sweep_slots=tuple(_whole(slots, 1) for slots in sweep["agent_slots"]),
        diagnostic_admissions_ms=_whole(sweep["diagnostic_admissions_ms"]),
        class_admissions_ms=_whole(sweep["class_admissions_ms"]),
        opening_window_ms=_whole(raw["opening"]["window_ms"], 1),
        profiles={profile: _profile(raw["profiles"][profile]) for profile in Profile},
        estimates=Estimates(**{key: _whole(value) for key, value in raw["estimates"].items()}),
        protocol={key: raw[key] for key in _PROTOCOL} | {"replay_fixture_digest": file_digest(REPLAY_FIXTURE)},
    )


def load_suite(name: str = DEFAULT_SUITE) -> SuiteSpec:
    """The suite of that name. `PlanError` for a name that is none, or a file that is not a suite."""
    if not _SUITE_NAME.fullmatch(name):
        raise PlanError(Refusal.UNKNOWN_SUITE)
    try:
        text = (SUITES / f"{name}.toml").read_text(encoding="utf-8")
    except OSError:
        raise PlanError(Refusal.UNKNOWN_SUITE) from None
    try:
        return _read(name, tomllib.loads(text))
    except (ContractError, ValueError, KeyError, TypeError):
        # The reason is fixed: what a file holds is not repeated.
        raise PlanError(Refusal.INVALID_SUITE) from None


# --- Expanding a profile ---------------------------------------------------------------------


def _from(items: list[Any], repetition: int, repetitions: int) -> list[Any]:
    """The list, started a part of the way down that grows with the repetition."""
    at = repetition * len(items) // repetitions
    return items[at:] + items[:at]


def _identities(spec: SuiteSpec, shape: Shape, agent_slots: int) -> int:
    return -(-agent_slots // spec.conversations[shape])


def _sweep(spec: SuiteSpec, students: int) -> list[tuple[Shape, int, tuple[Role, ...]]]:
    """The sweep of one repetition: each shape at the suite's slots, then at the class size, a
    cell that is both planned once with both roles."""
    cells = []
    for shape in Shape:
        of_class = students * spec.conversations[shape]
        for agent_slots in (*spec.sweep_slots, *([] if of_class in spec.sweep_slots else [of_class])):
            diagnostic = (Role.DIAGNOSTIC,) if agent_slots in spec.sweep_slots else ()
            floor = (Role.CLASS_FLOOR,) if agent_slots == of_class else ()
            cells.append((shape, agent_slots, diagnostic + floor))
    return cells


class _Expansion:
    """The cells and trials of a plan, as they are added in order."""

    def __init__(self, spec: SuiteSpec, input_seed: int, schedule_seed: int) -> None:
        self.spec = spec
        self.seeds = (input_seed, schedule_seed)
        self.cells: list[PlannedCell] = []
        self.trials: list[TrialIdentity] = []
        self.namespaces = 0

    def namespace(self) -> int:
        self.namespaces += 1
        return self.namespaces - 1

    def seeds_of(self, purpose: str, repetition: int) -> tuple[int, int]:
        return (
            _derive("input", self.seeds[0], purpose, repetition),
            _derive("schedule", self.seeds[1], purpose, repetition),
        )

    def add(
        self,
        phase: Phase,
        shape: Shape,
        agent_slots: int,
        repetition: int,
        roles: tuple[Role, ...],
        *,
        identities: int,
        replay_slots: int,
        admissions_ms: int,
        seeds: tuple[int, int],
        namespace: int,
        arrivals_ms: tuple[int, ...] | None = None,
        attempts: list[tuple[str, Harness]] | None = None,
    ) -> None:
        cell = len(self.cells)
        first = len(self.trials)
        self.trials += [
            TrialIdentity(trial=first + at, cell=cell, instance_id=instance, harness=harness, attempt=repetition)
            for at, (instance, harness) in enumerate(attempts or [])
        ]
        self.cells.append(
            PlannedCell(
                identity=CellIdentity(
                    cell=cell, phase=phase, shape=shape, agent_slots=agent_slots, repetition=repetition
                ),
                roles=roles,
                identities=identities,
                replay_slots=replay_slots,
                admissions_ms=admissions_ms,
                drain_ms=0 if phase is Phase.WARM_UP else self.spec.limits.drain_ms,
                input_seed=seeds[0],
                schedule_seed=seeds[1],
                prefix_namespace=namespace,
                steady_state=phase not in (Phase.WARM_UP, Phase.CLASS_OPENING),
                arrivals_ms=(0,) * replay_slots if arrivals_ms is None else arrivals_ms,
                trials=tuple(range(first, len(self.trials))),
            )
        )


def _digests(spec: SuiteSpec, profile: Profile, cells: Sequence[PlannedCell]) -> tuple[str, str, str]:
    """The suite, arrival and protocol digests of a workload: of the suite, of the arrivals of
    its cells, and of the suite's protocol with the profile and what was planned for each cell."""
    protocol = spec.protocol | {
        "profile": spec.profiles[profile].table,
        "background": {task: spec.background[task] for task in sorted(spec.background)},
        "core": sorted(spec.core),
        "cells": [cell.planned() for cell in cells],
    }
    arrivals = _digest([list(cell.arrivals_ms) for cell in cells])
    return contracts.content_hash(spec.suite), arrivals, _digest(protocol)


def digests_hold(plan: Plan) -> bool:
    """Whether the plan's workload still names what its spec and its cells would send: the suite,
    arrival and protocol digests computed again from them are the ones the workload froze."""
    workload = plan.workload
    frozen = (workload.suite_digest, workload.arrival_digest, workload.protocol_digest)
    return _digests(plan.spec, workload.profile, plan.cells) == frozen


def expand(
    spec: SuiteSpec,
    profile: Profile,
    students: int,
    *,
    input_seed: int | None = None,
    schedule_seed: int | None = None,
) -> Plan:
    """The plan of a profile for a class of `students`, under the suite's seeds or the given ones."""
    input_seed = spec.input_seed if input_seed is None else input_seed
    schedule_seed = spec.schedule_seed if schedule_seed is None else schedule_seed
    seeds_fit = all(type(seed) is int and 0 <= seed < 2**32 for seed in (input_seed, schedule_seed))
    if type(students) is not int or not 1 <= students <= MAX_STUDENTS or not seeds_fit:
        raise PlanError(Refusal.INVALID_REQUEST)
    chosen = spec.profiles[profile]
    plan = _Expansion(spec, input_seed, schedule_seed)
    per = spec.conversations
    families = spec.families(profile)
    # With one seat there is no other seat to load a live task: both loads are the low-load cell.
    one_seat = students == 1
    loads = [Role.LOW_LOAD] if one_seat else list(chosen.loads)

    shape, agent_slots = spec.warm_up
    plan.add(
        Phase.WARM_UP,
        shape,
        agent_slots,
        0,
        (Role.WARM_UP,),
        identities=_identities(spec, shape, agent_slots),
        replay_slots=agent_slots,
        admissions_ms=0,
        seeds=plan.seeds_of("warm_up", 0),
        namespace=plan.namespace(),
    )
    rounds = max(chosen.repetitions, chosen.attempts)
    steady = [plan.namespace() for _round in range(rounds)]
    for at in range(rounds):
        seeds = plan.seeds_of("repetition", at)
        if at < chosen.repetitions:
            for shape, agent_slots, roles in _from(_sweep(spec, students), at, chosen.repetitions):
                floor = Role.CLASS_FLOOR in roles
                plan.add(
                    Phase.CAPACITY_SWEEP,
                    shape,
                    agent_slots,
                    at,
                    roles,
                    identities=_identities(spec, shape, agent_slots),
                    replay_slots=agent_slots,
                    admissions_ms=spec.class_admissions_ms if floor else spec.diagnostic_admissions_ms,
                    seeds=seeds,
                    namespace=steady[at],
                )
        for load in loads if at < chosen.attempts else []:
            for _family, shape, attempts in _from(families, at, chosen.attempts):
                # One seat runs the task. At class size the other seats keep up the replay.
                background = 0 if load is Role.LOW_LOAD else students - 1
                plan.add(
                    Phase.LIVE_LOW_LOAD if load is Role.LOW_LOAD else Phase.LIVE_CLASS_LOAD,
                    shape,
                    (1 + background) * per[shape],
                    at,
                    LOADS if one_seat else (load,),
                    identities=1 + background,
                    replay_slots=background * per[shape],
                    admissions_ms=len(attempts) * spec.limits.trial_wall_time_ms,
                    seeds=seeds,
                    namespace=steady[at],
                    attempts=attempts,
                )
        if at < chosen.repetitions:
            for shape in _from(list(Shape), at, chosen.repetitions):
                agent_slots = students * per[shape]
                starts = (
                    _derive("arrival", seeds[1], shape, slot) % spec.opening_window_ms for slot in range(agent_slots)
                )
                plan.add(
                    Phase.CLASS_OPENING,
                    shape,
                    agent_slots,
                    at,
                    (Role.OPENING,),
                    identities=students,
                    replay_slots=agent_slots,
                    admissions_ms=spec.opening_window_ms,
                    seeds=seeds,
                    namespace=plan.namespace(),
                    arrivals_ms=tuple(sorted(starts)),
                )

    cells = tuple(plan.cells)
    repeated = [cell for cell in cells if cell.identity.phase is Phase.CAPACITY_SWEEP]
    for seeds_used in ({cell.input_seed for cell in repeated}, {cell.schedule_seed for cell in repeated}):
        if len(seeds_used) != chosen.repetitions:
            # Two repetitions on one seed would be, in part, one measurement made twice.
            raise PlanError(Refusal.INVALID_REQUEST)
    scheduled = {trial.harness for trial in plan.trials}
    suite_digest, arrival_digest, protocol_digest = _digests(spec, profile, cells)
    try:
        workload = Workload(
            profile=profile,
            students=students,
            suite_digest=suite_digest,
            arrival_digest=arrival_digest,
            protocol_digest=protocol_digest,
            input_seed=input_seed,
            schedule_seed=schedule_seed,
            cache_policy=spec.cache_policy,
            harnesses=tuple(pin for pin in spec.pins if pin.harness in scheduled),
            limits=spec.limits,
            sampling=spec.sampling,
            objectives=spec.objectives,
            cells=tuple(cell.identity for cell in cells),
            trials=tuple(plan.trials),
        )
    except ContractError:
        raise PlanError(Refusal.INVALID_SUITE) from None
    return Plan(spec=spec, workload=workload, cells=cells)


# --- Choosing the batches --------------------------------------------------------------------


def schedule(plan: Plan, window_ms: int) -> Schedule:
    """The bookings of at most `window_ms` that run the plan: its cells in order, each whole and
    in one booking. `PlanError` when a cell fits no booking of that window."""
    estimates = plan.spec.estimates
    warm_up, *measured = plan.cells
    overhead = estimates.capacity_wait_ms + plan.reserved_ms(warm_up) + estimates.cleanup_ms
    longest = max(plan.reserved_ms(cell) for cell in measured)
    if overhead + longest > window_ms:
        raise PlanError(Refusal.CELL_OUTSIDE_WINDOW)
    batches: list[Batch] = []
    held: list[int] = []
    used = overhead
    for cell in measured:
        needs = plan.reserved_ms(cell)
        if held and used + needs > window_ms:
            batches.append(Batch(batch=len(batches), cells=tuple(held), minimum_window_ms=used))
            held, used = [], overhead
        held.append(cell.identity.cell)
        used += needs
    batches.append(Batch(batch=len(batches), cells=tuple(held), minimum_window_ms=used))
    return Schedule(
        window_ms=window_ms,
        overhead_ms=overhead,
        minimum_window_ms=overhead + longest,
        batches=tuple(batches),
    )
