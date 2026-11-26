"""The scorecard of a run regenerated from its saved evidence, the offline comparison of two runs
and the public export (docs/design/agent-session-evaluation.md, "Reading a result" and
"Reproducing and comparing runs").

The saved bundle under `fixtures/report/` is the qualification plan of the course suite for a
class of two, on two hosts, written by `build` below: `uv run --locked python` on this file, with
the import path `scripts/evaluate.sh` sets, writes it again. Its class-size repetitions are 102 later
turns of 20 tokens a second each. Hands-on answers the slowest later turns in 3.9 s, under the 5 s
limit; several-agents streams a fifth of its turns at 5 tokens a second in repetition 1, below the
10 tokens a second p10 floor. Every trial passes, but one optimization attempt on OpenCode at class
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
    later_turns: int = 102
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
    turn, a tool pause of 5 to 8 s between them."""
    identity = cell.identity
    per_student = 1 if identity.shape.value == "hands_on" else 2
    turns = -(-variant.later_turns // identity.agent_slots)
    rows: list[RequestRecord] = []
    for slot in range(identity.agent_slots):
        eligible = START + 1000 + slot * 100
        for turn in range(turns + 1):
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
                    finish_reason=FinishReason.TOOL_USE if turn < turns else FinishReason.STOP,
                    usage=Usage(
                        input_tokens=20_000 + 1000 * turn, cached_input_tokens=20_000 if turn else 0, output_tokens=21
                    ),
                )
            )
            eligible = last + 10 + 5000 + 1000 * (turn % 4)
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
    (and a delegated worker beside the first, for a family that delegates). Returns the cell's
    observations and when its last trial ended."""
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
    return observations, at


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
            window = (clock, last, last + 10_000)
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
    assert conditions["host_count"] == {"setting": "host_count", "requested": 2, "observed": 2, "status": "match"}
    # What no evidence showed is not observed, and never filled from the request.
    assert (conditions["step_budget_tokens"]["observed"], conditions["step_budget_tokens"]["status"]) == (
        None,
        "not_observed",
    )
    repetition = summary["capacity"]["hands_on"]["repetitions"][0]
    assert (repetition["later_turns"], repetition["later_turns_min"], repetition["failed"], repetition["attempts"]) == (
        102,
        100,
        0,
        104,
    )
    assert repetition["failed_observed_floor"] and repetition["failed_wilson_high"] > 0
    harnesses = {(row["load"], row["harness"]) for row in summary["capacity_by_harness"]}
    assert {("low_load", "codex"), ("class_load", "opencode"), ("class_load", "sdk_tool_loop")} <= harnesses
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


# --- Comparing ---------------------------------------------------------------------------------


def test_compare_prints_the_treatment_diff_before_the_scorecard(reference: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["compare", str(reference), str(FIXTURE)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("treatment diff (hosts):\n  host_count [hosts]: 1 -> 2\n")
    assert printed.index("treatment diff") < printed.index("# Comparison scorecard")
    assert "## Task time and completions across tokenizers" in printed


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
