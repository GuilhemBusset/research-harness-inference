"""The scorecard of a run regenerated from its saved evidence, the offline comparison of two runs
and the public export (docs/design/agent-session-evaluation.md, "Reading a result" and
"Reproducing and comparing runs").

The saved bundle under `fixtures/report/` is the qualification plan of the course suite for a
class of two, on two hosts, written by `build` below: `uv run --locked python` on this file, with
the import path `scripts/evaluate.sh` sets, writes it again. Its class-size repetitions are 102 later
turns of 20 tokens a second each. Hands-on answers the slowest later turns in 3.9 s, under the 5 s
limit; several-agents streams a fifth of its turns at 5 tokens a second in repetition 1, below the
10 tokens a second p10 floor. Each live cell at class load has the other seat's replay behind its
trials. Every trial passes, but one optimization attempt on OpenCode at class
load and three debugging attempts on Codex at low load. The reference of a comparison is the same
plan on one host, which answers hands-on in 6.9 s and passes fewer attempts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from evaluate import cli, contracts, plan, report
from evaluate.capacity import speed_of
from evaluate.contracts import (
    METRIC_UNITS,
    CellRecord,
    CellStatus,
    Checkpoint,
    FinishReason,
    GpuType,
    Grade,
    Harness,
    HostLaunch,
    HostMetric,
    HostSample,
    InstanceType,
    KvDtype,
    Manifest,
    PathCategory,
    Phase,
    Profile,
    Quantization,
    RequestOutcome,
    RequestRecord,
    Route,
    Run,
    SendTally,
    Settings,
    SpeculativeDecoding,
    Traffic,
    TrialOutcome,
    TrialRecord,
    Usage,
)
from evaluate.plan import Role
from evaluate.report import Refusal, ReportError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from evaluate.contracts import Document

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "report"
PRICE, SOURCE = cli._example()[1], cli.Source.EXAMPLE  # the rate `plan` prices with by default
START, ADMITTED, END = 100_000, 1_000_000, 1_120_000
ROUTES = {Harness.CLAUDE_CODE: Route.MESSAGES, Harness.CODEX: Route.RESPONSES}


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def launch(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Variant:
    """What a built run is: its treatment, what served it and what it measured."""

    host_count: int = 2
    step_budget_tokens: int = 8192
    launched: str | None = launch("step-8192")
    requested_launch: str | None = launch("step-8192")
    hands_on_ttft_ms: int = 3000
    slow_several_agents_repetition: int | None = 1
    later_turns: int | None = None  # every turn admissions allow
    fails: frozenset[tuple[str, str, str, int]] = frozenset(
        {("class_load", "optimization.t01.i02", "opencode", 1)}
        | {("low_load", f"debugging.t01.i0{n}", "codex", 0) for n in (1, 2, 3)}
    )
    objectives: dict[str, float] = field(default_factory=dict)
    sampling: dict[str, int] = field(default_factory=dict)
    input_seed: int | None = None
    drop_cells: frozenset[int] = frozenset()


REFERENCE = Variant(
    host_count=1,
    hands_on_ttft_ms=6000,
    fails=Variant().fails
    | {("class_load", "optimization.t01.i01", "claude_code", 2), ("class_load", "research.t01.i02", "claude_code", 0)},
)


def settings(variant: Variant, *, observed: bool) -> Settings:
    return Settings(
        host_count=variant.host_count,
        instance_type=InstanceType.G7E_2XLARGE,
        gpu_type=GpuType.RTX_PRO_6000,
        gpus_per_host=1,
        model_revision="b" * 40,
        tokenizer_revision="c" * 40,
        quantization=Quantization.BITS_4,
        engine_image_digest="sha256:" + "e" * 64,
        vllm_version="0.33.0",
        # Neither is in any series: observed only when the driver reads it.
        context_limit_tokens=None if observed else 131_072,
        kv_dtype=KvDtype.BITS_8,
        prefix_caching=True,
        tensor_parallelism=1,
        step_budget_tokens=None if observed else variant.step_budget_tokens,
        speculative_decoding=SpeculativeDecoding.OFF,
        renderer_digest=None if observed else "sha256:" + "f" * 64,
        gateway_profile_digest="sha256:" + "a" * 64,
    )


def tally(rows: Sequence[RequestRecord]) -> SendTally:
    return SendTally(
        attempts=len(rows),
        unfinished=0,
        completed=sum(not row.failed for row in rows),
        truncated_stream=0,
        transport_error=0,
        http_refusal=0,
        error_event=0,
        retries=0,
        usage_missing=sum(row.usage is None for row in rows),
        eligible=sum(speed_of(row) is not None for row in rows),
    )


def replay(cell: plan.PlannedCell, variant: Variant) -> list[RequestRecord]:
    """The replayed conversations of a class-size cell: each slot one conversation, turn after
    turn, a tool pause of 5 to 8 s between them, until admissions close or, for a variant that
    sets its later turns, until the slots have sent that many."""
    identity = cell.identity
    per_student = 1 if identity.shape.value == "hands_on" else 2
    turns = None if variant.later_turns is None else -(-variant.later_turns // identity.agent_slots)
    rows: list[RequestRecord] = []
    for slot in range(identity.agent_slots):
        eligible, turn = START + 1000 + slot * 100, 0
        while (eligible <= ADMITTED) if turns is None else (turn <= turns):
            sent = eligible + 5
            hands_on = identity.shape.value == "hands_on"
            ttft = variant.hands_on_ttft_ms + 100 * (turn % 10) if hands_on and turn else 1500
            slow = identity.repetition == variant.slow_several_agents_repetition and not hands_on and turn % 5 == 1
            first = sent + ttft
            last = first + (4000 if slow else 1000)
            rows.append(
                RequestRecord(
                    request=len(rows),
                    cell=identity.cell,
                    student=slot // per_student,
                    conversation=slot,
                    turn=turn,
                    traffic=Traffic.BACKGROUND,
                    trial=None,
                    route=Route.MESSAGES,
                    eligible_ms=eligible,
                    sent_ms=sent,
                    first_output_ms=first,
                    first_answer_ms=first,
                    last_output_ms=last,
                    ended_ms=last + 10,
                    outcome=RequestOutcome.COMPLETED,
                    finish_reason=FinishReason.TOOL_USE,
                    usage=Usage(
                        input_tokens=20_000 + 1000 * turn, cached_input_tokens=20_000 if turn else 0, output_tokens=21
                    ),
                )
            )
            eligible, turn = last + 10 + 5000 + 1000 * (turn % 4), turn + 1
        # The conversation's last turn ends it.
        rows[-1] = replace(rows[-1], finish_reason=FinishReason.STOP)
    return rows


def samples(cell: int, hosts: int) -> list[HostSample]:
    found = []
    for host in range(hosts):
        for at in range(START - 5000, END + 30_000, 30_000):
            # Four output tokens a second from the start of the cell, and no preemption.
            values = {
                HostMetric.OUTPUT_TOKENS_TOTAL: 4 * (min(max(at, START), END) - START) / 1000,
                HostMetric.PREEMPTIONS_TOTAL: 0.0,
            }
            found += [
                HostSample(cell=cell, host=host, at_ms=at, metric=metric, unit=METRIC_UNITS[metric], value=value)
                for metric, value in values.items()
            ]
    return found


def live(
    cell: plan.PlannedCell, at: int, executions: int, variant: Variant, workload: contracts.Workload
) -> tuple[list[Document], int]:
    """The trials of a live cell, one after the other from `at`, each two turns of its harness
    (and a delegated worker beside the first, for a family that delegates), and at class load the
    other seats' replay from before the first trial until after the last. Returns the cell's
    observations and when its last trial ended."""
    begun = at
    load = "low_load" if cell.identity.phase is Phase.LIVE_LOW_LOAD else "class_load"
    required = {task.task_id: task.required_properties for task in plan.load_suite().suite.tasks}
    observations: list[Document] = []
    for number in cell.trials:
        trial = workload.trials[number]
        failed = (load, trial.instance_id, trial.harness.value, trial.attempt) in variant.fails
        properties = required[trial.instance_id.rsplit(".", 1)[0]]
        route = ROUTES.get(trial.harness, Route.CHAT_COMPLETIONS)
        delegates = trial.instance_id.startswith(("analysis", "research"))
        rows = []
        for conversation, turn, sent in [(0, 0, at + 1000), (0, 1, at + 9000)] + (
            [(1, 0, at + 1500)] if delegates else []
        ):
            rows.append(
                RequestRecord(
                    request=len(observations) + len(rows),
                    cell=cell.identity.cell,
                    student=0,
                    conversation=conversation,
                    turn=turn,
                    traffic=Traffic.LIVE,
                    trial=number,
                    route=route,
                    eligible_ms=sent,
                    sent_ms=sent,
                    first_output_ms=sent + 800,
                    first_answer_ms=sent + 900,
                    last_output_ms=sent + 3800,
                    ended_ms=sent + 3810,
                    outcome=RequestOutcome.COMPLETED,
                    finish_reason=FinishReason.TOOL_USE if turn == 0 else FinishReason.STOP,
                    usage=Usage(input_tokens=6000 + 2000 * turn, cached_input_tokens=4000 * turn, output_tokens=61),
                )
            )
        ended = at + 60_000 + 10_000 * (trial.attempt % 2)
        observations += rows
        observations.append(
            TrialRecord(
                execution=executions + len([one for one in observations if isinstance(one, TrialRecord)]),
                trial=number,
                replaces=None,
                student=0,
                started_ms=at,
                ended_ms=ended,
                outcome=TrialOutcome.FAILED if failed else TrialOutcome.PASSED,
                failure=contracts.FailureReason.REJECTED if failed else None,
                invalid=None,
                grade=Grade(properties=(not failed,) + (True,) * (properties - 1), within_limits=True),
                model_turns=4 + 2 * delegates,
                retries=0,
                usage=Usage(input_tokens=14_000, cached_input_tokens=4000, output_tokens=122),
            )
        )
        at = ended + 1000
    if cell.replay_slots:
        observations += background(cell, begun - 500, at + 2000, len(observations))
    return observations, at


def background(cell: plan.PlannedCell, start: int, end: int, first: int) -> list[RequestRecord]:
    """The replay the seats after the live one keep up in a live cell at class size: a turn of
    each of their conversations every 10 s from `start` until a turn has ended after `end`."""
    per = plan.load_suite().conversations[cell.identity.shape]
    rows: list[RequestRecord] = []
    for slot in range(cell.replay_slots):
        sent, turn = start + 50 * slot, 0
        while not rows or rows[-1].conversation != slot or rows[-1].ended_ms < end:
            rows.append(
                RequestRecord(
                    request=first + len(rows),
                    cell=cell.identity.cell,
                    student=1 + slot // per,
                    conversation=slot,
                    turn=turn,
                    traffic=Traffic.BACKGROUND,
                    trial=None,
                    route=Route.MESSAGES,
                    eligible_ms=sent,
                    sent_ms=sent,
                    first_output_ms=sent + 1500,
                    first_answer_ms=sent + 1500,
                    last_output_ms=sent + 2500,
                    ended_ms=sent + 2510,
                    outcome=RequestOutcome.COMPLETED,
                    finish_reason=None,
                    usage=Usage(input_tokens=20_000, cached_input_tokens=20_000 if turn else 0, output_tokens=21),
                )
            )
            sent, turn = sent + 10_000, turn + 1
    return rows


CANDIDATE = Variant()


def build(directory: Path, variant: Variant = CANDIDATE) -> Path:
    """Writes the bundle of a variant: every class-size repetition and every live cell of the plan."""
    suite = plan.load_suite()
    planned = plan.expand(suite, Profile.QUALIFY, 2, input_seed=variant.input_seed)
    workload = replace(
        planned.workload,
        objectives=replace(planned.workload.objectives, **variant.objectives),
        sampling=replace(planned.workload.sampling, **variant.sampling),
    )
    manifest = Manifest(
        workload=workload,
        requested=settings(variant, observed=False),
        run=Run(
            ordinal=0,
            source_commit="a" * 40,
            lock_digest="sha256:" + "1a" * 32,
            harness_image_digest="sha256:" + "2b" * 32,
            driver_image_digest="sha256:" + "3c" * 32,
            driver_cpus=8,
            driver_memory_mib=16_384,
            path_category=PathCategory.IN_REGION,
        ),
        observed=settings(variant, observed=True),
    )
    hosts = tuple(
        HostLaunch(host=host, in_service_ms=0, launch_fingerprint=variant.launched, launch_whole_window=True)
        for host in range(variant.host_count)
    )
    attempts = []
    clock = START
    executions = 0
    for cell in planned.cells:
        identity = cell.identity
        if identity.cell in variant.drop_cells:
            continue
        offset = clock - START
        if Role.CLASS_FLOOR in cell.roles:
            rows = [shift(row, offset) for row in replay(cell, variant)]
            observations: list[Document] = [
                *rows,
                *(shift(one, offset) for one in samples(identity.cell, variant.host_count)),
            ]
            window = (START + offset, ADMITTED + offset, END + offset)
        elif identity.phase in (Phase.LIVE_LOW_LOAD, Phase.LIVE_CLASS_LOAD):
            observations, last = live(cell, clock + 1000, executions, variant, workload)
            executions += len(cell.trials)
            rows = [one for one in observations if isinstance(one, RequestRecord)]
            window = (clock, last, last + 20_000)
        else:
            continue
        record = CellRecord(
            cell=identity.cell,
            started_ms=window[0],
            admissions_ended_ms=window[1],
            ended_ms=window[2],
            status=CellStatus.COMPLETE,
            invalid_flags=(),
            conversations_planned=identity.agent_slots,
            conversations_started=identity.agent_slots,
            conversations_completed=identity.agent_slots,
            conversations_abandoned=0,
            hosts_observed=variant.host_count,
            driver_lag_p95_ms=5.0,
        )
        served = tuple(replace(host, in_service_ms=window[2] - window[0]) for host in hosts)
        attempts.append(report.Evidence(record, tuple(observations), tally(rows), served, variant.requested_launch))
        clock = window[2] + 60_000
    report.write_evidence(directory, manifest, attempts)
    return directory


def shift[D: (RequestRecord, HostSample)](one: D, offset: int) -> D:
    """A record of a class-size cell, moved to the cell's place in the run."""
    if isinstance(one, HostSample):
        return replace(one, at_ms=one.at_ms + offset)
    times = ("eligible_ms", "sent_ms", "first_output_ms", "first_answer_ms", "last_output_ms", "ended_ms")
    return replace(one, **{name: getattr(one, name) + offset for name in times if getattr(one, name) is not None})


