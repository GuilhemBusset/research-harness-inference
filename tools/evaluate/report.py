"""Regenerates the scorecard of a run from its saved evidence, compares two runs offline, and
exports a reviewed public bundle (docs/design/agent-session-evaluation.md, "Reading a result" and
"Reproducing and comparing runs").

**The bundle** is a directory, the numeric evidence of one run:

- `manifest.json`: the run's `contracts.Manifest`, schema-versioned;
- `checkpoints.jsonl`: one `contracts.Checkpoint` for each attempt of a cell, in the order the
  attempts started: its `CellRecord`, the count and digest of its observations, the scheduler's
  tally of its sends, each serving host with its seconds in service and its launch fingerprint, and
  the fingerprint the requested release launches an engine with;
- `observations/attempt-<n>.jsonl`: the observations of attempt `n`, one canonical line each, only
  ever added to: its `RequestRecord`s, `TrialRecord`s and `HostSample`s;
- the derived files, which `regenerate` writes from the evidence alone: `summary.json`,
  `report.md` and `summary.sha256`, the digest of `summary.json`.

An attempt is kept as the driver's journal (`evaluate.journal`) keeps it, a checkpoint and a file
of observations sealed by their count and digest, so a line that goes missing from the file is
refused, not read. Its checkpoint also holds the numbers the capacity reading takes from outside
the request rows: the scheduler's own tally of its sends (`replay.Summary.tally`), so a request row
that was never written cannot leave both sides of the reconciliation, and the seconds each host
was in service, with the launch fingerprint the telemetry read of it. `write_evidence` writes a bundle; `load` reads
one and refuses anything else in the directory.

**Reading it.** The plan is found again from the manifest: the suite of `evaluate/suites/` whose
expansion, under the manifest's profile, class size and seeds, is the manifest's workload, by its
identity, but for the floors and the sample budgets. Those are read as the manifest stored them:
the run read the deployed values once, and they need not be the suite's examples. The capacity is `capacity.read` over the measured attempts, the quality is `quality.read`
over every trial and cell record. Nothing reads a clock, the network or an account: the same bundle
and the same host price give the same bytes of `summary.json`, so the same digest, on any machine.
The host price is a bounded number with a source of a fixed vocabulary (`PRICE_SOURCES`), and the
summary keeps it as given: `saved_price` reads it back, so a regenerated bundle keeps its digest.

**What the summary shows.** The requested and the observed settings side by side, neither filled
from the other. Capacity at the class size for each shape, each repetition with its rows, its
counts, its engine throughput by host and summed, and its verdict, and the shape's limiting row:
the floor with the least headroom over its objective in any repetition, a missed floor before any
met one. Every measured cell of the capacity sweep, by shape, load and repetition. Whether each
live cell read at class load ran under that load. The live requests of each harness in each shape
at each load. Quality by family, harness and load, the weighted rates of each shape and the
combined one. The replay against the live tasks of each shape: p50 and p90 of input and output
tokens, cached share, tool delay and turns per task, the route mix and the fan-out (a measure the
evidence lacks is `null`, never zero). Task time, completions per hour and emitted tokens per turn
under the run's tokenizer. And the recommendation.

**The recommendation** is made for each shape at the class size, from runs that were measured:
the run's observed host count is a candidate only when every scheduled repetition passed capacity
(`ShapeVerdict.PASS`) sent from the class's seats (each student of the class, each with the
shape's conversations side by side, all the seats at once: `_seats_shown`), every required family/harness cell of the
shape meets the absolute floor at class load, the paired relative check at class load passes, the
class load was there (each live cell read at class load has every counted send as a row, a driver
within its lag tolerance, a request of each trial, one trial at a time, the live task alone on the first seat, and on
every other seat its shape's conversations side by side through each trial, never idle longer
than its schedule pauses, a trial that ends during such a pause included: `load_evidence`), and
that host count served (requested and observed in the manifest, and the hosts each attempt the
shape is read on observed and accounted for). A low-load miss is shown in its own rows and decides
nothing at class size. A run with no paired decision of its own is never recommended: one read
alone, and the reference of a comparison, the baseline the change is measured from. The smallest
qualifying host count is recommended; a class size or a host count that was not measured is never
projected, it stays unknown. A common host count is given only when one run qualifies for both
shapes.

**Comparing** two bundles (`compare`) prints the treatment diff first, from the manifests alone,
before the rest of either bundle is read: the requested settings that differ, the observed ones
that differ, any setting a run observed other than it requested, the run's settings that differ,
and the lever each changed setting belongs to (`Lever`). A run whose observed settings contradict
its requested ones is refused (`CONTRADICTED_SETTING`): no change can be attributed to what it
requested. Observed settings that differ with their lever not declared changed are refused as
undeclared, setting by setting: a declared host count does not cover another instance type
observed. A paired decision is withheld (inconclusive, `LOAD_UNSHOWN`) unless both runs show the
load it is read at, the reference as much as the candidate: at low load, each live task alone,
one trial after another, with nothing sent beside it.
Two runs compare only when they are one planned workload: a changed floor (`objectives`) or sample
budget (`sampling`) is refused by name, any other change of the workload as incompatible. Each
lever that changed must be shown to have served, in both runs: the hosts and the release by the
observed settings, the launch settings by every serving host's launch fingerprint, through each
attempt and equal to the one its run's release requests (and the two runs' requests differ), with
every host the attempt observed accounted for, the gateway profile by the observed one. A
flags-only change needs both provenances, the engines' and the gateway's. A change that lacks
that evidence, a flags-only one in particular, is refused as unverified, and so are launch
fingerprints that differ when no launch setting was declared changed. A change of several levers is labelled `combined`.

**Exporting** (`export`) is explicit and writes a new directory under `docs/evaluations/` and
nowhere else. It reads the bundle as `load` does, which holds every value to the contracts (an
enumerated label, a bounded number, a digest, a run-relative time), refuses any file or field the
format does not have, and requires each attempt to start once the one before it ended. It regenerates
the derived files, refuses a bundle whose own derived files are stale, and runs the public hygiene
scanner (`tools/hygiene/scan.py`) over the text of every file. Only then does it write, into a
temporary directory renamed into place: a refused export writes nothing.

A refusal is one member of `Refusal`, a fixed vocabulary, and never repeats a value it read. It
needs `spikes` on the path, for `common.percentile`, as `capacity` and `quality` do, and what
`spikes/common.py` imports and `evaluate.streams` needs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Any

from common import percentile
from evaluate import capacity, contracts, quality
from evaluate import plan as planning
from evaluate.capacity import Floor, ShapeVerdict
from evaluate.contracts import (
    Checkpoint,
    ContractError,
    FinishReason,
    HostSample,
    Manifest,
    RequestOutcome,
    RequestRecord,
    Route,
    SendTally,
    Settings,
    Shape,
    Traffic,
    TrialRecord,
)
from evaluate.plan import PlanError, Role
from evaluate.quality import SHAPE_FAMILIES, Load, Outcome
from evaluate.streams import Tally

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from evaluate.contracts import CellRecord, Document, HostLaunch, Objectives

REPO = Path(__file__).resolve().parents[2]
HYGIENE_SCANNER = REPO / "tools" / "hygiene" / "scan.py"
SUMMARY_VERSION = 1
MANIFEST, CHECKPOINTS, OBSERVATIONS = "manifest.json", "checkpoints.jsonl", "observations"
SUMMARY, REPORT, DIGEST = "summary.json", "report.md", "summary.sha256"
DERIVED = (SUMMARY, REPORT, DIGEST)
EVALUATIONS = Path("docs") / "evaluations"
_EXPORT_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_PLACES = 6
# Where the host price of the cost estimates comes from, as `cli.Source` labels it: the only
# labels a summary carries for it. The price itself is a bounded number. `deployed` is the price
# the selected pool's deployed settings give its instance type, which a live run reads.
PRICE_SOURCES = ("example", "supplied", "deployed")
PRICE_MAX = 10**6


class Refusal(StrEnum):
    """Why a bundle was not read, two runs not compared, or an export not written."""

    UNREADABLE_BUNDLE = "unreadable-bundle"
    UNEXPECTED_FILE = "unexpected-file"
    OBSERVATIONS_DAMAGED = "observations-damaged"
    TIMES_GO_BACK = "times-go-back"
    UNKNOWN_PLAN = "unknown-plan"
    INCONSISTENT_EVIDENCE = "inconsistent-evidence"
    CHANGED_FLOOR = "changed-floor"
    CHANGED_SAMPLE_BUDGET = "changed-sample-budget"
    INCOMPATIBLE_WORKLOAD = "incompatible-workload"
    UNVERIFIED_TREATMENT = "unverified-treatment"
    UNDECLARED_TREATMENT = "undeclared-treatment"
    CONTRADICTED_SETTING = "contradicted-setting"
    UNKNOWN_PRICE = "unknown-price"
    EXPORT_OUTSIDE_EVALUATIONS = "export-outside-evaluations"
    EXPORT_EXISTS = "export-exists"
    STALE_DERIVED = "stale-derived"
    HYGIENE_FINDING = "hygiene-finding"


class ReportError(Exception):
    """A refusal, with its reason and, for a treatment, the lever it is about."""

    def __init__(self, reason: Refusal, lever: Lever | None = None) -> None:
        super().__init__(reason.value if lever is None else f"{reason.value} ({lever.value})")
        self.reason = reason
        self.lever = lever


class Lever(StrEnum):
    """What a changed requested setting changes, and what evidence shows it served."""

    HOSTS = "hosts"
    RELEASE = "release"
    TOKENIZER = "tokenizer"
    LAUNCH_SETTINGS = "launch_settings"
    GATEWAY_PROFILE = "gateway_profile"


LEVERS: dict[str, Lever] = {
    "host_count": Lever.HOSTS,
    "instance_type": Lever.HOSTS,
    "gpu_type": Lever.HOSTS,
    "gpus_per_host": Lever.HOSTS,
    "model_revision": Lever.RELEASE,
    "engine_image_digest": Lever.RELEASE,
    "vllm_version": Lever.RELEASE,
    "tokenizer_revision": Lever.TOKENIZER,
    "quantization": Lever.LAUNCH_SETTINGS,
    "context_limit_tokens": Lever.LAUNCH_SETTINGS,
    "kv_dtype": Lever.LAUNCH_SETTINGS,
    "prefix_caching": Lever.LAUNCH_SETTINGS,
    "tensor_parallelism": Lever.LAUNCH_SETTINGS,
    "step_budget_tokens": Lever.LAUNCH_SETTINGS,
    "speculative_decoding": Lever.LAUNCH_SETTINGS,
    "renderer_digest": Lever.LAUNCH_SETTINGS,
    "gateway_profile_digest": Lever.GATEWAY_PROFILE,
}
# The levers a release does not show by itself: the same image and model can run other flags.
FLAGS = frozenset({Lever.LAUNCH_SETTINGS, Lever.GATEWAY_PROFILE})


# --- The bundle ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One attempt of a cell, as `write_evidence` takes it."""

    record: CellRecord
    observations: tuple[Document, ...]
    tally: SendTally | None
    hosts: tuple[HostLaunch, ...]
    requested_launch_fingerprint: str | None


