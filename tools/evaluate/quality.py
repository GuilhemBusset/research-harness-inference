"""Reads the course quality of a run, and decides a candidate against a reference
(docs/design/agent-session-evaluation.md, "Reading a result").

Everything here is computed from the public records of a run (`Records`): the planned workload of
its manifest, its trial records and its cell records. It reads no file, no clock and no network,
so a saved bundle gives the same reading on any machine. The floor, the margin and the number of
bootstrap draws are the ones the workload declares (`objectives.success_share_min`,
`objectives.paired_margin_points`, `sampling.bootstrap_draws`): they are course policy, frozen in
the workload's identity, and none is a constant of this module.

**The loads.** A live cell of the plan is read at the load of its phase. With one seat no other
seat can load a task, so the plan has low-load cells only and each carries both loads
(`evaluate.plan`): its trials are read at low load and at class load, once in each reading. A
load the plan has no trial for has no reading, and its paired decisions are inconclusive.

**What counts.** Every execution of a trial is kept, and one planned trial counts once, through
the execution that no later execution of that trial replaces. It counts when that execution
completed, as a pass or a failure, in a cell of the plan with a record that is valid for
qualification. An invalid execution that was not replaced, a trial with no execution, one with
several that do not replace each other, and a trial of a cell that is flagged, incomplete or
without a record do not count: the trial is then missing, never a failure and never a pass.

**A quality cell** (`CellQuality`) is a family on a harness at a load. It has its successes over
its counted attempts, the instances that passed every planned repeat, and the floor: met or
missed on the share of successful attempts when every planned trial counts, inconclusive
otherwise.

**The weighted rate** averages success over the counted attempts of each instance, then over the
instances of a family/harness, then equally over the harnesses of a family. The two families of a
shape weigh the same, and the two shapes weigh the same in the combined result. A rate that lacks
an instance, a family or a shape is `None`.

**Time and cost** (`Throughput`) are read for a load and for each family in it: the mean time of
a successful task, successful tasks per elapsed hour of the live cells, and a host cost for each
success. The cost is the elapsed instance-hours of those cells, each record's window times the
hosts it observed, at a host price the caller gives: failed and replaced work and the background
traffic of a loaded cell are paid for like the rest. It is an estimate from host time, which its
field names say. With no success the two ratios are `None` and the counts, the hours and the cost
are still given. A cell with no record, or a record with no host count, leaves what depends on it
`None`. A live cell that ran two families is in the time of each.

**The paired decision** (`compare`) is made for each shape at each load, and for the combined
result. The two runs must be of one workload (`contracts.workload_identity`): it holds the task
inputs, the harnesses, the seeds, the background load and the limits, so the same identity is
what makes two observations a matched pair. Under two identities nothing is compared and every
decision is inconclusive. Under one, the difference is candidate minus reference, in percentage
points of the weighted rate, on the planned trials that count in both runs; a family with a
planned trial that does not count in one of them makes its shape inconclusive.

The interval is a bootstrap. A draw resamples the instances of each family, with all their
harnesses, and within each sampled instance the repeat indices, the same ones for both runs and
for every harness. The generator of a family is seeded from the workload's identity and the
family, and only its `random()` is used, whose sequence Python keeps: the same records give the
same interval. The bounds are the 2.5th and 97.5th percentiles of the draws, interpolated as
`common.percentile` does. A lower bound at or above the margin passes, an interval wholly below
it fails, and one that spans it is inconclusive. When every draw gives the same difference there
is no interval: the observed difference is reported and the decision is inconclusive.

Differences are kept as whole numbers over one denominator until they are reported, so that
equal differences are equal and a bound on the margin is exactly on it.

A trial or cell record of nothing the workload plans is refused (`QualityError`). It needs
`spikes` on the path, for `common.percentile`, and what `spikes/common.py` imports.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum
from fractions import Fraction
from random import Random
from typing import TYPE_CHECKING

from common import percentile
from evaluate.contracts import Family, Harness, Phase, Shape, TrialOutcome, workload_identity

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from evaluate.contracts import CellRecord, TrialIdentity, TrialRecord, Workload

HOUR_MS = 3_600_000
# The bounds of the 95% interval, as percentiles of the bootstrap draws.
LOWER_PERCENTILE, UPPER_PERCENTILE = 2.5, 97.5


class Load(StrEnum):
    """The two conditions a task is run under, named as the roles of `evaluate.plan`."""

    LOW = "low_load"
    CLASS = "class_load"


# The families each course shape requires.
SHAPE_FAMILIES = {
    Shape.HANDS_ON: (Family.OPTIMIZATION, Family.DEBUGGING),
    Shape.SEVERAL_AGENTS: (Family.ANALYSIS, Family.RESEARCH),
}


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class Reason(StrEnum):
    """Why a paired decision is inconclusive."""

    # The two runs are not of one workload: nothing matches their observations.
    UNMATCHED_WORKLOAD = "unmatched-workload"
    # A planned trial does not count in one of the runs, or the plan has none at that load.
    MISSING_PAIRS = "missing-pairs"
    SPANS_MARGIN = "spans-margin"
    # Every resample gave the same difference: there is no interval to read.
    DEGENERATE_PRECISION = "degenerate-precision"


class QualityError(ValueError):
    """Records that are not of their workload. The message names the rule, never a value."""


@dataclass(frozen=True)
class Records:
    """What a run left to read its quality from: the workload of its manifest, every execution of
    its trials, and the records of its cells."""

    workload: Workload
    trials: tuple[TrialRecord, ...]
    cells: tuple[CellRecord, ...]


@dataclass(frozen=True)
class CellQuality:
    """A family on a harness at a load: `successes` over `attempts`, the counted ones of the
    `planned` trials. `executions` is every execution kept for them, replaced and invalid ones
    included. `rate` is the mean over its instances of each one's share of successes."""

    family: Family
    harness: Harness
    load: Load
    planned: int
    executions: int
    attempts: int
    successes: int
    instances: int
    # The instances that passed every planned repeat.
    instances_all_passed: int
    rate: float | None
    floor: float

    @property
    def complete(self) -> bool:
        return self.attempts == self.planned

    @property
    def success_share(self) -> float | None:
        return self.successes / self.attempts if self.attempts else None

    @property
    def floor_outcome(self) -> Outcome:
        """The absolute floor on the share of successful attempts. A cell with a planned trial
        that does not count is inconclusive, whatever the others did."""
        if not self.complete:
            return Outcome.INCONCLUSIVE
        return Outcome.PASS if self.successes / self.attempts >= self.floor else Outcome.FAIL