def scorecard(directory: Path) -> report.Derived:
    return report.regenerate(report.load(directory), usd_per_host_hour=PRICE, rate_source=SOURCE)


def compared(reference: Path, candidate: Path) -> dict:
    return report.compare(report.load(reference), report.load(candidate), usd_per_host_hour=PRICE, rate_source=SOURCE)


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("reference") / "bundle", REFERENCE)


def reseal(directory: Path) -> None:
    """Seals each checkpoint again over its file as it now is: an edit that is not caught by the seal."""
    lines = []
    for line in (directory / report.CHECKPOINTS).read_text().splitlines():
        checkpoint = contracts.loads(line, Checkpoint)
        data = (directory / f"observations/attempt-{checkpoint.attempt}.jsonl").read_bytes()
        count, seal = data.count(b"\n"), "sha256:" + hashlib.sha256(data).hexdigest()
        lines.append(contracts.dumps(replace(checkpoint, observations=count, observations_digest=seal)) + "\n")
    (directory / report.CHECKPOINTS).write_text("".join(lines))


def manifest_edited(directory: Path, change: Callable[[dict], object]) -> Path:
    """The bundle with its manifest changed in place, as JSON."""
    path = directory / report.MANIFEST
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))
    return directory


def edited(tmp_path: Path, name: str, change: Callable[[str], str]) -> Path:
    """A copy of the saved bundle with one file changed, its derived files left out."""
    copy = tmp_path / "edited"
    shutil.copytree(FIXTURE, copy, ignore=shutil.ignore_patterns(*report.DERIVED))
    path = copy / name
    path.write_text(change(path.read_text()))
    reseal(copy)
    return copy