@dataclass(frozen=True)
class Attempt:
    """One attempt of a cell, as `load` reads it back."""

    checkpoint: Checkpoint
    requests: tuple[RequestRecord, ...]
    trials: tuple[TrialRecord, ...]
    samples: tuple[HostSample, ...]

    @property
    def record(self) -> CellRecord:
        return self.checkpoint.record


@dataclass(frozen=True)
class Bundle:
    """A bundle as it was read: its manifest, the plan it was found to be of, its attempts, and
    the text of each of its evidence files by its name in the bundle."""

    suite: str
    manifest: Manifest
    plan: planning.Plan
    attempts: tuple[Attempt, ...]
    files: Mapping[str, str]
    derived: Mapping[str, str]


def _seal(data: bytes) -> tuple[int, str]:
    """The count and the digest of the whole lines of a file, as the journal seals them."""
    return data.count(b"\n"), "sha256:" + hashlib.sha256(data).hexdigest()


def _observation_file(attempt: int) -> str:
    return f"{OBSERVATIONS}/attempt-{attempt}.jsonl"


def write_evidence(directory: Path, manifest: Manifest, attempts: Sequence[Evidence]) -> None:
    """Writes a bundle's evidence into `directory`, which must not exist: the manifest, a
    checkpoint for each attempt in order, and each attempt's observations, sealed."""
    directory.mkdir(parents=True)
    (directory / OBSERVATIONS).mkdir()
    (directory / MANIFEST).write_text(contracts.dumps(manifest) + "\n", encoding="utf-8")
    checkpoints = []
    for at, one in enumerate(attempts):
        data = "".join(contracts.dumps(observation) + "\n" for observation in one.observations).encode("utf-8")
        (directory / _observation_file(at)).write_bytes(data)
        count, digest = _seal(data)
        checkpoint = Checkpoint(
            attempt=at,
            record=one.record,
            observations=count,
            observations_digest=digest,
            tally=one.tally,
            hosts=one.hosts,
            requested_launch_fingerprint=one.requested_launch_fingerprint,
        )
        checkpoints.append(contracts.dumps(checkpoint) + "\n")
    (directory / CHECKPOINTS).write_text("".join(checkpoints), encoding="utf-8")


def _files(directory: Path) -> dict[str, bytes]:
    """Every file of the bundle by its name in it. A link or anything else that is no file is
    refused: nothing outside the directory is read."""
    found: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ReportError(Refusal.UNEXPECTED_FILE)
        if path.is_file():
            found[name] = path.read_bytes()
        elif name != OBSERVATIONS:
            raise ReportError(Refusal.UNEXPECTED_FILE)
    return found


def _plan_of(manifest: Manifest) -> tuple[str, planning.Plan]:
    """The suite and the plan whose workload is the manifest's. The floors and the sample budgets
    are the ones the manifest stored, which the run read from the deployment and may differ from
    the suite's example values: the plan is found under the suite's and read under the run's."""
    workload = manifest.workload
    for path in sorted(planning.SUITES.glob("*.toml")):
        try:
            plan = planning.expand(
                planning.load_suite(path.stem),
                workload.profile,
                workload.students,
                input_seed=workload.input_seed,
                schedule_seed=workload.schedule_seed,
            )
        except PlanError:
            continue
        stored = dataclasses.replace(workload, objectives=plan.workload.objectives, sampling=plan.workload.sampling)
        if contracts.workload_identity(stored) == plan.workload_hash:
            return path.stem, dataclasses.replace(plan, workload=workload)
    raise ReportError(Refusal.UNKNOWN_PLAN)


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReportError(Refusal.UNREADABLE_BUNDLE) from None


def load(directory: Path) -> Bundle:
    """The bundle in `directory`, every value held to the contracts, or a refusal."""
    if not directory.is_dir():
        raise ReportError(Refusal.UNREADABLE_BUNDLE)
    files = _files(directory)
    if MANIFEST not in files or CHECKPOINTS not in files:
        raise ReportError(Refusal.UNREADABLE_BUNDLE)
    try:
        manifest = contracts.loads(files[MANIFEST], Manifest)
        lines = _text(files[CHECKPOINTS]).splitlines()
        checkpoints = [contracts.loads(line, Checkpoint) for line in lines]
    except ContractError:
        raise ReportError(Refusal.UNREADABLE_BUNDLE) from None
    if any(checkpoint.attempt != at for at, checkpoint in enumerate(checkpoints)):
        raise ReportError(Refusal.UNREADABLE_BUNDLE)
    expected = {MANIFEST, CHECKPOINTS, *DERIVED, *(_observation_file(one.attempt) for one in checkpoints)}
    if not files.keys() <= expected:
        raise ReportError(Refusal.UNEXPECTED_FILE)
    attempts = []
    ended = 0
    for checkpoint in checkpoints:
        record = checkpoint.record
        if record.started_ms < ended:
            raise ReportError(Refusal.TIMES_GO_BACK)
        ended = record.ended_ms
        data = files.get(_observation_file(checkpoint.attempt), b"")
        if _seal(data) != (checkpoint.observations, checkpoint.observations_digest) or data[-1:] not in (b"", b"\n"):
            raise ReportError(Refusal.OBSERVATIONS_DAMAGED)
        try:
            documents = [contracts.parse(line) for line in _text(data).splitlines()]
        except ContractError:
            raise ReportError(Refusal.UNREADABLE_BUNDLE) from None
        requests = tuple(one for one in documents if isinstance(one, RequestRecord))
        trials = tuple(one for one in documents if isinstance(one, TrialRecord))
        samples = tuple(one for one in documents if isinstance(one, HostSample))
        if len(requests) + len(trials) + len(samples) != len(documents):
            raise ReportError(Refusal.UNREADABLE_BUNDLE)
        if any(one.cell != record.cell for one in (*requests, *samples)):
            raise ReportError(Refusal.INCONSISTENT_EVIDENCE)
        attempts.append(Attempt(checkpoint, requests, trials, samples))
    suite, plan = _plan_of(manifest)
    evidence = {name: _text(data) for name, data in files.items() if name not in DERIVED}
    derived = {name: _text(data) for name, data in files.items() if name in DERIVED}
    return Bundle(suite, manifest, plan, tuple(attempts), evidence, derived)


# --- Reading a run ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    bundle: Bundle
    capacity: capacity.Capacity
    quality: dict[Load, quality.LoadReading]
    records: quality.Records

    @property
    def manifest(self) -> Manifest:
        return self.bundle.manifest


def tally_of(sent: SendTally) -> Tally:
    """The scheduler's tally of a checkpoint, as `capacity` reads it."""
    return Tally(
        attempts=sent.attempts,
        unfinished=sent.unfinished,
        completed=sent.completed,
        failures={
            RequestOutcome.TRUNCATED_STREAM: sent.truncated_stream,
            RequestOutcome.TRANSPORT_ERROR: sent.transport_error,
            RequestOutcome.HTTP_REFUSAL: sent.http_refusal,
            RequestOutcome.ERROR_EVENT: sent.error_event,
        },
        retries=sent.retries,
        usage_missing=sent.usage_missing,
        eligible=sent.eligible,
    )


def read(bundle: Bundle, *, usd_per_host_hour: float) -> Reading:
    """The capacity and the quality of a bundle, at a host price for the cost estimates."""
    evidence = [
        capacity.CellEvidence(
            record=attempt.record,
            requests=attempt.requests,
            samples=attempt.samples,
            tally=None if attempt.checkpoint.tally is None else tally_of(attempt.checkpoint.tally),
            in_service_seconds={host.host: host.in_service_ms / 1000 for host in attempt.checkpoint.hosts},
        )
        for attempt in bundle.attempts
    ]
    records = quality.Records(
        workload=bundle.manifest.workload,
        trials=tuple(trial for attempt in bundle.attempts for trial in attempt.trials),
        cells=tuple(attempt.record for attempt in bundle.attempts),
    )
    try:
        return Reading(
            bundle=bundle,
            capacity=capacity.read(bundle.plan, evidence, usd_per_host_hour=usd_per_host_hour),
            quality=quality.read(records, usd_per_host_hour=usd_per_host_hour),
            records=records,
        )
    except (capacity.CapacityError, quality.QualityError):
        raise ReportError(Refusal.INCONSISTENT_EVIDENCE) from None


def check_price(usd_per_host_hour: float, rate_source: str) -> None:
    """A host price a summary can carry: a bounded number above zero, from a source of the fixed
    vocabulary. Anything else is refused before a summary is made of it."""
    number = isinstance(usd_per_host_hour, int | float) and not isinstance(usd_per_host_hour, bool)
    if not number or not math.isfinite(usd_per_host_hour) or not 0 < usd_per_host_hour < PRICE_MAX:
        raise ReportError(Refusal.UNKNOWN_PRICE)
    if rate_source not in PRICE_SOURCES:
        raise ReportError(Refusal.UNKNOWN_PRICE)


def saved_price(bundle: Bundle) -> tuple[float, str] | None:
    """The host price the bundle's saved summary was made at, and its source, so that regenerating
    it reproduces it; `None` when the bundle has no summary yet."""
    text = bundle.derived.get(SUMMARY)
    if text is None:
        return None
    try:
        price = json.loads(text)["host_price"]
        rate, source = price["usd_per_host_hour"], price["source"]
    except (ValueError, KeyError, TypeError):
        raise ReportError(Refusal.UNREADABLE_BUNDLE) from None
    check_price(rate, source)
    return float(rate), source


def _measured(bundle: Bundle) -> list[Attempt]:
    """The attempt each cell is read on, as `capacity` chooses it: its valid one, or its last."""
    by_cell: dict[int, list[Attempt]] = {}
    for attempt in bundle.attempts:
        by_cell.setdefault(attempt.record.cell, []).append(attempt)
    chosen = []
    for cell in sorted(by_cell):
        kept = by_cell[cell]
        valid = [one for one in kept if one.record.valid_for_qualification]
        chosen.append(valid[0] if valid else kept[-1])
    return chosen


# --- Numbers ------------------------------------------------------------------------------------


def _n(value: float | None) -> float | None:
    return None if value is None else round(float(value), _PLACES) + 0.0


def _at(values: Sequence[float], percent: float) -> float | None:
    return _n(percentile(values, percent)) if values else None