@dataclass(frozen=True)
class Throughput:
    """The time and the host cost of live trials. The cost is an estimate from host time: elapsed
    instance-hours at the given host price, whatever ran in them."""

    attempts: int
    successes: int
    # The mean time of a successful task, tool waits included.
    successful_task_time_ms: float | None
    elapsed_hours: float | None
    instance_hours: float | None
    tasks_per_hour: float | None
    host_cost_estimate_usd: float | None
    host_cost_estimate_per_success_usd: float | None


@dataclass(frozen=True)
class FamilyReading:
    """A family at a load: its rate, equal over its harnesses, and its time and cost."""

    family: Family
    load: Load
    rate: float | None
    throughput: Throughput


@dataclass(frozen=True)
class LoadReading:
    """The quality of a run at one load. `shapes` and `combined` are the weighted rates."""

    load: Load
    cells: tuple[CellQuality, ...]
    families: tuple[FamilyReading, ...]
    shapes: dict[Shape, float | None]
    combined: float | None
    throughput: Throughput

    def cell(self, family: Family, harness: Harness) -> CellQuality:
        return next(cell for cell in self.cells if (cell.family, cell.harness) == (family, harness))

    def family(self, family: Family) -> FamilyReading:
        return next(reading for reading in self.families if reading.family is family)


@dataclass(frozen=True)
class Decision:
    """The paired decision of a shape at a load, or of the combined result (`shape` is `None`).
    The difference and the bounds are candidate minus reference, in percentage points. The margin
    and the draws are the ones the workload declares, whether or not anything was compared."""

    load: Load
    shape: Shape | None
    outcome: Outcome
    reason: Reason | None
    difference_points: float | None
    lower_points: float | None
    upper_points: float | None
    margin_points: float
    draws: int


@dataclass(frozen=True)
class Comparison:
    """A candidate against a reference: whether they are of one workload, and each decision."""

    matched: bool
    decisions: tuple[Decision, ...]

    def of(self, load: Load, shape: Shape | None = None) -> Decision:
        return next(decision for decision in self.decisions if (decision.load, decision.shape) == (load, shape))


# --- What counts ------------------------------------------------------------------------------


def loads_of(workload: Workload, cell: int) -> tuple[Load, ...]:
    """The loads a cell of the plan is read at: none unless it is live, and both for the live
    cells of one seat."""
    phase = workload.cells[cell].phase
    if phase is Phase.LIVE_CLASS_LOAD:
        return (Load.CLASS,)
    if phase is Phase.LIVE_LOW_LOAD:
        return (Load.LOW, Load.CLASS) if workload.students == 1 else (Load.LOW,)
    return ()