# --- The saved bundle --------------------------------------------------------------------------


def test_a_saved_bundle_reproduces_its_scorecard_and_digest_offline():
    derived = scorecard(FIXTURE)
    assert derived == scorecard(FIXTURE)
    assert derived.summary == (FIXTURE / report.SUMMARY).read_text()
    assert derived.report == (FIXTURE / report.REPORT).read_text()
    assert derived.digest == (FIXTURE / report.DIGEST).read_text().strip() == digest(derived.summary)
    assert derived.digest in derived.report
    rows = json.loads(derived.summary)["capacity"]
    hands_on, several = rows["hands_on"]["limiting_row"], rows["several_agents"]["limiting_row"]
    # Hands-on passes, nearest its time to first token; several-agents misses its p10 speed.
    assert (rows["hands_on"]["verdict"], hands_on["floor"], hands_on["missed"]) == ("pass", "ttft_p95", False)
    assert (rows["several_agents"]["verdict"], several["floor"], several["missed"]) == ("known_miss", "speed_p10", True)
    assert several["repetition"] == 1 and rows["several_agents"]["missed_repetitions"] == [1]


def test_the_scorecard_shows_capacity_and_quality_by_shape_harness_and_load_and_both_conditions():
    summary = json.loads(scorecard(FIXTURE).summary)
    conditions = {row["setting"]: row for row in summary["conditions"]}
    assert {key: conditions["host_count"][key] for key in ("requested", "observed", "status")} == {
        "requested": 2,
        "observed": 2,
        "status": "match",
    }
    # What no evidence showed is not observed, and never filled from the request.
    assert (conditions["step_budget_tokens"]["observed"], conditions["step_budget_tokens"]["status"]) == (
        None,
        "not_observed",
    )
    repetition = summary["capacity"]["hands_on"]["repetitions"][0]
    assert (repetition["later_turns"], repetition["later_turns_min"], repetition["failed"], repetition["attempts"]) == (
        164,
        100,
        0,
        166,
    )
    assert repetition["failed_observed_floor"] and repetition["failed_wilson_high"] > 0
    assert [host["output_tokens_per_s"] for host in repetition["engine_output_tokens_per_s_by_host"]] == [4.0, 4.0]
    harnesses = {(row["shape"], row["load"], row["harness"]) for row in summary["capacity_by_harness"]}
    # A harness that serves both shapes has a row in each.
    assert {("hands_on", "class_load", "claude_code"), ("several_agents", "class_load", "claude_code")} <= harnesses
    assert {("hands_on", "low_load", "codex"), ("hands_on", "class_load", "opencode")} <= harnesses
    # Each class-load live cell shows the other seat's replay behind its trials.
    loaded = [row for rows in summary["class_load_evidence"].values() for row in rows]
    assert loaded and all((row["status"], row["background_seats"]) == ("met", 1) for row in loaded)
    cells = {
        (load, cell["family"], cell["harness"]): cell
        for load, one in summary["quality"].items()
        for cell in one["cells"]
    }
    assert cells["low_load", "debugging", "codex"]["floor_outcome"] == "fail"
    assert (
        cells["class_load", "optimization", "opencode"]["successes"],
        cells["class_load", "optimization", "opencode"]["attempts"],
    ) == (8, 9)
    assert set(summary["quality"]["class_load"]["shapes"]) == {"hands_on", "several_agents"}


def test_a_line_missing_from_the_evidence_is_refused_or_left_unaccounted(tmp_path: Path):
    name = "observations/attempt-0.jsonl"
    lines = (FIXTURE / name).read_text().splitlines(keepends=True)
    damaged = tmp_path / "damaged"
    shutil.copytree(FIXTURE, damaged)
    (damaged / name).write_text("".join(lines[1:]))
    with pytest.raises(ReportError) as refused:
        report.load(damaged)
    assert refused.value.reason is Refusal.OBSERVATIONS_DAMAGED
    # Sealed again without the row, the scheduler's own tally still counts the send it lost.
    rows = edited(tmp_path, name, lambda text: "".join(text.splitlines(keepends=True)[1:]))
    first = json.loads(scorecard(rows).summary)["capacity"]["hands_on"]["repetitions"][0]
    assert first["verdict"] == "invalid" and "attempts_unaccounted" in first["reasons"]


def test_each_host_keeps_its_own_engine_throughput_beside_the_pool_total(tmp_path: Path):
    def skew(text: str) -> str:
        rows = [json.loads(line) for line in text.splitlines()]
        for row in rows:
            if row.get("metric") == "output_tokens_total":
                row["value"] *= 2 if row["host"] == 0 else 0
        return "".join(json.dumps(row) + "\n" for row in rows)

    copy = edited(tmp_path, "observations/attempt-0.jsonl", skew)
    first = json.loads(scorecard(copy).summary)["capacity"]["hands_on"]["repetitions"][0]
    assert first["engine_output_tokens_per_s"] == 8.0
    assert [host["output_tokens_per_s"] for host in first["engine_output_tokens_per_s_by_host"]] == [8.0, 0.0]


def test_every_measured_sweep_load_is_in_the_scorecard(tmp_path: Path):
    bundle = report.load(FIXTURE)
    cell = next(
        one
        for one in bundle.plan.cells
        if Role.DIAGNOSTIC in one.roles and one.identity.shape.value == "hands_on" and one.identity.agent_slots == 4
    )
    first = bundle.attempts[0].checkpoint
    record = replace(first.record, cell=cell.identity.cell, conversations_planned=4, conversations_started=4)
    record = replace(record, conversations_completed=4)

    def sweep(name: str, ttft_ms: int) -> list[dict]:
        rows = replay(cell, Variant(hands_on_ttft_ms=ttft_ms))
        evidence = report.Evidence(
            record,
            (*rows, *samples(cell.identity.cell, 2)),
            tally(rows),
            first.hosts,
            first.requested_launch_fingerprint,
        )
        report.write_evidence(tmp_path / name, bundle.manifest, [evidence])
        return json.loads(scorecard(tmp_path / name).summary)["capacity_sweep"]

    fast, slow = sweep("fast", 1000), sweep("slow", 6000)
    assert [(row["shape"], row["agent_slots"], row["repetition"]) for row in slow] == [("hands_on", 4, 0)]
    assert (fast[0]["verdict"], slow[0]["verdict"]) == ("pass", "measured_miss")
    assert slow[0]["ttft_p95_ms"] > 5000 > fast[0]["ttft_p95_ms"]


def test_a_zero_ceiling_is_a_miss_with_headroom_not_a_division():
    reading = report.read(report.load(FIXTURE), usd_per_host_hour=PRICE)
    objectives = replace(reading.manifest.workload.objectives, ttft_p95_ms_max=0.0)
    row = report.limiting_row(reading.capacity.shapes[contracts.Shape.HANDS_ON], objectives)
    assert row is not None and row["floor"] == "ttft_p95" and row["headroom"] < 0


# --- The recommendation ------------------------------------------------------------------------


def test_only_a_tested_host_count_meeting_capacity_and_both_quality_checks_is_recommended(reference: Path):
    alone = json.loads(scorecard(FIXTURE).summary)["recommendation"]["shapes"]
    # Read alone, a run has no relative check: nothing is recommended.
    assert alone["hands_on"]["status"] == "unknown"
    assert alone["hands_on"]["tested"][0]["checks"]["relative_quality"] == "unknown"
    comparison = compared(reference, FIXTURE)
    decision = next(one for one in comparison["decisions"] if (one["load"], one["shape"]) == ("class_load", "hands_on"))
    assert decision["outcome"] == "pass" and decision["lower_points"] >= decision["margin_points"]
    shapes = comparison["recommendation"]["shapes"]
    # One host missed capacity; two met it and both quality checks at class load, whatever the
    # low-load debugging cell on Codex missed.
    assert (shapes["hands_on"]["status"], shapes["hands_on"]["host_count"]) == ("recommended", 2)
    assert [one["status"] for one in shapes["hands_on"]["tested"]] == ["missed", "met"]
    assert (shapes["several_agents"]["status"], shapes["several_agents"]["host_count"]) == ("not_met", None)
    assert comparison["recommendation"]["common"] is None