def _spread(values: Sequence[float]) -> dict[str, Any]:
    return {"count": len(values), "p50": _at(values, 50), "p90": _at(values, 90)}


def _value(value: object) -> object:
    """A setting as the summary writes it: a label as its text, a number or a digest as it is."""
    return value.value if isinstance(value, StrEnum) else value


# --- The parts of the summary -------------------------------------------------------------------


def _conditions(manifest: Manifest) -> list[dict[str, Any]]:
    """Each setting as requested and as observed, neither filled from the other."""
    rows = []
    for field in dataclasses.fields(Settings):
        asked, seen = getattr(manifest.requested, field.name), getattr(manifest.observed, field.name)
        if asked is None:
            status = "not_declared" if seen is None else "observed_only"
        elif seen is None:
            status = "not_observed"
        else:
            status = "match" if asked == seen else "differs"
        rows.append(
            {
                "setting": field.name,
                "lever": LEVERS[field.name].value,
                "requested": _value(asked),
                "observed": _value(seen),
                "status": status,
            }
        )
    return rows


def _headroom(floor: Floor, cell: capacity.CellReading, objectives: Objectives) -> float | None:
    """How far a repetition's row is inside its objective, as a share of the objective: below zero
    is past it. An objective of zero has no ratio: the headroom is the difference itself. A
    preemption has no ratio either: one or more is a headroom of -1."""
    if floor is Floor.TTFT_P95:
        p95, ceiling = cell.later_turns.p95_ms, objectives.ttft_p95_ms_max
        if p95 is None:
            return None
        return (ceiling - p95) / ceiling if ceiling > 0 else ceiling - p95
    if floor in (Floor.SPEED_P50, Floor.SPEED_P10):
        speed = cell.speeds.p50_tokens_per_s if floor is Floor.SPEED_P50 else cell.speeds.p10_tokens_per_s
        minimum = (
            objectives.stream_speed_p50_tokens_per_s_min
            if floor is Floor.SPEED_P50
            else objectives.stream_speed_p10_tokens_per_s_min
        )
        return None if speed is None or minimum <= 0 else (speed - minimum) / minimum
    if floor is Floor.FAILED_SHARE:
        share, cap = cell.failures.share, objectives.failed_share_max
        if share is None:
            return None
        return (cap - share) / cap if cap > 0 else -share
    preemptions = None if cell.engine is None else cell.engine.preemptions
    return None if not preemptions else -1.0


def limiting_row(shape: capacity.ShapeReading, objectives: Objectives) -> dict[str, Any] | None:
    """The row that limits a shape: the floor with the least headroom in any repetition. A floor
    that was observed missed comes before every met one."""
    found = []
    for cell in shape.repetitions:
        for floor in Floor:
            headroom = _headroom(floor, cell, objectives)
            if headroom is not None:
                missed = floor in cell.observed_misses
                found.append((not missed, headroom, list(Floor).index(floor), floor, cell.identity.repetition))
    if not found:
        return None
    met, headroom, _order, floor, repetition = min(found)
    return {"floor": floor.value, "headroom": _n(headroom), "missed": not met, "repetition": repetition}


# Why a repetition that met its floors is no pass: it was not sent from the class's seats
# (`_seats_shown`), so its rows, still shown, are no measure of the class.
SEATS_UNSHOWN = "seats_unshown"


def _repetition(cell: capacity.CellReading, seats_shown: bool = True) -> dict[str, Any]:
    engine = cell.engine
    failures = cell.failures
    verdict, reasons = cell.verdict.value, [reason.value for reason in cell.reasons]
    if cell.verdict is capacity.Verdict.PASS and not seats_shown:
        verdict, reasons = capacity.Verdict.INSUFFICIENT_EVIDENCE.value, [*reasons, SEATS_UNSHOWN]
    return {
        "repetition": cell.identity.repetition,
        "cell": cell.identity.cell,
        "verdict": verdict,
        "reasons": reasons,
        "floors": {floor.value: check.value for floor, check in cell.floors.items()},
        "observed_misses": [floor.value for floor in cell.observed_misses],
        "later_turns": cell.later_turns.count,
        "later_turns_min": cell.ttft_samples_min,
        "ttft_p50_ms": _n(cell.later_turns.p50_ms),
        "ttft_p95_ms": _n(cell.later_turns.p95_ms),
        "answer_p95_ms": _n(cell.later_turns.answer_p95_ms),
        "first_turns": cell.first_turns.count,
        "first_turn_p95_ms": _n(cell.first_turns.p95_ms),
        "streams": cell.speeds.count,
        "streams_min": cell.stream_samples_min,
        "speed_p50_tokens_per_s": _n(cell.speeds.p50_tokens_per_s),
        "speed_p10_tokens_per_s": _n(cell.speeds.p10_tokens_per_s),
        "failed": failures.failed,
        "attempts": failures.attempts,
        "failed_share": _n(failures.share),
        "failed_wilson_low": _n(failures.wilson_low),
        "failed_wilson_high": _n(failures.wilson_high),
        "failed_observed_floor": failures.observed_floor,
        "usage_missing": failures.usage_missing,
        "driver_lag_p95_ms": _n(cell.driver_lag_p95_ms),
        "preemptions": None if engine is None else _n(engine.preemptions),
        "engine_output_tokens_per_s": None if engine is None else _n(engine.output_tokens_per_s),
        "engine_hosts": None if engine is None else len(engine.hosts),
        # Each host's own throughput beside the pool's sum: a pool can hide an idle host.
        "engine_output_tokens_per_s_by_host": (
            None
            if engine is None
            else [{"host": host.host, "output_tokens_per_s": _n(host.output_tokens_per_s)} for host in engine.hosts]
        ),
        "delivered_output_tokens": None if engine is None else engine.delivered_output_tokens,
        "cached_share": None if engine is None else _n(engine.cached_share),
        "waiting_requests_peak": None if engine is None else _n(engine.waiting_requests_peak),
        "gpu_utilization_mean": None if engine is None else _n(engine.gpu_utilization_mean),
        "host_hours": None if engine is None else _n(engine.host_hours),
        "host_cost_estimate_usd": None if engine is None else _n(engine.host_cost_estimate_usd),
        "host_cost_estimate_per_million_output_usd": (
            None if engine is None else _n(engine.host_cost_estimate_per_million_output_usd)
        ),
    }