def _family(trial: TrialIdentity) -> Family:
    return Family(trial.instance_id.split(".")[0])


def _planned(workload: Workload, load: Load) -> list[TrialIdentity]:
    return [trial for trial in workload.trials if load in loads_of(workload, trial.cell)]


def _counted(records: Records) -> dict[int, TrialRecord]:
    """The execution that counts for each planned trial that has one."""
    workload = records.workload
    if any(record.cell >= len(workload.cells) for record in records.cells):
        raise QualityError("a cell record is of a cell of the plan")
    measured = {record.cell for record in records.cells if record.valid_for_qualification}
    executions: dict[int, list[TrialRecord]] = {}
    for record in records.trials:
        if record.trial >= len(workload.trials):
            raise QualityError("a trial record is of a trial of the plan")
        executions.setdefault(record.trial, []).append(record)
    counted = {}
    for trial, kept in executions.items():
        replaced = {record.replaces for record in kept}
        last = [record for record in kept if record.execution not in replaced]
        if len(last) == 1 and last[0].counts_as_attempt and workload.trials[trial].cell in measured:
            counted[trial] = last[0]
    return counted


# --- Reading a run ----------------------------------------------------------------------------


def _mean(values: Sequence[Fraction | None]) -> Fraction | None:
    """The equal-weight mean, or nothing when a part is missing."""
    known = [value for value in values if value is not None]
    return sum(known, Fraction(0)) / len(known) if known and len(known) == len(values) else None


def _number(value: Fraction | None) -> float | None:
    return None if value is None else float(value)


def _cell(
    key: tuple[Family, Harness, Load],
    trials: list[TrialIdentity],
    counted: dict[int, TrialRecord],
    executions: Counter[int],
    floor: float,
) -> tuple[CellQuality, Fraction | None]:
    """A quality cell, and its rate as an exact number."""
    instances: dict[str, list[TrialIdentity]] = {}
    for trial in trials:
        instances.setdefault(trial.instance_id, []).append(trial)
    shares: list[Fraction | None] = []
    all_passed = 0
    for repeats in instances.values():
        outcomes = [counted[trial.trial].outcome for trial in repeats if trial.trial in counted]
        passed = sum(outcome is TrialOutcome.PASSED for outcome in outcomes)
        shares.append(Fraction(passed, len(outcomes)) if outcomes else None)
        all_passed += passed == len(repeats)
    attempts = [counted[trial.trial] for trial in trials if trial.trial in counted]
    rate = _mean(shares)
    family, harness, load = key
    cell = CellQuality(
        family=family,
        harness=harness,
        load=load,
        planned=len(trials),
        executions=sum(executions[trial.trial] for trial in trials),
        attempts=len(attempts),
        successes=sum(record.outcome is TrialOutcome.PASSED for record in attempts),
        instances=len(instances),
        instances_all_passed=all_passed,
        rate=_number(rate),
        floor=floor,
    )
    return cell, rate


def _throughput(
    records: Records, counted: dict[int, TrialRecord], trials: list[TrialIdentity], usd_per_host_hour: float
) -> Throughput:
    cells = {trial.cell for trial in trials}
    windows = [record for record in records.cells if record.cell in cells]
    elapsed_ms: int | None = 0
    instance_ms: int | None = 0
    if cells != {record.cell for record in windows}:
        elapsed_ms = instance_ms = None
    for record in windows:
        window = record.ended_ms - record.started_ms
        hosts = record.hosts_observed
        elapsed_ms = None if elapsed_ms is None else elapsed_ms + window
        instance_ms = None if instance_ms is None or hosts is None else instance_ms + window * hosts
    attempts = [counted[trial.trial] for trial in trials if trial.trial in counted]
    times = [record.ended_ms - record.started_ms for record in attempts if record.outcome is TrialOutcome.PASSED]
    successes = len(times)
    cost = None if instance_ms is None else instance_ms / HOUR_MS * usd_per_host_hour
    return Throughput(
        attempts=len(attempts),
        successes=successes,
        successful_task_time_ms=sum(times) / successes if successes else None,
        elapsed_hours=None if elapsed_ms is None else elapsed_ms / HOUR_MS,
        instance_hours=None if instance_ms is None else instance_ms / HOUR_MS,
        tasks_per_hour=successes * HOUR_MS / elapsed_ms if elapsed_ms else None,
        host_cost_estimate_usd=cost,
        host_cost_estimate_per_success_usd=cost / successes if cost is not None and successes else None,
    )