def test_the_reference_is_a_baseline_and_an_inconclusive_pair_recommends_nothing():
    comparison = compared(FIXTURE, FIXTURE)
    decision = next(one for one in comparison["decisions"] if (one["load"], one["shape"]) == ("class_load", "hands_on"))
    assert decision["outcome"] == "inconclusive"
    hands_on = comparison["recommendation"]["shapes"]["hands_on"]
    assert (hands_on["status"], hands_on["host_count"]) == ("unknown", None)
    reference = hands_on["tested"][0]["checks"]
    assert (reference["relative_basis"], reference["relative_quality"]) == ("baseline", "unknown")


def test_a_host_count_the_evidence_does_not_show_serving_is_not_recommended(tmp_path: Path):
    # Both manifests say three hosts; every attempt was served by two.
    runs = build(tmp_path / "reference", replace(REFERENCE, host_count=2)), build(tmp_path / "candidate")
    for run in runs:
        path = run / report.MANIFEST
        manifest = json.loads(path.read_text())
        manifest["requested"]["host_count"] = manifest["observed"]["host_count"] = 3
        path.write_text(json.dumps(manifest))
    hands_on = compared(*runs)["recommendation"]["shapes"]["hands_on"]
    assert hands_on["host_count"] is None
    assert [one["checks"]["host_count"] for one in hands_on["tested"]] == ["unknown", "unknown"]


type Live = tuple[tuple[RequestRecord, ...], tuple[TrialRecord, ...], CellRecord]


def class_load_changed(source: Path, target: Path, change: Callable[[Live], Live], *, recount: bool = True) -> Path:
    """A copy of a bundle with each live cell at class load changed: its requests, its trials and
    its record. The scheduler's tally is counted again over the rows unless `recount` is false."""
    bundle = report.load(source)
    evidence = []
    for attempt in bundle.attempts:
        rows, trials, record, sent = attempt.requests, attempt.trials, attempt.record, attempt.checkpoint.tally
        if bundle.manifest.workload.cells[record.cell].phase is Phase.LIVE_CLASS_LOAD:
            rows, trials, record = change((rows, trials, record))
            sent = tally(rows) if recount else sent
        checkpoint = attempt.checkpoint
        observations = (*rows, *trials, *attempt.samples)
        evidence.append(
            report.Evidence(record, observations, sent, checkpoint.hosts, checkpoint.requested_launch_fingerprint)
        )
    report.write_evidence(target, bundle.manifest, evidence)
    return target


def without(traffic: Traffic) -> Callable[[Live], Live]:
    return lambda live: (tuple(row for row in live[0] if row.traffic is not traffic), live[1], live[2])


def idle_during_trials(live: Live) -> Live:
    """The other seat's replay: one turn as the first trial starts and the next once the last one
    ended, every send of it accounted for."""
    rows, trials, record = live
    first, last = min(trial.started_ms for trial in trials), max(trial.ended_ms for trial in trials)
    template = next(row for row in rows if row.traffic is Traffic.BACKGROUND)
    before = shift(template, first - 500 - template.eligible_ms)
    after = shift(replace(before, request=before.request + 1, turn=1), last - first + 1000)
    return (*(row for row in rows if row.traffic is Traffic.LIVE), before, after), trials, record


def one_seat(live: Live) -> Live:
    """The live task and the replay both on the second seat of the class."""
    rows, trials, record = live
    return tuple(replace(row, student=1) for row in rows), tuple(replace(one, student=1) for one in trials), record


def lagging(live: Live) -> Live:
    return live[0], live[1], replace(live[2], driver_lag_p95_ms=10_000.0)


@pytest.mark.parametrize(
    ("change", "recount", "gap"),
    [
        # Without the other seat every send is still accounted for.
        (without(Traffic.BACKGROUND), True, "background_missing"),
        # Without the live rows the scheduler still counts the sends they were.
        (without(Traffic.LIVE), False, "sends_unaccounted"),
        (idle_during_trials, True, "background_idle"),
        (one_seat, True, "seats_unplanned"),
        # Past the protocol's 250 ms tolerance, what was sent is not the schedule.
        (lagging, True, "driver_lag"),
    ],
)
def test_class_load_quality_needs_the_class_load_it_was_measured_under(
    reference: Path, tmp_path: Path, change: Callable[[Live], Live], recount: bool, gap: str
):
    candidate = class_load_changed(FIXTURE, tmp_path / "candidate", change, recount=recount)
    hands_on = compared(reference, candidate)["recommendation"]["shapes"]["hands_on"]
    assert hands_on["host_count"] is None and hands_on["tested"][1]["checks"]["class_load_evidence"] == "unknown"
    gaps = {row["gap"] for row in json.loads(scorecard(candidate).summary)["class_load_evidence"]["hands_on"]}
    assert gaps == {gap}


def requests_changed(
    source: Path, target: Path, change: Callable[[plan.PlannedCell, tuple[RequestRecord, ...]], Sequence[RequestRecord]]
) -> Path:
    """A copy of a bundle with the requests of each attempt changed, its sends counted again."""
    bundle = report.load(source)
    evidence = []
    for attempt in bundle.attempts:
        rows = tuple(change(bundle.plan.cells[attempt.record.cell], attempt.requests))
        checkpoint = attempt.checkpoint
        observations = (*rows, *attempt.trials, *attempt.samples)
        evidence.append(
            report.Evidence(
                attempt.record, observations, tally(rows), checkpoint.hosts, checkpoint.requested_launch_fingerprint
            )
        )
    report.write_evidence(target, bundle.manifest, evidence)
    return target


def test_class_capacity_is_measured_from_each_seat_of_the_class(reference: Path, tmp_path: Path):
    def one_student(cell: plan.PlannedCell, rows: tuple[RequestRecord, ...]) -> Sequence[RequestRecord]:
        floor = Role.CLASS_FLOOR in cell.roles and cell.identity.shape.value == "hands_on"
        return tuple(replace(row, student=0) for row in rows) if floor else rows

    candidate = requests_changed(FIXTURE, tmp_path / "candidate", one_student)
    assert json.loads(scorecard(candidate).summary)["capacity"]["hands_on"]["seats_shown"] == [False] * 3
    hands_on = compared(reference, candidate)["recommendation"]["shapes"]["hands_on"]
    assert hands_on["host_count"] is None and hands_on["tested"][1]["checks"]["capacity"] == "unknown"