def _seats_shown(plan: planning.Plan, planned: planning.PlannedCell, attempt: Attempt | None) -> bool:
    """Whether a replayed cell ran as the plan's seats (`replay.seat_of`): its replay from each of
    the cell's students, no conversation shared by two, and each student's conversations side by
    side as many at once as the slots the plan gives that seat, one in hands-on, two in
    several-agents. And the seats ran together (`_seats_together`). A repetition one student sent
    all of, or its students one after another, is no measure of a class."""
    if attempt is None:
        return False
    per = plan.spec.conversations[planned.identity.shape]
    background = [one for one in attempt.requests if one.traffic is Traffic.BACKGROUND]
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    for one in background:
        start, end = spans.get((one.student, one.conversation), (one.eligible_ms, one.ended_ms))
        spans[one.student, one.conversation] = (min(start, one.eligible_ms), max(end, one.ended_ms))
    if len({conversation for _student, conversation in spans}) != len(spans):
        return False
    seats = range(planned.identities)
    if {student for student, _conversation in spans} != set(seats):
        return False
    slots = {seat: sum(slot // per == seat for slot in range(planned.replay_slots)) for seat in seats}
    side_by_side = all(
        _peak(span for (student, _conversation), span in spans.items() if student == seat) == slots[seat]
        for seat in seats
    )
    return side_by_side and _seats_together(plan, attempt, background, slots)


def _seats_together(
    plan: planning.Plan, attempt: Attempt, background: Sequence[RequestRecord], slots: Mapping[int, int]
) -> bool:
    """Whether every seat of a replayed cell held its conversations at once through the whole
    admissions window the cell recorded, which must be as long as the plan's: from the cell's start,
    once the seat's last slot was scheduled to arrive and give or take as long as a schedule may
    pause (`_longest_pause_ms`), until admissions closed, each seat holds its slots side by side
    (`_background_gap`). Seats that each ran in turn never were a class at once, however each ran
    alone, and a replay that began late or stopped early left the class idle while it was read."""
    record = attempt.record
    planned = plan.cells[record.cell]
    if record.admissions_ended_ms - record.started_ms < planned.admissions_ms:
        return False
    pause = _longest_pause_ms(plan)
    per = plan.spec.conversations[planned.identity.shape]
    for seat, held in slots.items():
        arrivals = [planned.arrivals_ms[slot] for slot in range(seat * per, seat * per + held)]
        begin = record.started_ms + max(arrivals, default=0) + pause
        window = [(begin, float(record.admissions_ended_ms))]
        if begin >= record.admissions_ended_ms:
            return False
        if _background_gap([one for one in background if one.student == seat], window, pause, held) is not None:
            return False
    return True


def _capacity(reading: Reading) -> dict[str, Any]:
    objectives = reading.manifest.workload.objectives
    plan = reading.bundle.plan
    measured = {attempt.record.cell: attempt for attempt in _measured(reading.bundle)}
    shapes = {}
    for shape, one in reading.capacity.shapes.items():
        # Whether each repetition was sent from the class's seats, each in the shape.
        shown = [
            _seats_shown(plan, plan.cells[cell.identity.cell], measured.get(cell.identity.cell))
            for cell in one.repetitions
        ]
        # A pass needs every repetition sent from the class's seats; a known miss stands.
        verdict = one.verdict
        if verdict is ShapeVerdict.PASS and not (shown and all(shown)):
            verdict = ShapeVerdict.INCONCLUSIVE
        shapes[shape.value] = {
            "agent_slots": one.agent_slots,
            "verdict": verdict.value,
            "seats_shown": shown,
            "missed_repetitions": list(one.missed_repetitions),
            "limiting_row": limiting_row(one, objectives),
            "repetitions": [_repetition(cell, seats) for cell, seats in zip(one.repetitions, shown, strict=True)],
            "spread": {
                "ttft_p95_ms": [_n(one.ttft_p95_ms.low), _n(one.ttft_p95_ms.high)],
                "speed_p50_tokens_per_s": [_n(one.speed_p50_tokens_per_s.low), _n(one.speed_p50_tokens_per_s.high)],
                "speed_p10_tokens_per_s": [_n(one.speed_p10_tokens_per_s.low), _n(one.speed_p10_tokens_per_s.high)],
                "failed_share": [_n(one.failed_share.low), _n(one.failed_share.high)],
            },
        }
    return shapes


def _sweep(reading: Reading) -> list[dict[str, Any]]:
    """Every measured cell of the capacity sweep, each shape at each load it was driven at, with
    its rows and its verdict: where a shape crosses a floor. Descriptive: the recommendation is read
    at the class size alone. A row that was not sent from its seats (`_seats_shown`) is no pass."""
    plan = reading.bundle.plan
    measured = {attempt.record.cell: attempt for attempt in _measured(reading.bundle)}
    rows = [
        {
            "shape": cell.identity.shape.value,
            "agent_slots": cell.identity.agent_slots,
            "class_floor": cell.class_floor,
            **_repetition(cell, _seats_shown(plan, plan.cells[cell.identity.cell], measured.get(cell.identity.cell))),
        }
        for cell in reading.capacity.cells
        if cell.record is not None and cell.identity.phase is contracts.Phase.CAPACITY_SWEEP
    ]
    return sorted(rows, key=lambda row: (row["shape"], row["agent_slots"], row["repetition"], row["cell"]))


def _live(reading: Reading) -> list[tuple[Load, RequestRecord]]:
    """The live requests of the measured attempts, each at every load its cell is read at."""
    workload = reading.manifest.workload
    return [
        (load, request)
        for attempt in _measured(reading.bundle)
        for request in attempt.requests
        if request.traffic is Traffic.LIVE
        for load in quality.loads_of(workload, request.cell)
    ]


def _by_harness(reading: Reading) -> list[dict[str, Any]]:
    """What the live requests of each harness saw in each shape at each load: descriptive, held to
    no floor. A harness that serves both shapes has a row for each."""
    workload = reading.manifest.workload
    trials = workload.trials
    groups: dict[tuple[str, Load, str], list[RequestRecord]] = {}
    for load, request in _live(reading):
        if request.trial is not None and request.trial < len(trials):
            shape = workload.cells[request.cell].shape.value
            groups.setdefault((shape, load, trials[request.trial].harness.value), []).append(request)
    rows = []
    for (shape, load, harness), requests in sorted(groups.items()):
        later = [float(one.first_output_ms - one.sent_ms) for one in requests if one.turn > 0 and one.first_output_ms]
        speeds = [speed for one in requests if (speed := capacity.speed_of(one)) is not None]
        rows.append(
            {
                "shape": shape,
                "load": load.value,
                "harness": harness,
                "requests": len(requests),
                "failed": sum(one.failed for one in requests),
                "later_turns": len(later),
                "ttft_p95_ms": _at(later, 95),
                "streams": len(speeds),
                "speed_p50_tokens_per_s": _at(speeds, 50),
                "speed_p10_tokens_per_s": _at(speeds, 10),
            }
        )
    return rows


def _throughput(throughput: quality.Throughput) -> dict[str, Any]:
    return {
        "attempts": throughput.attempts,
        "successes": throughput.successes,
        "successful_task_time_ms": _n(throughput.successful_task_time_ms),
        "tasks_per_hour": _n(throughput.tasks_per_hour),
        "elapsed_hours": _n(throughput.elapsed_hours),
        "instance_hours": _n(throughput.instance_hours),
        "host_cost_estimate_usd": _n(throughput.host_cost_estimate_usd),
        "host_cost_estimate_per_success_usd": _n(throughput.host_cost_estimate_per_success_usd),
    }


def _quality(reading: Reading) -> dict[str, Any]:
    loads = {}
    for load, one in reading.quality.items():
        loads[load.value] = {
            "cells": [
                {
                    "family": cell.family.value,
                    "harness": cell.harness.value,
                    "planned": cell.planned,
                    "attempts": cell.attempts,
                    "successes": cell.successes,
                    "executions": cell.executions,
                    "instances": cell.instances,
                    "instances_all_passed": cell.instances_all_passed,
                    "rate": _n(cell.rate),
                    "floor": _n(cell.floor),
                    "floor_outcome": cell.floor_outcome.value,
                }
                for cell in one.cells
            ],
            "families": {
                family.family.value: {"rate": _n(family.rate), **_throughput(family.throughput)}
                for family in one.families
            },
            "shapes": {shape.value: _n(rate) for shape, rate in one.shapes.items()},
            "combined": _n(one.combined),
            "throughput": _throughput(one.throughput),
        }
    return loads


def _peak(intervals: Iterable[tuple[int, int]]) -> int:
    """The most intervals open at once; one that ends when another starts is not open with it."""
    # At one time an end (-1) sorts before a start (+1).
    events = sorted(edge for start, end in intervals for edge in ((start, 1), (end, -1)))
    peak = open_now = 0
    for _at_ms, change in events:
        open_now += change
        peak = max(peak, open_now)
    return peak


def _tool_delays(groups: Iterable[Sequence[RequestRecord]]) -> list[float]:
    """Within each conversation, the wait from the end of a turn to the time the next turn was
    eligible: the tool's own time. Only a turn that asked for a tool is followed by one: a live
    turn whose finish reason is anything else, or unknown, gives no tool wait. The replay driver
    writes no finish reason and pauses for a tool between every two turns of its schedule, so
    for its own rows that pause is read from their times alone."""
    delays = []
    for requests in groups:
        ordered = sorted(requests, key=lambda one: one.turn)
        for before, after in pairwise(ordered):
            scheduled = before.traffic is Traffic.BACKGROUND and before.finish_reason is None
            tool = (before.finish_reason is FinishReason.TOOL_USE or scheduled) and not before.failed
            if tool and after.turn == before.turn + 1:
                delays.append(float(max(0, after.eligible_ms - before.ended_ms)))
    return delays


def _distribution(
    requests: Sequence[RequestRecord], tasks: Sequence[Sequence[RequestRecord]], turns: Sequence[float]
) -> dict[str, Any]:
    """The p50 and p90 of what a side of the comparison sent and how its tasks ran."""
    usage = [one.usage for one in requests if one.usage is not None]
    inputs = [float(one.input_tokens) for one in usage if one.input_tokens is not None]
    outputs = [float(one.output_tokens) for one in usage if one.output_tokens is not None]
    cached = [
        one.cached_input_tokens / one.input_tokens
        for one in usage
        if one.input_tokens and one.cached_input_tokens is not None
    ]
    # A conversation is a seat's in its cell, and a live one is also its trial's: one seat runs
    # every trial of a live cell, each in conversations of its own.
    conversations: dict[tuple[int, int, int | None, int], list[RequestRecord]] = {}
    for one in requests:
        conversations.setdefault((one.cell, one.student, one.trial, one.conversation), []).append(one)
    fan_out = [float(_peak((one.sent_ms, one.ended_ms) for one in task)) for task in tasks if task]
    return {
        "requests": len(requests),
        "tasks": len([task for task in tasks if task]),
        "usage_missing": len(requests) - len(usage),
        "input_tokens": _spread(inputs),
        "output_tokens": _spread(outputs),
        "cached_share": _spread(cached),
        "tool_delay_ms": _spread(_tool_delays(conversations.values())),
        "turns_per_task": _spread(turns),
        "fan_out": {**_spread(fan_out), "max": max(fan_out, default=None)},
        "route_mix": {
            route.value: _n(sum(one.route is route for one in requests) / len(requests)) if requests else None
            for route in Route
        },
    }


def _distributions(reading: Reading) -> dict[str, Any]:
    """For each shape, the steady replay of its class-size cells against the live tasks of its
    families. A replay task is a conversation, its fan-out a seat's; a live task is a trial, all
    its conversations, and its turns are the model turns the trial counted across its agent tree."""
    plan = reading.bundle.plan
    workload = reading.manifest.workload
    measured = _measured(reading.bundle)
    found = {}
    for shape in Shape:
        floor_cells = {
            cell.identity.cell
            for cell in plan.cells
            if Role.CLASS_FLOOR in cell.roles and cell.steady_state and cell.identity.shape is shape
        }
        replay = [
            one
            for attempt in measured
            if attempt.record.cell in floor_cells
            for one in attempt.requests
            # The steady samples, as `capacity` reads them: sent in the window, before admissions closed.
            if one.traffic is Traffic.BACKGROUND
            and attempt.record.started_ms <= one.sent_ms <= attempt.record.admissions_ended_ms
        ]
        conversations: dict[tuple[int, int, int], list[RequestRecord]] = {}
        seats: dict[tuple[int, int], list[RequestRecord]] = {}
        for one in replay:
            conversations.setdefault((one.cell, one.student, one.conversation), []).append(one)
            seats.setdefault((one.cell, one.student), []).append(one)
        families = SHAPE_FAMILIES[shape]
        live_trials = {
            trial.trial for trial in workload.trials if trial.instance_id.split(".")[0] in {f.value for f in families}
        }
        live = [
            one
            for attempt in measured
            for one in attempt.requests
            if one.traffic is Traffic.LIVE and one.trial in live_trials
        ]
        by_trial: dict[int, list[RequestRecord]] = {}
        for one in live:
            if one.trial is not None:
                by_trial.setdefault(one.trial, []).append(one)
        turns = [
            float(trial.model_turns)
            for attempt in measured
            for trial in attempt.trials
            if trial.trial in live_trials and trial.counts_as_attempt and trial.model_turns is not None
        ]
        replay_side = _distribution(replay, list(seats.values()), [float(len(one)) for one in conversations.values()])
        replay_side["tasks"] = len(conversations)
        found[shape.value] = {"replay": replay_side, "live": _distribution(live, list(by_trial.values()), turns)}
    return found


def _tokenizer(reading: Reading) -> dict[str, Any]:
    """Task time, completions per hour and emitted tokens per turn, under the run's tokenizer."""
    manifest = reading.manifest
    loads: dict[str, Any] = {}
    live = _live(reading)
    for load, one in reading.quality.items():
        emitted = [
            float(request.usage.output_tokens)
            for at, request in live
            if at is load and request.usage is not None and request.usage.output_tokens is not None
        ]
        throughput = one.throughput
        loads[load.value] = {
            "successful_task_time_ms": _n(throughput.successful_task_time_ms),
            "tasks_per_hour": _n(throughput.tasks_per_hour),
            "success_share": _n(throughput.successes / throughput.attempts) if throughput.attempts else None,
            "successes": throughput.successes,
            "attempts": throughput.attempts,
            "emitted_tokens_per_turn": _n(fmean(emitted)) if emitted else None,
        }
    return {
        "tokenizer_revision": {
            "requested": manifest.requested.tokenizer_revision,
            "observed": manifest.observed.tokenizer_revision,
        },
        "model_revision": {
            "requested": manifest.requested.model_revision,
            "observed": manifest.observed.model_revision,
        },
        "loads": loads,
    }


# --- The recommendation -------------------------------------------------------------------------

MET, MISSED, UNKNOWN = "met", "missed", "unknown"
# What the relative check of a run is when it is read alone, and when it is the reference: in
# neither has the run a paired decision of its own, so in neither is it recommended.
NO_REFERENCE, BASELINE = "no_reference", "baseline"
LIVE_PHASES = (contracts.Phase.LIVE_LOW_LOAD, contracts.Phase.LIVE_CLASS_LOAD)


class LoadGap(StrEnum):
    """Why a live cell does not show the load it is read at."""

    NOT_MEASURED = "not_measured"
    SENDS_UNACCOUNTED = "sends_unaccounted"
    DRIVER_LAG = "driver_lag"
    TRIAL_UNSENT = "trial_unsent"
    SENT_OUTSIDE_RUN = "sent_outside_run"
    SEATS_UNPLANNED = "seats_unplanned"
    BACKGROUND_MISSING = "background_missing"
    BACKGROUND_SHORT = "background_short"
    BACKGROUND_IDLE = "background_idle"
    BACKGROUND_SERIAL = "background_serial"
    TRIALS_OVERLAP = "trials_overlap"
    NOT_ALONE = "not_alone"


# What `capacity` finds in a live cell that keeps it from showing its load: a send that is no row,
# or a driver that lagged past the protocol's tolerance, so that what was sent is not the schedule.
_LOAD_REASONS = {
    capacity.Reason.ATTEMPTS_UNACCOUNTED: LoadGap.SENDS_UNACCOUNTED,
    capacity.Reason.DRIVER_LAG: LoadGap.DRIVER_LAG,
}


def _longest_pause_ms(plan: planning.Plan) -> float:
    """The longest a replay seat is idle by its schedule: the longest tool pause, which is also the
    longest pause before a replacement conversation, and the driver's tolerated lag on top."""
    protocol = plan.spec.protocol
    return float(protocol["replay"]["tool_delay_ms"][1]) + float(protocol["driver"]["scheduling_lag_p95_ms_max"])


def _merged(spans: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Spans joined where they meet or overlap."""
    joined: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if joined and start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


def _background_gap(
    background: Sequence[RequestRecord], windows: Sequence[tuple[float, float]], pause: float, conversations: int
) -> LoadGap | None:
    """Whether a seat's replay kept up its shape's load through each trial: `conversations` of the
    seat side by side at every instant of every trial's window. A conversation holds its place from
    the time a request of it was eligible until it ended and then for as long as its schedule may
    pause (`pause`), so a trial that ends while its seat waits on a tool, the next send not yet due,
    still ran loaded. The first instant the load is missing names the gap: a seat that never ran
    its conversations side by side is serial, one whose replay had not begun when a trial started
    is short, one that stopped or paused longer than its schedule during a trial is idle."""
    if not background:
        return LoadGap.BACKGROUND_MISSING
    by_conversation: dict[int, list[tuple[float, float]]] = {}
    for one in background:
        by_conversation.setdefault(one.conversation, []).append((one.eligible_ms, one.ended_ms + pause))
    # One place for each conversation, however its turns overlap their pauses.
    held = [span for spans in by_conversation.values() for span in _merged(spans)]

    def depth(at: float) -> int:
        return sum(start <= at < end for start, end in held)

    peak, first_gap = 0, None
    for begin, finish in windows:
        cuts = sorted({begin, finish, *(edge for span in held for edge in span if begin < edge < finish)})
        for at in cuts[:-1] if len(cuts) > 1 else cuts:
            now = depth(at)
            peak = max(peak, now)
            if now < conversations and first_gap is None:
                first_gap = LoadGap.BACKGROUND_SHORT if at == begin else LoadGap.BACKGROUND_IDLE
    if 0 < peak < conversations:
        return LoadGap.BACKGROUND_SERIAL
    return first_gap


def _runs_by_trial(trials: Sequence[TrialRecord]) -> dict[int, list[TrialRecord]]:
    """Each trial's runs, in the order they started."""
    runs: dict[int, list[TrialRecord]] = {}
    for trial in sorted(trials, key=lambda one: (one.started_ms, one.execution)):
        runs.setdefault(trial.trial, []).append(trial)
    return runs


def _run_of(runs: Mapping[int, Sequence[TrialRecord]], request: RequestRecord) -> TrialRecord | None:
    """The run a live request belongs to: the last run of its trial that was running when it was
    sent. `None` for a request of no recorded run, or one sent while no run of its trial was
    running: before the first started, or between one run's end and the next one's start."""
    if request.trial is None or request.trial not in runs:
        return None
    running = [run for run in runs[request.trial] if run.started_ms <= request.sent_ms <= run.ended_ms]
    return running[-1] if running else None


def _trials_overlap(trials: Sequence[TrialRecord], live: Sequence[RequestRecord]) -> bool:
    """Whether two trials of a live cell ran at once: the seat runs one task at a time, so a trial
    begins once the one before it, each run of it and every request of it, has ended. The requests
    of one run may run side by side. Each run of a trial is its own span: a request belongs to its
    run (`_run_of`), so that a retry after another trial is no overlap with it."""
    runs = _runs_by_trial(trials)
    # A run by its execution; the requests of a trial with no run recorded, by their trial.
    spans: dict[tuple[str, int], tuple[int, int]] = {
        ("run", trial.execution): (trial.started_ms, trial.ended_ms) for trial in trials
    }
    for one in live:
        if one.trial is None:
            continue
        run = _run_of(runs, one)
        key = ("trial", one.trial) if run is None else ("run", run.execution)
        before = spans.get(key, (one.sent_ms, one.ended_ms))
        spans[key] = (min(before[0], one.sent_ms), max(before[1], one.ended_ms))
    return any(after[0] < before[1] for before, after in pairwise(sorted(spans.values())))


def _load_gap(reading: Reading, planned: planning.PlannedCell, attempt: Attempt | None) -> LoadGap | None:
    """What a live cell lacks to show the load it is read at, or `None`. Every send the scheduler
    counted must be a row (`capacity`'s reconciliation), the driver must have kept to its schedule
    within the protocol's tolerance, and every trial that counts must have sent its own requests.
    At low load the live task runs alone: nothing but its own requests, all from the first seat.
    At class load the live task runs alone on the first seat, each other seat the plan gives the
    background sends replay and nothing else, and each kept its shape's conversations side by side
    through every trial, never idle longer than its schedule pauses (`_background_gap`)."""
    if attempt is None:
        return LoadGap.NOT_MEASURED
    cell = next(one for one in reading.capacity.cells if one.identity.cell == planned.identity.cell)
    for reason, gap in _LOAD_REASONS.items():
        if reason in cell.reasons:
            return gap
    trials = [trial for trial in attempt.trials if trial.counts_as_attempt]
    live = [request for request in attempt.requests if request.traffic is Traffic.LIVE]
    # The run whose grade counts must have sent requests of its own: an interrupted run's
    # requests do not show the load its replacement was graded under.
    runs = _runs_by_trial(attempt.trials)
    sent = {run.execution for one in live if (run := _run_of(runs, one)) is not None}
    if any(trial.execution not in sent for trial in trials):
        return LoadGap.TRIAL_UNSENT
    # A request of a recorded trial sent while none of its runs was running is no evidence of the
    # load any run was graded under: the records disagree, and the cell shows no load.
    if any(one.trial in runs and _run_of(runs, one) is None for one in live):
        return LoadGap.SENT_OUTSIDE_RUN
    if _trials_overlap(attempt.trials, live):
        return LoadGap.TRIALS_OVERLAP
    if planned.identity.phase is contracts.Phase.LIVE_LOW_LOAD:
        alone = len(live) == len(attempt.requests) and all(one.student == 0 for one in (*attempt.trials, *live))
        return None if alone else LoadGap.NOT_ALONE
    if planned.identity.phase is not contracts.Phase.LIVE_CLASS_LOAD or not trials:
        return None
    seats = range(1, planned.identities)
    background = [request for request in attempt.requests if request.traffic is Traffic.BACKGROUND]
    if any(one.student != 0 for one in (*attempt.trials, *live)) or any(one.student not in seats for one in background):
        return LoadGap.SEATS_UNPLANNED
    plan = reading.bundle.plan
    windows = sorted((trial.started_ms, trial.ended_ms) for trial in trials)
    pause = _longest_pause_ms(plan)
    conversations = plan.spec.conversations[planned.identity.shape]
    for seat in seats:
        gap = _background_gap([one for one in background if one.student == seat], windows, pause, conversations)
        if gap is not None:
            return gap
    return None


def load_evidence(reading: Reading, shape: Shape, load: Load = Load.CLASS) -> list[dict[str, Any]]:
    """Each live cell of a shape read at a load, and whether its evidence shows that load."""
    measured = {attempt.record.cell: attempt for attempt in _measured(reading.bundle)}
    workload = reading.manifest.workload
    rows = []
    for planned in reading.bundle.plan.cells:
        identity = planned.identity
        if identity.shape is not shape or load not in quality.loads_of(workload, identity.cell):
            continue
        attempt = measured.get(identity.cell)
        gap = _load_gap(reading, planned, attempt)
        background = (
            0
            if attempt is None
            else len({one.student for one in attempt.requests if one.traffic is Traffic.BACKGROUND})
        )
        rows.append(
            {
                "cell": identity.cell,
                "repetition": identity.repetition,
                "status": MET if gap is None else UNKNOWN,
                "gap": None if gap is None else gap.value,
                "background_seats": background,
                "background_seats_planned": planned.identities - 1 if identity.phase in LIVE_PHASES else 0,
            }
        )
    return rows


def _hosts_served(reading: Reading, shape: Shape) -> bool:
    """Whether the run's host count is the one that served the shape: requested and observed in
    the manifest, and observed by each attempt its capacity and its quality are read on, with each
    of those hosts accounted for."""
    manifest = reading.manifest
    hosts = manifest.observed.host_count
    if hosts is None or hosts != manifest.requested.host_count:
        return False
    plan = reading.bundle.plan
    read_on = [
        attempt
        for attempt in _measured(reading.bundle)
        if (planned := plan.cells[attempt.record.cell]).identity.shape is shape
        and (Role.CLASS_FLOOR in planned.roles or planned.identity.phase in LIVE_PHASES)
    ]
    return bool(read_on) and all(
        attempt.record.hosts_observed == hosts and len(attempt.checkpoint.hosts) == hosts for attempt in read_on
    )


def _capacity_seats(reading: Reading, shape: Shape) -> bool:
    """Whether every class-size repetition of a shape was sent from the class's seats."""
    shaped = reading.capacity.shapes.get(shape)
    if shaped is None or not shaped.repetitions:
        return False
    plan = reading.bundle.plan
    measured = {attempt.record.cell: attempt for attempt in _measured(reading.bundle)}
    return all(
        _seats_shown(plan, plan.cells[cell.identity.cell], measured.get(cell.identity.cell))
        for cell in shaped.repetitions
    )


def _checks(reading: Reading, shape: Shape, relative: Outcome | str) -> dict[str, str]:
    """The checks a shape needs at class size, each met, missed or unknown."""
    shaped = reading.capacity.shapes.get(shape)
    verdict = None if shaped is None else shaped.verdict
    capacity_check = MET if verdict is ShapeVerdict.PASS else MISSED if verdict is ShapeVerdict.KNOWN_MISS else UNKNOWN
    if capacity_check == MET and not _capacity_seats(reading, shape):
        capacity_check = UNKNOWN
    at_class = reading.quality.get(Load.CLASS)
    cells = [] if at_class is None else [cell for cell in at_class.cells if cell.family in SHAPE_FAMILIES[shape]]
    families = {cell.family for cell in cells}
    if any(cell.floor_outcome is Outcome.FAIL for cell in cells):
        absolute = MISSED
    elif cells and families == set(SHAPE_FAMILIES[shape]) and all(cell.floor_outcome is Outcome.PASS for cell in cells):
        absolute = MET
    else:
        absolute = UNKNOWN
    if relative in (BASELINE, NO_REFERENCE):
        paired = UNKNOWN
    else:
        paired = {Outcome.PASS: MET, Outcome.FAIL: MISSED}.get(Outcome(relative), UNKNOWN)
    loaded = load_evidence(reading, shape)
    return {
        "capacity": capacity_check,
        "absolute_quality": absolute,
        "relative_quality": paired,
        "relative_basis": relative.value if isinstance(relative, Outcome) else relative,
        "class_load_evidence": MET if loaded and all(row["status"] == MET for row in loaded) else UNKNOWN,
        "host_count": MET if _hosts_served(reading, shape) else UNKNOWN,
    }


REQUIRED_CHECKS = ("capacity", "absolute_quality", "relative_quality", "class_load_evidence", "host_count")


def recommend(runs: Sequence[tuple[str, Reading, Mapping[Shape, Outcome] | str]]) -> dict[str, Any]:
    """The recommendation for each shape at the class size, from the runs given: each run's name,
    its reading, and its relative check (the paired decision of each shape at class load, or
    `BASELINE`, or `NO_REFERENCE`)."""
    students = runs[0][1].manifest.workload.students
    shapes: dict[str, Any] = {}
    qualifying: dict[str, set[str]] = {}
    for shape in Shape:
        tested = []
        for name, reading, relative in runs:
            basis = relative if isinstance(relative, str) else relative.get(shape, Outcome.INCONCLUSIVE)
            checks = _checks(reading, shape, basis)
            states = [checks[key] for key in REQUIRED_CHECKS]
            status = MET if all(state == MET for state in states) else MISSED if MISSED in states else UNKNOWN
            hosts = reading.manifest.observed.host_count
            tested.append({"run": name, "host_count": hosts, "status": status, "checks": checks})
            if status == MET:
                qualifying.setdefault(name, set()).add(shape.value)
        passing = [one for one in tested if one["status"] == MET]
        if passing:
            best = min(passing, key=lambda one: one["host_count"])
            outcome = {"status": "recommended", "host_count": best["host_count"], "run": best["run"]}
        elif all(one["status"] == MISSED for one in tested):
            outcome = {"status": "not_met", "host_count": None, "run": None}
        else:
            outcome = {"status": UNKNOWN, "host_count": None, "run": None}
        shapes[shape.value] = {**outcome, "class_size": students, "tested": tested}
    both = [name for name, held in qualifying.items() if held == {shape.value for shape in Shape}]
    hosts_of = {name: reading.manifest.observed.host_count for name, reading, _relative in runs}
    common = min(both, key=lambda name: hosts_of[name] or 0) if both else None
    return {
        "class_size": students,
        "shapes": shapes,
        "common": None if common is None else {"run": common, "host_count": hosts_of[common]},
        # What no run measured is never projected from what one did.
        "untested": UNKNOWN,
    }


# --- The summary --------------------------------------------------------------------------------


def scorecard(reading: Reading, *, usd_per_host_hour: float, rate_source: str) -> dict[str, Any]:
    """The summary of one run read alone."""
    manifest = reading.manifest
    workload = manifest.workload
    return {
        "kind": "summary",
        "summary_version": SUMMARY_VERSION,
        "schema_version": contracts.SCHEMA_VERSION,
        "run_id": manifest.run_id,
        "workload_id": manifest.workload_id,
        "treatment_id": manifest.treatment_id,
        "source_commit": manifest.run.source_commit,
        "suite": reading.bundle.suite,
        "profile": workload.profile.value,
        "class_size": workload.students,
        "objectives": workload.objectives.to_json(),
        "sampling": workload.sampling.to_json(),
        # The price as given, not rounded: regenerating the summary at it gives the same bytes.
        "host_price": {"usd_per_host_hour": float(usd_per_host_hour), "source": rate_source, "estimate": True},
        "conditions": _conditions(manifest),
        "capacity": _capacity(reading),
        "capacity_sweep": _sweep(reading),
        "class_load_evidence": {shape.value: load_evidence(reading, shape) for shape in Shape},
        "capacity_by_harness": _by_harness(reading),
        "quality": _quality(reading),
        "distributions": _distributions(reading),
        "tokenizer": _tokenizer(reading),
        "recommendation": recommend([("run", reading, NO_REFERENCE)]),
    }


def dump(summary: Mapping[str, Any]) -> str:
    """The summary's bytes: sorted keys, two-space indents, one final newline."""
    return json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n"


def dump_first(document: Mapping[str, Any], first: str) -> str:
    """One JSON document whose key `first` comes first, every other key sorted: the comparison
    as `--json` prints it, its treatment diff before anything else."""
    rest = json.loads(dump({key: value for key, value in document.items() if key != first}))
    head = json.loads(dump({first: document[first]}))
    return json.dumps(head | rest, indent=2, allow_nan=False) + "\n"


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Derived:
    summary: str
    report: str
    digest: str

    def files(self) -> dict[str, str]:
        return {SUMMARY: self.summary, REPORT: self.report, DIGEST: self.digest + "\n"}


def regenerate(bundle: Bundle, *, usd_per_host_hour: float, rate_source: str) -> Derived:
    """The derived files of a bundle: its summary, its report and the summary's digest."""
    check_price(usd_per_host_hour, rate_source)
    summary = scorecard(
        read(bundle, usd_per_host_hour=usd_per_host_hour), usd_per_host_hour=usd_per_host_hour, rate_source=rate_source
    )
    text = dump(summary)
    digest = digest_of(text)
    return Derived(summary=text, report=render(summary, digest), digest=digest)


def write_derived(directory: Path, derived: Derived) -> None:
    for name, text in derived.files().items():
        (directory / name).write_text(text, encoding="utf-8")


# --- Comparing two runs -------------------------------------------------------------------------


def treatment_diff(reference: Manifest, candidate: Manifest) -> dict[str, Any]:
    """What differs between two runs, before anything is compared: the requested settings, each
    with its lever, and the settings of the run that measured each."""
    settings: list[dict[str, Any]] = [
        {
            "setting": field.name,
            "lever": LEVERS[field.name].value,
            "reference": _value(getattr(reference.requested, field.name)),
            "candidate": _value(getattr(candidate.requested, field.name)),
        }
        for field in dataclasses.fields(Settings)
        if getattr(reference.requested, field.name) != getattr(candidate.requested, field.name)
    ]
    runs = [
        {
            "setting": field.name,
            "reference": _value(getattr(reference.run, field.name)),
            "candidate": _value(getattr(candidate.run, field.name)),
        }
        for field in dataclasses.fields(type(reference.run))
        if field.name != "ordinal" and getattr(reference.run, field.name) != getattr(candidate.run, field.name)
    ]
    observed = [
        {
            "setting": field.name,
            "lever": LEVERS[field.name].value,
            "reference": _value(getattr(reference.observed, field.name)),
            "candidate": _value(getattr(candidate.observed, field.name)),
        }
        for field in dataclasses.fields(Settings)
        if getattr(reference.observed, field.name) != getattr(candidate.observed, field.name)
    ]
    contradicted = [
        {
            "run": name,
            "setting": row["setting"],
            "lever": row["lever"],
            "requested": row["requested"],
            "observed": row["observed"],
        }
        for name, manifest in (("reference", reference), ("candidate", candidate))
        for row in _conditions(manifest)
        if row["status"] == "differs"
    ]
    levers = sorted({LEVERS[row["setting"]] for row in settings})
    if not levers:
        label = "repeat"
    elif len(levers) > 1:
        label = "combined"
    else:
        label = "flags_only" if levers[0] in FLAGS else levers[0].value
    return {
        "label": label,
        "levers": [lever.value for lever in levers],
        "settings": settings,
        "observed": observed,
        "contradicted": contradicted,
        "runs": runs,
    }


def _launch_fingerprint(bundle: Bundle) -> str | None:
    """The fingerprint the run's release requests, when every attempt says the same one."""
    said = {attempt.checkpoint.requested_launch_fingerprint for attempt in bundle.attempts}
    return said.pop() if len(said) == 1 else None


def _launched_as_requested(bundle: Bundle) -> bool:
    """Whether every serving host of every attempt is accounted for and said the requested launch
    fingerprint through its time in service: each attempt lists as many hosts as its record
    observed, which is the run's host count, and no host is left without a fingerprint."""
    requested = _launch_fingerprint(bundle)
    hosts = bundle.manifest.requested.host_count
    if requested is None or not bundle.attempts or hosts is None:
        return False
    return all(
        attempt.record.hosts_observed == hosts
        and len(attempt.checkpoint.hosts) == hosts
        and all(host.launch_fingerprint == requested and host.launch_whole_window for host in attempt.checkpoint.hosts)
        for attempt in bundle.attempts
    )


def _gateway_as_requested(bundle: Bundle) -> bool:
    """Whether the gateway profile that served the run was observed, and is the one requested."""
    manifest = bundle.manifest
    asked = manifest.requested.gateway_profile_digest
    return asked is not None and manifest.observed.gateway_profile_digest == asked


def _served(bundle: Bundle, lever: Lever, changed: Sequence[str]) -> bool:
    """Whether the run's evidence shows the changed settings of a lever served as requested."""
    manifest = bundle.manifest
    as_observed = all(
        getattr(manifest.requested, name) is not None
        and getattr(manifest.observed, name) == getattr(manifest.requested, name)
        for name in changed
    )
    if lever is Lever.HOSTS:
        hosts = manifest.requested.host_count
        cells = all(
            attempt.record.hosts_observed == hosts and len(attempt.checkpoint.hosts) == hosts
            for attempt in bundle.attempts
        )
        return as_observed and ("host_count" not in changed or cells)
    if lever is Lever.LAUNCH_SETTINGS:
        # What the engine itself exposes must not say otherwise; what it does not expose is
        # established by the fingerprint alone.
        contradicted = any(
            getattr(manifest.observed, name) is not None
            and getattr(manifest.observed, name) != getattr(manifest.requested, name)
            for name in changed
        )
        return not contradicted and _launched_as_requested(bundle) and _gateway_as_requested(bundle)
    if lever is Lever.GATEWAY_PROFILE:
        # A flags-only change needs both provenances: the gateway's and each engine's.
        return as_observed and _gateway_as_requested(bundle) and _launched_as_requested(bundle)
    return as_observed


def read_manifest(directory: Path) -> Manifest:
    """The manifest of a bundle alone, held to its contract: what the treatment diff is made of,
    before the rest of the bundle is read."""
    try:
        return contracts.loads((directory / MANIFEST).read_bytes(), Manifest)
    except (OSError, ContractError):
        raise ReportError(Refusal.UNREADABLE_BUNDLE) from None


def check_workloads(reference: Manifest, candidate: Manifest) -> None:
    """A refusal unless the two runs are one planned workload, at the same floors and budgets."""
    if candidate.workload.objectives != reference.workload.objectives:
        raise ReportError(Refusal.CHANGED_FLOOR)
    if candidate.workload.sampling != reference.workload.sampling:
        raise ReportError(Refusal.CHANGED_SAMPLE_BUDGET)
    if candidate.workload_id != reference.workload_id:
        raise ReportError(Refusal.INCOMPATIBLE_WORKLOAD)


def check_comparable(reference: Bundle, candidate: Bundle) -> dict[str, Any]:
    """The diff of two runs, once they are shown comparable; a refusal otherwise."""
    ours, theirs = candidate.manifest, reference.manifest
    check_workloads(theirs, ours)
    diff = treatment_diff(theirs, ours)
    if diff["contradicted"]:
        # A run whose evidence says another setting served than it requested: no change can be
        # attributed to what either requested.
        raise ReportError(Refusal.CONTRADICTED_SETTING, Lever(diff["contradicted"][0]["lever"]))
    changed: dict[Lever, list[str]] = {}
    for row in diff["settings"]:
        changed.setdefault(LEVERS[row["setting"]], []).append(row["setting"])
    for lever, names in sorted(changed.items()):
        if not (_served(reference, lever, names) and _served(candidate, lever, names)):
            raise ReportError(Refusal.UNVERIFIED_TREATMENT, lever)
    declared = {row["setting"] for row in diff["settings"]}
    undeclared = sorted(
        LEVERS[row["setting"]]
        for row in diff["observed"]
        if row["reference"] is not None and row["candidate"] is not None and row["setting"] not in declared
    )
    if undeclared:
        # The two observed a setting different that neither requested apart: it is a change of its
        # own, even beside a declared one of its lever (another instance beside another count).
        raise ReportError(Refusal.UNDECLARED_TREATMENT, undeclared[0])
    fingerprints = (_launch_fingerprint(reference), _launch_fingerprint(candidate))
    if Lever.LAUNCH_SETTINGS in changed and fingerprints[0] == fingerprints[1]:
        # Both releases launch the same arguments: the declared change never reached an engine.
        raise ReportError(Refusal.UNVERIFIED_TREATMENT, Lever.LAUNCH_SETTINGS)
    if Lever.LAUNCH_SETTINGS not in changed and None not in fingerprints and fingerprints[0] != fingerprints[1]:
        raise ReportError(Refusal.UNDECLARED_TREATMENT, Lever.LAUNCH_SETTINGS)
    diff["verified"] = [lever.value for lever in sorted(changed)]
    return diff


def _realized(reading: Reading) -> dict[str, Any]:
    """The load each shape's class-size repetitions realized: what was sent and what came back."""
    found = {}
    for shape, one in reading.capacity.shapes.items():
        cells = {cell.identity.cell for cell in one.repetitions}
        requests = [
            request
            for attempt in _measured(reading.bundle)
            if attempt.record.cell in cells
            for request in attempt.requests
            if request.traffic is not Traffic.WARM_UP
        ]
        outputs = [
            float(request.usage.output_tokens)
            for request in requests
            if not request.failed and request.usage is not None and request.usage.output_tokens is not None
        ]
        found[shape.value] = {
            "requests": len(requests),
            "completed": sum(not request.failed for request in requests),
            "output_tokens_mean": _n(fmean(outputs)) if outputs else None,
            "output_tokens": _spread(outputs),
        }
    return found


# Why a paired decision is withheld: a run of the pair does not show the load it is read at.
LOAD_UNSHOWN = "load-unshown"


def _load_shown(reading: Reading, load: Load, shape: Shape | None) -> bool:
    """Whether every live cell a paired decision reads, of its shape (or of both, for the
    combined one) at its load, shows that load (`load_evidence`)."""
    shapes = list(Shape) if shape is None else [shape]
    rows = [row for one in shapes for row in load_evidence(reading, one, load)]
    return bool(rows) and all(row["status"] == MET for row in rows)


def _decision(decision: quality.Decision, unshown: Sequence[str]) -> dict[str, Any]:
    """A paired decision as the comparison shows it. One whose load a run of the pair does not
    show (`unshown`, the names of those runs) is withheld: inconclusive, whatever its interval."""
    outcome, reason = decision.outcome.value, None if decision.reason is None else decision.reason.value
    if unshown:
        outcome, reason = Outcome.INCONCLUSIVE.value, LOAD_UNSHOWN
    return {
        "load": decision.load.value,
        "shape": None if decision.shape is None else decision.shape.value,
        "outcome": outcome,
        "reason": reason,
        "load_unshown": list(unshown),
        "difference_points": _n(decision.difference_points),
        "lower_points": _n(decision.lower_points),
        "upper_points": _n(decision.upper_points),
        "margin_points": _n(decision.margin_points),
        "draws": decision.draws,
    }


def compare(reference: Bundle, candidate: Bundle, *, usd_per_host_hour: float, rate_source: str) -> dict[str, Any]:
    """The comparison of a candidate against a reference: the diff first, the paired decisions,
    the realized load of each, task time across their tokenizers, and the recommendation over the
    two. A refusal when they cannot be compared."""
    check_price(usd_per_host_hour, rate_source)
    diff = check_comparable(reference, candidate)
    theirs = read(reference, usd_per_host_hour=usd_per_host_hour)
    ours = read(candidate, usd_per_host_hour=usd_per_host_hour)
    paired = quality.compare(ours.records, theirs.records)
    # A pair is compared at a load only where both runs show it: an unloaded reference does not
    # qualify a loaded candidate, nor the other way round.
    decisions = [
        _decision(
            decision,
            [
                name
                for name, run in (("reference", theirs), ("candidate", ours))
                if not _load_shown(run, decision.load, decision.shape)
            ],
        )
        for decision in paired.decisions
    ]
    relative = {
        shape: Outcome(next(one for one in decisions if one["load"] == Load.CLASS and one["shape"] == shape)["outcome"])
        for shape in Shape
    }
    return {
        "kind": "comparison",
        "summary_version": SUMMARY_VERSION,
        "diff": diff,
        "decisions": decisions,
        "realized": {"reference": _realized(theirs), "candidate": _realized(ours)},
        "tokenizers": {"reference": _tokenizer(theirs), "candidate": _tokenizer(ours)},
        "recommendation": recommend([("reference", theirs, BASELINE), ("candidate", ours, relative)]),
        "reference": scorecard(theirs, usd_per_host_hour=usd_per_host_hour, rate_source=rate_source),
        "candidate": scorecard(ours, usd_per_host_hour=usd_per_host_hour, rate_source=rate_source),
    }


# --- Markdown -----------------------------------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4g}" if abs(value) < 1e4 else f"{value:.0f}"
    return str(value)


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines + [""]


def _p(spread: Mapping[str, Any]) -> str:
    return f"{_cell(spread['p50'])} / {_cell(spread['p90'])} (n={spread['count']})"


def _recommendation_lines(recommendation: Mapping[str, Any]) -> list[str]:
    lines = ["## Recommendation", ""]
    rows = []
    for shape, one in recommendation["shapes"].items():
        for tested in one["tested"]:
            checks = tested["checks"]
            rows.append(
                (
                    shape,
                    one["status"],
                    one["host_count"],
                    tested["run"],
                    tested["host_count"],
                    checks["capacity"],
                    checks["absolute_quality"],
                    f"{checks['relative_quality']} ({checks['relative_basis']})",
                    checks["class_load_evidence"],
                    checks["host_count"],
                )
            )
    lines += _table(
        (
            "Shape",
            "Recommendation",
            "Host count",
            "Run",
            "Tested hosts",
            "Capacity",
            "Absolute quality (class load)",
            "Relative quality (class load)",
            "Class load shown",
            "Hosts served",
        ),
        rows,
    )
    common = recommendation["common"]
    lines.append(
        f"Class size {recommendation['class_size']}. Common configuration: "
        + ("none" if common is None else f"{common['host_count']} host(s), run {common['run']}")
        + ". Any other class size or host count: unknown (not measured, not projected)."
    )
    return [*lines, ""]


def _run_lines(summary: Mapping[str, Any], level: str = "##") -> list[str]:
    lines = [f"{level} Configured and observed conditions", ""]
    lines += _table(
        ("Setting", "Requested", "Observed", "Status"),
        ((row["setting"], row["requested"], row["observed"], row["status"]) for row in summary["conditions"]),
    )
    price = summary["host_price"]
    lines += [
        f"{level} Capacity at the class size",
        "",
        f"Host cost is an estimate at {_cell(price['usd_per_host_hour'])} USD a host-hour ({price['source']} rate).",
        "",
    ]
    for shape, one in summary["capacity"].items():
        row = one["limiting_row"]
        limit = (
            "none"
            if row is None
            else f"{row['floor']} (headroom {_cell(row['headroom'])}, repetition {row['repetition']}"
            + (", missed)" if row["missed"] else ")")
        )
        seats = "yes" if one["seats_shown"] and all(one["seats_shown"]) else "no"
        heading = (
            f"**{shape}** at {one['agent_slots']} agent slots: {one['verdict']}; limiting row: {limit};"
            f" sent from the class's seats in every repetition: {seats}."
        )
        lines += [heading, ""]
        lines += _table(
            (
                "Rep",
                "Verdict",
                "Reasons",
                "p95 TTFT ms (later turns/min)",
                "p50 / p10 tok/s (streams/min)",
                "Failed / attempts",
                "Failed share (Wilson 95%)",
                "Preemptions",
                "Engine tok/s (by host)",
                "Cost / M output USD (estimate)",
            ),
            (
                (
                    rep["repetition"],
                    rep["verdict"],
                    ", ".join(rep["reasons"]) or "–",
                    f"{_cell(rep['ttft_p95_ms'])} ({rep['later_turns']}/{rep['later_turns_min']})",
                    (
                        f"{_cell(rep['speed_p50_tokens_per_s'])} / {_cell(rep['speed_p10_tokens_per_s'])} "
                        f"({rep['streams']}/{rep['streams_min']})"
                    ),
                    f"{rep['failed']} / {rep['attempts']}"
                    + (" (observed floor)" if rep["failed_observed_floor"] else ""),
                    f"{_cell(rep['failed_share'])} ({_cell(rep['failed_wilson_low'])}–{_cell(rep['failed_wilson_high'])})",
                    rep["preemptions"],
                    _by_host(rep),
                    rep["host_cost_estimate_per_million_output_usd"],
                )
                for rep in one["repetitions"]
            ),
        )
        spread = one["spread"]
        lines += [
            "Across repetitions: p95 TTFT "
            + " to ".join(_cell(v) for v in spread["ttft_p95_ms"])
            + " ms, p10 speed "
            + " to ".join(_cell(v) for v in spread["speed_p10_tokens_per_s"])
            + " tok/s, failed share "
            + " to ".join(_cell(v) for v in spread["failed_share"])
            + ".",
            "",
        ]
    lines += [f"{level} Capacity sweep (every measured load)", ""]
    lines += _table(
        ("Shape", "Agent slots", "Rep", "Class size", "Verdict", "p95 TTFT ms", "p50 / p10 tok/s", "Failed share"),
        (
            (
                row["shape"],
                row["agent_slots"],
                row["repetition"],
                row["class_floor"],
                row["verdict"],
                f"{_cell(row['ttft_p95_ms'])} ({row['later_turns']})",
                f"{_cell(row['speed_p50_tokens_per_s'])} / {_cell(row['speed_p10_tokens_per_s'])} ({row['streams']})",
                row["failed_share"],
            )
            for row in summary["capacity_sweep"]
        ),
    )
    lines += [f"{level} Class load behind the live tasks", ""]
    lines += _table(
        ("Shape", "Cell", "Rep", "Shown", "Gap", "Background seats (planned)"),
        (
            (
                shape,
                row["cell"],
                row["repetition"],
                row["status"],
                row["gap"],
                f"{row['background_seats']} ({row['background_seats_planned']})",
            )
            for shape, rows in summary["class_load_evidence"].items()
            for row in rows
        ),
    )
    lines += [f"{level} Live requests by shape, harness and load", ""]
    lines += _table(
        ("Shape", "Load", "Harness", "Requests", "Failed", "p95 TTFT ms (later turns)", "p50 / p10 tok/s (streams)"),
        (
            (
                row["shape"],
                row["load"],
                row["harness"],
                row["requests"],
                row["failed"],
                f"{_cell(row['ttft_p95_ms'])} ({row['later_turns']})",
                f"{_cell(row['speed_p50_tokens_per_s'])} / {_cell(row['speed_p10_tokens_per_s'])} ({row['streams']})",
            )
            for row in summary["capacity_by_harness"]
        ),
    )
    lines += [f"{level} Quality by family, harness and load", ""]
    for load, one in summary["quality"].items():
        lines += _table(
            ("Load", "Family", "Harness", "Successes / attempts (planned)", "Instances 3/3", "Floor"),
            (
                (
                    load,
                    cell["family"],
                    cell["harness"],
                    f"{cell['successes']} / {cell['attempts']} ({cell['planned']})",
                    f"{cell['instances_all_passed']} / {cell['instances']}",
                    f"{cell['floor_outcome']} (at {_cell(cell['floor'])})",
                )
                for cell in one["cells"]
            ),
        )
        rates = ", ".join(f"{shape} {_cell(rate)}" for shape, rate in one["shapes"].items())
        lines += [f"Weighted success at {load}: {rates}; combined {_cell(one['combined'])}.", ""]
    lines += [f"{level} Replay and live tasks by shape (p50 / p90)", ""]
    rows = []
    for shape, sides in summary["distributions"].items():
        replay, live = sides["replay"], sides["live"]
        for measure in ("input_tokens", "output_tokens", "cached_share", "tool_delay_ms", "turns_per_task", "fan_out"):
            rows.append((shape, measure, _p(replay[measure]), _p(live[measure])))
        for route in replay["route_mix"]:
            rows.append((shape, f"route {route}", replay["route_mix"][route], live["route_mix"][route]))
    lines += _table(("Shape", "Measure", "Replay", "Live tasks"), rows)
    return lines


def _by_host(rep: Mapping[str, Any]) -> str:
    hosts = rep["engine_output_tokens_per_s_by_host"]
    if hosts is None:
        return "–"
    each = ", ".join(f"{one['host']}: {_cell(one['output_tokens_per_s'])}" for one in hosts)
    return f"{_cell(rep['engine_output_tokens_per_s'])} ({each})"


def _tokenizer_rows(name: str, tokenizer: Mapping[str, Any]) -> list[tuple[object, ...]]:
    revision = tokenizer["tokenizer_revision"]
    return [
        (
            name,
            revision["requested"],
            revision["observed"],
            load,
            one["successful_task_time_ms"],
            one["tasks_per_hour"],
            f"{one['successes']} / {one['attempts']}",
            one["emitted_tokens_per_turn"],
        )
        for load, one in tokenizer["loads"].items()
    ]


_TOKENIZER_HEADER = (
    "Run",
    "Tokenizer (requested)",
    "Tokenizer (observed)",
    "Load",
    "Successful task ms",
    "Completions / hour",
    "Successes / attempts",
    "Emitted tokens / turn",
)


def render(summary: Mapping[str, Any], digest: str) -> str:
    """The report of one run, from its summary alone."""
    lines = [
        "# Evaluation scorecard",
        "",
        f"Summary digest: `{digest}`",
        "",
        f"Run `{summary['run_id']}`, workload `{summary['workload_id']}`, treatment `{summary['treatment_id']}`.",
        f"Suite {summary['suite']}, profile {summary['profile']}, class size {summary['class_size']}.",
        "",
    ]
    lines += _recommendation_lines(summary["recommendation"])
    lines += _run_lines(summary)
    lines += ["## Task time and completions across tokenizers", ""]
    lines += _table(_TOKENIZER_HEADER, _tokenizer_rows("run", summary["tokenizer"]))
    return "\n".join(lines).rstrip("\n") + "\n"


def render_diff(diff: Mapping[str, Any]) -> str:
    """The treatment diff, which `compare` prints before anything else."""
    lines = [f"treatment diff ({diff['label']}):"]
    lines += [
        f"  {row['setting']} [{row['lever']}]: {_cell(row['reference'])} -> {_cell(row['candidate'])}"
        for row in diff["settings"]
    ] or ["  no requested setting differs"]
    lines += [
        f"  observed {row['setting']} [{row['lever']}]: {_cell(row['reference'])} -> {_cell(row['candidate'])}"
        for row in diff["observed"]
    ]
    lines += [
        f"  contradicted in {row['run']}: {row['setting']} [{row['lever']}] requested {_cell(row['requested'])},"
        f" observed {_cell(row['observed'])}"
        for row in diff["contradicted"]
    ]
    lines += [f"  run {row['setting']}: {_cell(row['reference'])} -> {_cell(row['candidate'])}" for row in diff["runs"]]
    return "\n".join(lines) + "\n"


def render_comparison(comparison: Mapping[str, Any]) -> str:
    """The comparison's scorecard, after its diff."""
    diff = comparison["diff"]
    lines = [
        "# Comparison scorecard",
        "",
        f"Label: {diff['label']}. Verified levers: {', '.join(diff['verified']) or 'none'}.",
        "",
    ]
    lines += _recommendation_lines(comparison["recommendation"])
    lines += ["## Paired quality (candidate minus reference, points)", ""]
    lines += _table(
        ("Load", "Shape", "Outcome", "Reason", "Difference", "95% interval", "Margin", "Draws"),
        (
            (
                one["load"],
                one["shape"] or "combined",
                one["outcome"],
                one["reason"] if not one["load_unshown"] else f"{one['reason']} ({', '.join(one['load_unshown'])})",
                one["difference_points"],
                f"{_cell(one['lower_points'])} to {_cell(one['upper_points'])}",
                one["margin_points"],
                one["draws"],
            )
            for one in comparison["decisions"]
        ),
    )
    lines += ["## Realized load at the class size", ""]
    lines += _table(
        ("Run", "Shape", "Requests", "Completed", "Output tokens mean", "Output tokens p50 / p90"),
        (
            (name, shape, one["requests"], one["completed"], one["output_tokens_mean"], _p(one["output_tokens"]))
            for name, shapes in comparison["realized"].items()
            for shape, one in shapes.items()
        ),
    )
    lines += ["## Task time and completions across tokenizers", ""]
    lines += _table(
        _TOKENIZER_HEADER,
        [row for name, tokenizer in comparison["tokenizers"].items() for row in _tokenizer_rows(name, tokenizer)],
    )
    lines += ["## Candidate", ""]
    lines += _run_lines(comparison["candidate"], "###")
    lines += ["## Reference", ""]
    lines += _run_lines(comparison["reference"], "###")
    return "\n".join(lines).rstrip("\n") + "\n"


# --- Exporting ----------------------------------------------------------------------------------

type Scan = Callable[[str, str], Sequence[object]]


def hygiene_scan() -> Scan:
    """The public hygiene scanner's `scan_text`, loaded from its file and never changed."""
    name = "_evaluate_hygiene_scan"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, HYGIENE_SCANNER)
        if spec is None or spec.loader is None:
            raise ImportError("the hygiene scanner")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module.scan_text


def export(
    source: Path,
    destination: Path,
    *,
    usd_per_host_hour: float,
    rate_source: str,
    root: Path = REPO,
    scan: Scan | None = None,
) -> Derived:
    """Writes the bundle in `source`, validated and scanned, with its derived files, as the new
    directory `destination` under `docs/evaluations/` of `root`. Nothing is written unless every
    check passes."""
    evaluations = (root / EVALUATIONS).resolve()
    target = destination.resolve()
    if target.parent != evaluations or not _EXPORT_NAME.fullmatch(target.name):
        raise ReportError(Refusal.EXPORT_OUTSIDE_EVALUATIONS)
    if target.exists():
        raise ReportError(Refusal.EXPORT_EXISTS)
    bundle = load(source)
    derived = regenerate(bundle, usd_per_host_hour=usd_per_host_hour, rate_source=rate_source)
    files = {**bundle.files, **derived.files()}
    if any(text != files[name] for name, text in bundle.derived.items()):
        raise ReportError(Refusal.STALE_DERIVED)
    scan = hygiene_scan() if scan is None else scan
    public = EVALUATIONS / target.name
    found = [name for name, text in sorted(files.items()) if scan(text, (public / name).as_posix())]
    if found:
        raise ReportError(Refusal.HYGIENE_FINDING)
    evaluations.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=evaluations))
    try:
        for name, text in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return derived