def read(records: Records, *, usd_per_host_hour: float) -> dict[Load, LoadReading]:
    """The quality of a run at each load its plan has a trial for. `usd_per_host_hour` is the
    configured price of one host for one hour, which the host cost is estimated with."""
    workload = records.workload
    counted = _counted(records)
    executions = Counter(record.trial for record in records.trials)
    readings = {}
    for load in Load:
        planned = _planned(workload, load)
        if not planned:
            continue
        grouped: dict[tuple[Family, Harness, Load], list[TrialIdentity]] = {}
        for trial in planned:
            grouped.setdefault((_family(trial), trial.harness, load), []).append(trial)
        cells = []
        harness_rates: dict[Family, list[Fraction | None]] = {}
        for key in sorted(grouped):
            cell, rate = _cell(key, grouped[key], counted, executions, workload.objectives.success_share_min)
            cells.append(cell)
            harness_rates.setdefault(cell.family, []).append(rate)
        rates = {family: _mean(parts) for family, parts in harness_rates.items()}
        shapes = {shape: _mean([rates.get(family) for family in SHAPE_FAMILIES[shape]]) for shape in Shape}
        readings[load] = LoadReading(
            load=load,
            cells=tuple(cells),
            families=tuple(
                FamilyReading(
                    family=family,
                    load=load,
                    rate=_number(rate),
                    throughput=_throughput(
                        records, counted, [trial for trial in planned if _family(trial) is family], usd_per_host_hour
                    ),
                )
                for family, rate in rates.items()
            ),
            shapes={shape: _number(rate) for shape, rate in shapes.items()},
            combined=_number(_mean(list(shapes.values()))),
            throughput=_throughput(records, counted, planned, usd_per_host_hour),
        )
    return readings


# --- Deciding a candidate against a reference -------------------------------------------------

# The paired observations of one sampled instance: its harnesses, as columns of the family's, and
# for each repeat index the candidate's success minus the reference's on each of them.
type _Rows = Sequence[tuple[int, ...]]
type _Tally = tuple[tuple[int, ...], tuple[int, ...]]


@dataclass(frozen=True)
class _Instance:
    columns: tuple[int, ...]
    rows: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class _Series:
    """Paired differences of a weighted rate, as whole numbers over one denominator: the observed
    one, and one for each bootstrap draw."""

    denominator: int
    observed: int
    draws: tuple[int, ...]


def _tally(sample: Iterable[tuple[_Instance, _Rows]], width: int) -> _Tally:
    """For each harness of a family, the sum of the differences sampled on it, and their count."""
    sums, counts = [0] * width, [0] * width
    for instance, rows in sample:
        for row in rows:
            for column, change in zip(instance.columns, row, strict=True):
                sums[column] += change
        for column in instance.columns:
            counts[column] += len(rows)
    return tuple(sums), tuple(counts)


def _difference(tally: _Tally) -> Fraction:
    """The family's difference: each harness's mean, then equally over the harnesses sampled."""
    rates = [Fraction(total, count) for total, count in zip(*tally, strict=True) if count]
    return sum(rates, Fraction(0)) / len(rates)


def _paired(
    planned: list[TrialIdentity], candidate: dict[int, TrialRecord], reference: dict[int, TrialRecord]
) -> list[_Instance] | None:
    """The matched observations of a family at a load, by instance, or nothing when a planned
    trial does not count in both runs or the family has none."""
    harnesses = sorted({trial.harness for trial in planned})
    changes: dict[str, dict[tuple[Harness, int], int]] = {}
    for trial in planned:
        ours, theirs = candidate.get(trial.trial), reference.get(trial.trial)
        if ours is None or theirs is None:
            return None
        change = (ours.outcome is TrialOutcome.PASSED) - (theirs.outcome is TrialOutcome.PASSED)
        changes.setdefault(trial.instance_id, {})[trial.harness, trial.attempt] = change
    instances = []
    for instance in sorted(changes):
        observed = changes[instance]
        on = sorted({harness for harness, _repeat in observed})
        repeats = sorted({repeat for _harness, repeat in observed})
        if len(observed) != len(on) * len(repeats):
            # A repeat index that is not planned on every harness of the instance pairs nothing.
            return None
        rows = tuple(tuple(observed[harness, repeat] for harness in on) for repeat in repeats)
        instances.append(_Instance(columns=tuple(harnesses.index(harness) for harness in on), rows=rows))
    return instances or None