def test_seats_measured_one_after_another_are_no_class(reference: Path, tmp_path: Path):
    def staggered(cell: plan.PlannedCell, rows: tuple[RequestRecord, ...]) -> Sequence[RequestRecord]:
        if Role.CLASS_FLOOR not in cell.roles or cell.identity.shape.value != "hands_on":
            return rows
        start = min(row.eligible_ms for row in rows)
        # Each student's whole replay before the next student's first send.
        return tuple(shift(row, start + row.student * 420_000 + row.turn * 8000 - row.eligible_ms) for row in rows)

    source = build(tmp_path / "source", replace(CANDIDATE, hands_on_ttft_ms=500))
    assert compared(reference, source)["recommendation"]["shapes"]["hands_on"]["host_count"] == 2
    candidate = requests_changed(source, tmp_path / "candidate", staggered)
    assert json.loads(scorecard(candidate).summary)["capacity"]["hands_on"]["seats_shown"] == [False] * 3
    hands_on = compared(reference, candidate)["recommendation"]["shapes"]["hands_on"]
    assert hands_on["host_count"] is None and hands_on["tested"][1]["checks"]["capacity"] == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        # Every seat idle for the first 270 s of admissions, its replay moved later.
        lambda rows: tuple(shift(row, 270_000) for row in rows),
        # Every seat idle for the last 300 s of admissions, its replay stopped early.
        lambda rows: tuple(row for row in rows if row.sent_ms <= ADMITTED - 300_000),
    ],
    ids=["late", "early"],
)
def test_a_class_replay_idle_through_part_of_its_admissions_cannot_qualify(
    reference: Path, tmp_path: Path, change: Callable[[tuple[RequestRecord, ...]], Sequence[RequestRecord]]
):
    def changed(cell: plan.PlannedCell, rows: tuple[RequestRecord, ...]) -> Sequence[RequestRecord]:
        floor = Role.CLASS_FLOOR in cell.roles and cell.identity.shape.value == "hands_on"
        return change(rows) if floor else rows

    assert compared(reference, FIXTURE)["recommendation"]["shapes"]["hands_on"]["host_count"] == 2
    candidate = requests_changed(FIXTURE, tmp_path / "candidate", changed)
    summary = json.loads(scorecard(candidate).summary)
    shape = summary["capacity"]["hands_on"]
    assert shape["seats_shown"] == [False] * 3
    sweep = [row for row in summary["capacity_sweep"] if row["class_floor"] and row["shape"] == "hands_on"]
    assert shape["verdict"] != "pass" and all(row["verdict"] != "pass" for row in (*shape["repetitions"], *sweep))
    hands_on = compared(reference, candidate)["recommendation"]["shapes"]["hands_on"]
    assert hands_on["host_count"] is None and hands_on["tested"][1]["checks"]["capacity"] == "unknown"


def test_a_class_replay_that_stopped_early_shows_its_rows_but_no_capacity_pass(tmp_path: Path):
    """Each seat stopped 600 s into its admissions, every repetition still over its sample
    minimums and its floors: the rows stay shown, the shape and its class-size rows are no pass."""

    def stopped(cell: plan.PlannedCell, rows: tuple[RequestRecord, ...]) -> Sequence[RequestRecord]:
        if Role.CLASS_FLOOR not in cell.roles or cell.identity.shape.value != "hands_on":
            return rows
        cutoff = min(row.sent_ms for row in rows) + 600_000
        return tuple(row for row in rows if row.sent_ms < cutoff)

    derived = scorecard(requests_changed(FIXTURE, tmp_path / "stopped", stopped))
    summary = json.loads(derived.summary)
    shape = summary["capacity"]["hands_on"]
    sweep = [row for row in summary["capacity_sweep"] if row["class_floor"] and row["shape"] == "hands_on"]
    assert shape["seats_shown"] == [False] * 3 and shape["verdict"] == "inconclusive" and len(sweep) == 3
    for row in (*shape["repetitions"], *sweep):
        assert row["verdict"] == "insufficient_evidence" and row["reasons"] == [report.SEATS_UNSHOWN]
        assert row["later_turns"] >= row["later_turns_min"] and row["ttft_p95_ms"] is not None
    # The Markdown headline shows the same verdict as the summary.
    assert f"**hands_on** at {shape['agent_slots']} agent slots: inconclusive;" in derived.report


def test_a_trial_run_again_after_the_next_one_ended_overlaps_nothing(tmp_path: Path):
    """The first run of a trial interrupted, and the trial run again, with a request of its own,
    once the next trial ended: each run is its own span on the seat."""

    def retried(live: Live) -> Live:
        rows, trials, record = live
        first, second, *rest = trials
        interrupted = replace(
            first, outcome=TrialOutcome.INVALID, failure=None, invalid=contracts.InvalidReason.INTERRUPTED, grade=None
        )
        start = second.ended_ms + 300
        execution = max(one.execution for one in trials) + 1
        retry = replace(first, execution=execution, replaces=first.execution, started_ms=start, ended_ms=start + 400)
        sent = next(row for row in rows if row.trial == first.trial)
        times = dict(eligible_ms=start + 50, sent_ms=start + 50, first_output_ms=start + 100)
        times |= dict(first_answer_ms=start + 100, last_output_ms=start + 340, ended_ms=start + 350)
        again = replace(sent, request=max(row.request for row in rows) + 1, **times)
        return (*rows, again), (interrupted, second, *rest, retry), record

    candidate = class_load_changed(FIXTURE, tmp_path / "candidate", retried)
    evidence = json.loads(scorecard(candidate).summary)["class_load_evidence"]["hands_on"]
    assert evidence and all((row["status"], row["gap"]) == ("met", None) for row in evidence)


def test_a_counted_retry_needs_requests_of_its_own(reference: Path, tmp_path: Path):
    """The interrupted run sent every request of the trial; the run graded in its place, later,
    sent none: the interrupted run's requests do not show the load the grade was made under."""

    def retry_without_send(live: Live) -> Live:
        rows, trials, record = live
        first, *rest = trials
        ended = max(row.ended_ms for row in rows if row.trial == first.trial)
        interrupted = replace(
            first,
            ended_ms=ended,
            outcome=TrialOutcome.INVALID,
            failure=None,
            invalid=contracts.InvalidReason.INTERRUPTED,
            grade=None,
        )
        execution = max(one.execution for one in trials) + 1
        retry = replace(first, started_ms=ended + 1000, execution=execution, replaces=first.execution)
        return rows, (interrupted, retry, *rest), record

    assert compared(reference, FIXTURE)["recommendation"]["shapes"]["hands_on"]["host_count"] == 2
    candidate = class_load_changed(FIXTURE, tmp_path / "candidate", retry_without_send)
    gaps = {row["gap"] for row in json.loads(scorecard(candidate).summary)["class_load_evidence"]["hands_on"]}
    assert "trial_unsent" in gaps
    comparison = compared(reference, candidate)
    paired = next(one for one in comparison["decisions"] if (one["load"], one["shape"]) == ("class_load", "hands_on"))
    assert (paired["outcome"], paired["reason"]) == ("inconclusive", report.LOAD_UNSHOWN)
    assert comparison["recommendation"]["shapes"]["hands_on"]["host_count"] is None


@pytest.mark.parametrize(("sent_before", "gap"), [(None, "trial_unsent"), (1, "sent_outside_run")])
def test_requests_sent_before_their_trial_started_show_no_load(
    reference: Path, tmp_path: Path, sent_before: int | None, gap: str
):
    """The first trial recorded as starting just after its first request (`sent_before`), or after
    all of them had ended (`None`) with the replay kept from that start on: what was sent before
    the trial started is no evidence of the load it was graded under."""

    def started_late(live: Live) -> Live:
        rows, trials, record = live
        first, *rest = trials
        own = [row for row in rows if row.trial == first.trial]
        if sent_before is not None:
            start = sorted(row.sent_ms for row in own)[sent_before - 1] + 1
            return rows, (replace(first, started_ms=start), *rest), record
        ended = max(row.ended_ms for row in own)
        start = min(row.sent_ms for row in rows if row.traffic is Traffic.BACKGROUND and row.sent_ms > ended)
        kept = tuple(row for row in rows if row.traffic is Traffic.LIVE or row.sent_ms >= start)
        return kept, (replace(first, started_ms=start), *rest), record

    candidate = class_load_changed(FIXTURE, tmp_path / "candidate", started_late)
    gaps = {row["gap"] for row in json.loads(scorecard(candidate).summary)["class_load_evidence"]["hands_on"]}
    assert gap in gaps
    assert compared(reference, candidate)["recommendation"]["shapes"]["hands_on"]["host_count"] is None


@pytest.mark.parametrize(("phase", "load"), [(Phase.LIVE_LOW_LOAD, "low_load"), (Phase.LIVE_CLASS_LOAD, "class_load")])
def test_two_live_trials_at_once_on_the_live_seat_do_not_show_the_load(tmp_path: Path, phase: Phase, load: str):
    failure = ("low_load", "optimization.t01.i01", "claude_code", 2)
    reference = build(tmp_path / "reference", replace(REFERENCE, fails=REFERENCE.fails | {failure}))

    def hands_on(comparison: dict) -> dict:
        return next(one for one in comparison["decisions"] if (one["load"], one["shape"]) == (load, "hands_on"))

    assert hands_on(compared(reference, FIXTURE))["outcome"] == "pass"
    bundle = report.load(FIXTURE)
    evidence = []
    for attempt in bundle.attempts:
        rows, trials = attempt.requests, attempt.trials
        if bundle.plan.cells[attempt.record.cell].identity.phase is phase:
            # The second trial, and its requests, started with the first.
            first, second, *rest = trials
            delta = first.started_ms - second.started_ms
            moved = replace(second, started_ms=first.started_ms, ended_ms=second.ended_ms + delta)
            trials = (first, moved, *rest)
            rows = tuple(shift(row, delta) if row.trial == second.trial else row for row in rows)
        point = attempt.checkpoint
        observations = (*rows, *trials, *attempt.samples)
        evidence.append(
            report.Evidence(attempt.record, observations, point.tally, point.hosts, point.requested_launch_fingerprint)
        )
    candidate = tmp_path / "candidate"
    report.write_evidence(candidate, bundle.manifest, evidence)
    comparison = compared(reference, candidate)
    assert (hands_on(comparison)["outcome"], hands_on(comparison)["reason"]) == ("inconclusive", report.LOAD_UNSHOWN)
    gaps = {row["gap"] for row in json.loads(scorecard(candidate).summary)["class_load_evidence"]["hands_on"]}
    # The recommendation is read at class load: a low-load pair withheld leaves it standing.
    class_load = load == "class_load"
    assert ("trials_overlap" in gaps) is class_load
    assert comparison["recommendation"]["shapes"]["hands_on"]["host_count"] == (None if class_load else 2)


def test_several_agents_class_load_needs_each_seats_conversations_side_by_side(reference: Path, tmp_path: Path):
    passing = build(tmp_path / "passing", replace(CANDIDATE, slow_several_agents_repetition=None))
    assert compared(reference, passing)["recommendation"]["shapes"]["several_agents"]["host_count"] == 2

    def serial(live: Live) -> Live:
        rows, trials, record = live
        return tuple(row for row in rows if row.traffic is Traffic.LIVE or row.conversation == 0), trials, record

    candidate = class_load_changed(passing, tmp_path / "serial", serial)
    several = compared(reference, candidate)["recommendation"]["shapes"]["several_agents"]
    assert several["host_count"] is None and several["tested"][1]["checks"]["class_load_evidence"] == "unknown"
    gaps = {row["gap"] for row in json.loads(scorecard(candidate).summary)["class_load_evidence"]["several_agents"]}
    assert gaps == {"background_serial"}


@pytest.mark.parametrize(("early_ms", "host_count"), [(0, 2), (30_000, None)])
def test_a_trial_ending_in_its_seats_scheduled_pause_ran_loaded(
    reference: Path, tmp_path: Path, early_ms: int, host_count: int | None
):
    """The replay stops sending once told the trial has finished; its seat may be waiting on a
    tool then. A replay that stopped longer before than its schedule pauses left the trial idle."""

    quiet_at_the_end = []

    def stopped(live: Live) -> Live:
        rows, trials, record = live
        ended = max(trial.ended_ms for trial in trials)
        kept = tuple(row for row in rows if row.traffic is Traffic.LIVE or row.sent_ms <= ended - early_ms)
        quiet_at_the_end.append(max(row.ended_ms for row in kept if row.traffic is Traffic.BACKGROUND) < ended)
        return kept, trials, record

    candidate = class_load_changed(FIXTURE, tmp_path / "candidate", stopped)
    assert any(quiet_at_the_end)
    assert compared(reference, candidate)["recommendation"]["shapes"]["hands_on"]["host_count"] == host_count


def test_a_low_load_pair_is_decided_only_when_each_live_task_ran_alone(tmp_path: Path):
    failure = ("low_load", "optimization.t01.i01", "claude_code", 2)
    reference = build(tmp_path / "reference", replace(REFERENCE, fails=REFERENCE.fails | {failure}))

    def low_load(comparison: dict) -> dict:
        return next(one for one in comparison["decisions"] if (one["load"], one["shape"]) == ("low_load", "hands_on"))

    assert low_load(compared(reference, FIXTURE))["outcome"] == "pass"

    def loaded(cell: plan.PlannedCell, rows: tuple[RequestRecord, ...]) -> Sequence[RequestRecord]:
        if cell.identity.phase is not Phase.LIVE_LOW_LOAD:
            return rows
        first = max(row.request for row in rows) + 1
        beside = (
            replace(row, request=first + n, student=1, trial=None, traffic=Traffic.BACKGROUND)
            for n, row in enumerate(rows)
        )
        return (*rows, *beside)

    candidate = requests_changed(FIXTURE, tmp_path / "candidate", loaded)
    decision = low_load(compared(reference, candidate))
    assert (decision["outcome"], decision["reason"], decision["load_unshown"]) == (
        "inconclusive",
        report.LOAD_UNSHOWN,
        ["candidate"],
    )


def test_an_unloaded_reference_qualifies_no_candidate(tmp_path: Path):
    reference = class_load_changed(
        build(tmp_path / "original", REFERENCE), tmp_path / "reference", without(Traffic.BACKGROUND)
    )
    comparison = compared(reference, FIXTURE)
    decision = next(one for one in comparison["decisions"] if (one["load"], one["shape"]) == ("class_load", "hands_on"))
    assert (decision["outcome"], decision["reason"], decision["load_unshown"]) == (
        "inconclusive",
        report.LOAD_UNSHOWN,
        ["reference"],
    )
    assert comparison["recommendation"]["shapes"]["hands_on"]["host_count"] is None


# --- Comparing ---------------------------------------------------------------------------------