def _bootstrap(instances: list[_Instance], draws: int, generator: Random) -> _Series:
    """The observed difference of a family and its bootstrap draws. Only `random()` is used: its
    sequence is the one Python keeps from a release to the next."""
    width = 1 + max(column for instance in instances for column in instance.columns)
    size = len(instances)
    observed = _tally(((instance, instance.rows) for instance in instances), width)
    tallies = []
    for _draw in range(draws):
        sample = []
        for _pick in range(size):
            instance = instances[int(generator.random() * size)]
            rows = instance.rows
            sample.append((instance, [rows[int(generator.random() * len(rows))] for _repeat in rows]))
        tallies.append(_tally(sample, width))
    exact = {tally: _difference(tally) for tally in {observed, *tallies}}
    denominator = math.lcm(*(value.denominator for value in exact.values()))
    whole = {tally: value.numerator * (denominator // value.denominator) for tally, value in exact.items()}
    return _Series(denominator=denominator, observed=whole[observed], draws=tuple(whole[tally] for tally in tallies))


def _average(parts: Sequence[_Series | None]) -> _Series | None:
    """The equal-weight mean of the parts, draw by draw, or nothing when a part is missing."""
    series = [part for part in parts if part is not None]
    if not series or len(series) != len(parts):
        return None
    common = math.lcm(*(part.denominator for part in series))
    scales = [common // part.denominator for part in series]
    return _Series(
        denominator=common * len(series),
        observed=sum(part.observed * scale for part, scale in zip(series, scales, strict=True)),
        draws=tuple(
            sum(draw * scale for draw, scale in zip(together, scales, strict=True))
            for together in zip(*(part.draws for part in series), strict=True)
        ),
    )


def _undecided(load: Load, shape: Shape | None, reason: Reason, workload: Workload) -> Decision:
    """A decision with nothing compared, under the margin and the draws the workload declares."""
    return Decision(
        load=load,
        shape=shape,
        outcome=Outcome.INCONCLUSIVE,
        reason=reason,
        difference_points=None,
        lower_points=None,
        upper_points=None,
        margin_points=workload.objectives.paired_margin_points,
        draws=workload.sampling.bootstrap_draws,
    )


def _decide(undecided: Decision, series: _Series | None) -> Decision:
    """The decision the series gives, or the undecided one when a pair is missing."""
    if series is None:
        return undecided
    # A whole number divided by a whole number is rounded once: -5 points is exactly -5.0.
    observed = 100 * series.observed / series.denominator
    if len(set(series.draws)) <= 1:
        return replace(undecided, reason=Reason.DEGENERATE_PRECISION, difference_points=observed)
    points = [100 * draw / series.denominator for draw in series.draws]
    lower, upper = percentile(points, LOWER_PERCENTILE), percentile(points, UPPER_PERCENTILE)
    outcome, reason = Outcome.INCONCLUSIVE, Reason.SPANS_MARGIN
    if lower >= undecided.margin_points:
        outcome, reason = Outcome.PASS, None
    elif upper < undecided.margin_points:
        outcome, reason = Outcome.FAIL, None
    return replace(
        undecided, outcome=outcome, reason=reason, difference_points=observed, lower_points=lower, upper_points=upper
    )


def compare(candidate: Records, reference: Records) -> Comparison:
    """The paired decisions of a candidate against a reference: for each load, each shape and then
    the combined result. The margin and the draws are the reference workload's, which are the
    candidate's too whenever anything is compared."""
    workload = reference.workload
    identity = workload_identity(workload)
    matched = workload_identity(candidate.workload) == identity
    missing = Reason.MISSING_PAIRS if matched else Reason.UNMATCHED_WORKLOAD
    undecided = {(load, shape): _undecided(load, shape, missing, workload) for load in Load for shape in (*Shape, None)}
    if not matched:
        return Comparison(matched=False, decisions=tuple(undecided.values()))
    ours, theirs = _counted(candidate), _counted(reference)
    decisions = []
    for load in Load:
        planned = _planned(workload, load)
        families: dict[Family, _Series | None] = {}
        for family in Family:
            paired = _paired([trial for trial in planned if _family(trial) is family], ours, theirs)
            generator = Random(f"{identity}/{family}")
            families[family] = (
                None if paired is None else _bootstrap(paired, workload.sampling.bootstrap_draws, generator)
            )
        shapes: dict[Shape | None, _Series | None] = {
            shape: _average([families[family] for family in SHAPE_FAMILIES[shape]]) for shape in Shape
        }
        shapes[None] = _average(list(shapes.values()))
        decisions += [_decide(undecided[load, shape], series) for shape, series in shapes.items()]
    return Comparison(matched=True, decisions=tuple(decisions))