def test_compare_prints_the_treatment_diff_before_the_scorecard(reference: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["compare", str(reference), str(FIXTURE)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("treatment diff (hosts):\n  host_count [hosts]: 1 -> 2\n")
    assert printed.index("treatment diff") < printed.index("# Comparison scorecard")
    assert "## Task time and completions across tokenizers" in printed


def test_the_json_comparison_is_one_document_with_its_diff_first(reference: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["compare", str(reference), str(FIXTURE), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert next(iter(printed)) == "diff" and printed["diff"]["label"] == "hosts"


def test_a_changed_protocol_prints_the_diff_and_is_then_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    candidate = tmp_path / "candidate"
    shutil.copytree(FIXTURE, candidate)

    def change(manifest: dict) -> None:
        manifest["workload"]["protocol_digest"] = "sha256:" + "d" * 64
        manifest["requested"]["host_count"] = manifest["observed"]["host_count"] = 1

    manifest_edited(candidate, change)
    assert cli.main(["compare", str(FIXTURE), str(candidate)]) == 2
    printed = capsys.readouterr()
    assert printed.out.startswith("treatment diff (hosts):\n  host_count [hosts]: 2 -> 1\n")
    assert printed.err.startswith("refused: incompatible-workload")


def test_the_script_reads_and_compares_saved_bundles_offline_from_any_folder(tmp_path: Path):
    """`scripts/evaluate.sh`, in a shell with no import path and no AWS credentials."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith("AWS_") and name != "PYTHONPATH"}
    absent = {name: str(tmp_path / "absent") for name in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE")}
    copy = tmp_path / "bundle"
    shutil.copytree(FIXTURE, copy, ignore=shutil.ignore_patterns(*report.DERIVED))

    def script(*arguments: str) -> subprocess.CompletedProcess[str]:
        command = [str(FIXTURE.parents[3] / "scripts" / "evaluate.sh"), *arguments]
        return subprocess.run(command, cwd=tmp_path, env=kept | absent, capture_output=True, text=True, check=False)

    regenerated = script("report", str(copy))
    assert regenerated.returncode == 0, regenerated.stderr
    assert all((copy / name).read_text() == (FIXTURE / name).read_text() for name in report.DERIVED)
    repeat = script("compare", str(FIXTURE), str(copy))
    assert repeat.returncode == 0, repeat.stderr
    assert repeat.stdout.startswith("treatment diff (repeat):\n  no requested setting differs\n")


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        (Variant(objectives={"ttft_p95_ms_max": 6000.0}), Refusal.CHANGED_FLOOR),
        (Variant(sampling={"ttft_samples_min": 200}), Refusal.CHANGED_SAMPLE_BUDGET),
        (Variant(input_seed=7), Refusal.INCOMPATIBLE_WORKLOAD),
        # A flags-only change with no launch fingerprint from its hosts, or none that changed.
        (
            Variant(step_budget_tokens=4096, requested_launch=launch("step-4096"), launched=None),
            Refusal.UNVERIFIED_TREATMENT,
        ),
        (Variant(step_budget_tokens=4096), Refusal.UNVERIFIED_TREATMENT),
        # Launched arguments that differ with no setting declared changed.
        (Variant(launched=launch("other"), requested_launch=launch("other")), Refusal.UNDECLARED_TREATMENT),
    ],
)
def test_compare_refuses_another_workload_floor_budget_or_an_unproven_treatment(
    tmp_path: Path, variant: Variant, reason: Refusal, capsys: pytest.CaptureFixture[str]
):
    candidate = build(tmp_path / "candidate", variant)
    with pytest.raises(ReportError) as refused:
        compared(FIXTURE, candidate)
    assert refused.value.reason is reason
    assert cli.main(["compare", str(FIXTURE), str(candidate)]) == 2
    printed = capsys.readouterr()
    assert printed.out.startswith("treatment diff") and printed.err.startswith(f"refused: {reason.value}")


def test_a_flags_only_treatment_compares_once_each_host_launched_what_its_run_requested(tmp_path: Path):
    candidate = build(
        tmp_path / "candidate",
        Variant(step_budget_tokens=4096, launched=launch("step-4096"), requested_launch=launch("step-4096")),
    )
    diff = compared(FIXTURE, candidate)["diff"]
    assert (diff["label"], diff["verified"]) == ("flags_only", ["launch_settings"])
    assert [row["setting"] for row in diff["settings"]] == ["step_budget_tokens"]


@pytest.mark.parametrize("gap", ["host", "gateway"])
def test_a_flags_only_treatment_needs_every_serving_host_and_the_gateway_observed(tmp_path: Path, gap: str):
    fingerprint = launch("step-4096")
    candidate = build(
        tmp_path / "candidate", Variant(step_budget_tokens=4096, launched=fingerprint, requested_launch=fingerprint)
    )
    if gap == "host":
        # One of the two hosts that served an attempt is not in its checkpoint.
        path = candidate / report.CHECKPOINTS
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        lines[0]["hosts"].pop()
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    else:
        manifest_edited(candidate, lambda manifest: manifest["observed"].update(gateway_profile_digest=None))
    with pytest.raises(ReportError) as refused:
        compared(FIXTURE, candidate)
    assert (refused.value.reason, refused.value.lever) == (Refusal.UNVERIFIED_TREATMENT, report.Lever.LAUNCH_SETTINGS)


def test_a_declared_host_count_does_not_cover_other_hardware_observed(reference: Path, tmp_path: Path):
    runs = shutil.copytree(reference, tmp_path / "reference"), build(tmp_path / "candidate")
    for run in runs:
        manifest_edited(run, lambda manifest: manifest["requested"].update(instance_type=None, gpus_per_host=None))
    manifest_edited(
        runs[1], lambda manifest: manifest["observed"].update(instance_type="g7e.24xlarge", gpus_per_host=4)
    )
    with pytest.raises(ReportError) as refused:
        compared(*runs)
    assert (refused.value.reason, refused.value.lever) == (Refusal.UNDECLARED_TREATMENT, report.Lever.HOSTS)


def test_a_refused_json_comparison_logs_its_diff_before_the_refusal(tmp_path: Path):
    """Both streams into one log, as an operator redirects them, with stdout buffered."""
    candidate = build(tmp_path / "candidate", Variant(objectives={"ttft_p95_ms_max": 6000.0}))
    env = {name: value for name, value in os.environ.items() if name not in ("VIRTUAL_ENV", "PYTHONUNBUFFERED")}
    command = [str(FIXTURE.parents[3] / "scripts" / "evaluate.sh"), "compare", str(FIXTURE), str(candidate), "--json"]
    logged = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, check=False)
    assert logged.returncode == 2
    diff, refusal = logged.stdout.index('"diff"'), logged.stdout.index("refused: changed-floor")
    assert diff < refusal
    assert json.loads(logged.stdout[:refusal])["refused"] == "changed-floor"


def test_a_setting_observed_other_than_requested_withholds_the_attribution(
    reference: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    # Both runs requested one model; the candidate's hosts served another.
    candidate = build(tmp_path / "candidate")
    manifest_edited(candidate, lambda manifest: manifest["observed"].update(model_revision="d" * 40))
    with pytest.raises(ReportError) as refused:
        compared(reference, candidate)
    assert (refused.value.reason, refused.value.lever) == (Refusal.CONTRADICTED_SETTING, report.Lever.RELEASE)
    assert cli.main(["compare", str(reference), str(candidate)]) == 2
    assert "contradicted in candidate: model_revision [release]" in capsys.readouterr().out


LAST_HANDS_ON_REPETITION = max(
    cell.identity.cell
    for cell in plan.expand(plan.load_suite(), Profile.QUALIFY, 2).cells
    if Role.CLASS_FLOOR in cell.roles and cell.identity.shape.value == "hands_on"
)


@pytest.mark.parametrize(
    ("variant", "short"),
    [
        # Sixty later turns in every repetition, below the hundred a repetition is read on.
        (Variant(later_turns=60), [0, 1, 2]),
        # A scheduled repetition with no evidence at all.
        (Variant(drop_cells=frozenset({LAST_HANDS_ON_REPETITION})), [2]),
    ],
)
def test_insufficient_evidence_is_reported_and_no_capacity_is_projected(
    reference: Path, tmp_path: Path, variant: Variant, short: list[int]
):
    candidate = build(tmp_path / "candidate", variant)
    hands_on = json.loads(scorecard(candidate).summary)["capacity"]["hands_on"]
    assert hands_on["verdict"] == "inconclusive"
    insufficient = [rep for rep in hands_on["repetitions"] if rep["verdict"] == "insufficient_evidence"]
    assert [rep["repetition"] for rep in insufficient] == short
    assert all(rep["later_turns"] < rep["later_turns_min"] for rep in insufficient)
    # Against the reference, the two hosts are neither recommended nor scaled from what was seen.
    recommendation = compared(reference, candidate)["recommendation"]
    assert (recommendation["shapes"]["hands_on"]["status"], recommendation["shapes"]["hands_on"]["host_count"]) == (
        "unknown",
        None,
    )
    assert recommendation["shapes"]["hands_on"]["tested"][1]["checks"]["capacity"] == "unknown"
    assert recommendation["untested"] == "unknown"


# --- Replay against live tasks -----------------------------------------------------------------


def test_the_report_sets_the_replay_against_the_live_tasks_of_each_shape_and_task_time_by_tokenizer(reference: Path):
    summary = json.loads(scorecard(FIXTURE).summary)
    hands_on, several = summary["distributions"]["hands_on"], summary["distributions"]["several_agents"]
    replay, tasks = hands_on["replay"], hands_on["live"]
    assert replay["input_tokens"]["p50"] < replay["input_tokens"]["p90"]
    assert (tasks["output_tokens"]["p50"], tasks["output_tokens"]["p90"]) == (61.0, 61.0)
    assert 5000 <= replay["tool_delay_ms"]["p50"] <= 8000 and tasks["tool_delay_ms"]["p50"] > 0
    # A live task is a trial, whose turns are the model turns of its agent tree.
    assert (tasks["turns_per_task"]["p50"], several["live"]["turns_per_task"]["p50"]) == (4.0, 6.0)
    assert (tasks["fan_out"]["max"], several["live"]["fan_out"]["max"]) == (1.0, 2.0)
    assert replay["route_mix"]["messages"] == 1.0 and tasks["route_mix"]["chat_completions"] > 0
    assert replay["cached_share"]["count"] > 0
    tokenizer = summary["tokenizer"]
    assert tokenizer["tokenizer_revision"] == {"requested": "c" * 40, "observed": "c" * 40}
    assert tokenizer["loads"]["class_load"]["tasks_per_hour"] > 0
    assert tokenizer["loads"]["class_load"]["emitted_tokens_per_turn"] == 61.0
    both = compared(reference, FIXTURE)["tokenizers"]
    assert set(both) == {"reference", "candidate"}


def test_the_replay_drivers_own_rows_give_its_scheduled_tool_pauses(tmp_path: Path):
    """The driver writes no finish reason: its pauses are read from the turns' times."""
    from evaluate.tests import test_replay as driven

    bundle = report.load(FIXTURE)
    cell = next(
        one for one in bundle.plan.cells if Role.CLASS_FLOOR in one.roles and one.identity.shape.value == "hands_on"
    )
    run, _pool = driven.run(bundle.plan, cell.identity.cell, sizes=driven.SIZES, deadline_ms=driven.START + 20_000)
    assert any(row.turn > 0 for row in run.requests) and all(row.finish_reason is None for row in run.requests)
    report.write_evidence(
        tmp_path / "driven", bundle.manifest, [report.Evidence(run.record, tuple(run.requests), None, (), None)]
    )
    delays = json.loads(scorecard(tmp_path / "driven").summary)["distributions"]["hands_on"]["replay"]["tool_delay_ms"]
    low, high = driven.SIZES.tool_delay_ms
    assert delays["count"] > 0 and low <= delays["p50"] <= high


def test_a_live_turn_of_unknown_finish_gives_no_tool_wait(tmp_path: Path):
    def unknown(text: str) -> str:
        rows = [json.loads(line) for line in text.splitlines()]
        for row in rows:
            if row.get("traffic") == "live":
                row["finish_reason"] = None
        return "".join(json.dumps(row) + "\n" for row in rows)

    copy = tmp_path / "unknown"
    shutil.copytree(FIXTURE, copy, ignore=shutil.ignore_patterns(*report.DERIVED))
    for path in (copy / report.OBSERVATIONS).glob("*.jsonl"):
        path.write_text(unknown(path.read_text()))
    reseal(copy)
    sides = json.loads(scorecard(copy).summary)["distributions"]["hands_on"]
    assert (sides["live"]["tool_delay_ms"]["count"], sides["live"]["tool_delay_ms"]["p50"]) == (0, None)
    assert sides["replay"]["tool_delay_ms"]["count"] > 0


# --- The public export -------------------------------------------------------------------------


def test_an_export_writes_a_validated_scanned_bundle_under_docs_evaluations(tmp_path: Path):
    target = tmp_path / "docs" / "evaluations" / "two-hosts"
    derived = report.export(FIXTURE, target, usd_per_host_hour=PRICE, rate_source=SOURCE, root=tmp_path)
    written = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
    assert written == sorted(path.relative_to(FIXTURE).as_posix() for path in FIXTURE.rglob("*") if path.is_file())
    assert (target / report.DIGEST).read_text().strip() == derived.digest
    scan = report.hygiene_scan()
    assert not [finding for path in target.rglob("*") if path.is_file() for finding in scan(path.read_text(), "x")]
    assert scorecard(target) == derived
    with pytest.raises(ReportError) as refused:
        report.export(FIXTURE, tmp_path / "elsewhere", usd_per_host_hour=PRICE, rate_source=SOURCE, root=tmp_path)
    assert refused.value.reason is Refusal.EXPORT_OUTSIDE_EVALUATIONS


def test_regenerating_an_export_keeps_the_price_it_was_made_at(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    source, target = tmp_path / "source", tmp_path / "docs" / "evaluations" / "priced"
    shutil.copytree(FIXTURE, source, ignore=shutil.ignore_patterns(*report.DERIVED))
    derived = report.export(source, target, usd_per_host_hour=7.25, rate_source="supplied", root=tmp_path)
    assert cli.main(["report", str(target)]) == 0
    assert (target / report.DIGEST).read_text().strip() == derived.digest
    assert json.loads((target / report.SUMMARY).read_text())["host_price"]["source"] == "supplied"
    # An explicit rate reprices it.
    assert cli.main(["report", str(target), "--rate", "2"]) == 0
    assert (target / report.DIGEST).read_text().strip() != derived.digest
    capsys.readouterr()


def test_a_price_source_outside_the_vocabulary_is_refused_before_anything_is_written(tmp_path: Path):
    target = tmp_path / "docs" / "evaluations" / "run"
    with pytest.raises(ReportError) as refused:
        report.export(FIXTURE, target, usd_per_host_hour=2.0, rate_source="lab-seat-3", root=tmp_path)
    assert refused.value.reason is Refusal.UNKNOWN_PRICE
    assert not (tmp_path / "docs").exists()


HOME = "/" + "home"  # written in two parts, so that this file is no finding of its own


UNREADABLE = Refusal.UNREADABLE_BUNDLE


@pytest.mark.parametrize(
    ("name", "change", "reason"),
    [
        # A private label where an enumeration is expected.
        (
            report.MANIFEST,
            lambda text: text.replace('"path_category":"in_region"', '"path_category":"lab-seat-3"'),
            UNREADABLE,
        ),
        # Raw model text, as a field the format does not have and as a file of its own.
        (
            "observations/attempt-0.jsonl",
            lambda text: text.replace(
                '"outcome":"completed"', '"outcome":"completed","text":"Sure, here is the plan"', 1
            ),
            UNREADABLE,
        ),
        ("observations/transcript.txt", lambda _text: "Sure, here is the plan\n", Refusal.UNEXPECTED_FILE),
        # A path, in a commit's place and in a label's.
        (
            report.MANIFEST,
            lambda text: text.replace('"source_commit":"' + "a" * 40, '"source_commit":"' + HOME + "/student/run"),
            UNREADABLE,
        ),
        (
            "observations/attempt-0.jsonl",
            lambda text: text.replace('"route":"messages"', '"route":"work/solve.py"', 1),
            UNREADABLE,
        ),
    ],
)
def test_an_export_refuses_a_private_label_model_text_or_a_path_and_writes_nothing(
    tmp_path: Path, name: str, change: Callable[[str], str], reason: Refusal
):
    if not (FIXTURE / name).exists():
        source = tmp_path / "edited"
        shutil.copytree(FIXTURE, source, ignore=shutil.ignore_patterns(*report.DERIVED))
        (source / name).write_text(change(""))
    else:
        source = edited(tmp_path, name, change)
    target = tmp_path / "docs" / "evaluations" / "edited"
    with pytest.raises(ReportError) as refused:
        report.export(source, target, usd_per_host_hour=PRICE, rate_source=SOURCE, root=tmp_path)
    assert refused.value.reason is reason
    assert not (tmp_path / "docs").exists()


def test_an_export_the_hygiene_scanner_finds_anything_in_is_not_written(tmp_path: Path):
    scanned: list[str] = []

    def finds(text: str, path: str) -> list[str]:
        scanned.append(path)
        return ["a finding"] if path.endswith(report.REPORT) else []

    target = tmp_path / "docs" / "evaluations" / "found"
    with pytest.raises(ReportError) as refused:
        report.export(FIXTURE, target, usd_per_host_hour=PRICE, rate_source=SOURCE, root=tmp_path, scan=finds)
    assert refused.value.reason is Refusal.HYGIENE_FINDING
    assert "docs/evaluations/found/manifest.json" in scanned and len(scanned) == len(list(FIXTURE.rglob("*.*")))
    assert not (tmp_path / "docs").exists()


if __name__ == "__main__":
    shutil.rmtree(FIXTURE, ignore_errors=True)
    build(FIXTURE)
    report.write_derived(FIXTURE, scorecard(FIXTURE))
    sys.exit(0)
