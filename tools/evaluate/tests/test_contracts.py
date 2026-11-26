"""The public format of an evaluation run (docs/design/agent-session-evaluation.md, "Reproducing
and comparing runs"): what it keeps across a read, what it refuses and what it tells apart.

Every document here is made up. No number is a measurement and no digest is one of a real file.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import contracts
from contracts import (
    CellIdentity,
    CellRecord,
    CellStatus,
    ContractError,
    FailureReason,
    GpuType,
    Grade,
    GraderOutcome,
    Harness,
    HarnessAssignment,
    HarnessFeature,
    HarnessPin,
    HostMetric,
    HostSample,
    Instance,
    InstanceType,
    InvalidFlag,
    InvalidReason,
    Manifest,
    Phase,
    Quantization,
    RequestOutcome,
    RequestRecord,
    Route,
    Shape,
    Suite,
    Task,
    Traffic,
    TrialIdentity,
    TrialOutcome,
    TrialRecord,
    Unit,
    Usage,
    Workload,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

TOOL = Path(__file__).resolve().parents[1]


class _Drop:
    """The mark of a key to leave out. Its name is the same in every process that collects."""

    def __repr__(self) -> str:
        return "DROP"


DROP = _Drop()


def digest(letter: str) -> str:
    return "sha256:" + letter * 64


def instance(instance_id: str, **changes: Any) -> Instance:
    return Instance(**dict(instance_id=instance_id, fixture_digest=digest("a"), grader_digest=digest("b")) | changes)


def task(task_id: str, *harnesses: HarnessAssignment, **changes: Any) -> Task:
    fields: dict[str, Any] = dict(task_id=task_id, required_properties=3, harnesses=harnesses) | changes
    if "instances" not in fields:
        fields["instances"] = tuple(instance(f"{task_id}.i0{number}") for number in (1, 2, 3))
    return Task(**fields)


CLAUDE_CODE = HarnessAssignment(harness=Harness.CLAUDE_CODE, features=())
DELEGATING = HarnessAssignment(harness=Harness.CLAUDE_CODE, features=(HarnessFeature.DELEGATION,))
CODEX = HarnessAssignment(harness=Harness.CODEX, features=(HarnessFeature.FOLLOW_UP,))
SUITE = Suite(version=1, tasks=(task("analysis.t01", DELEGATING), task("optimization.t01", CLAUDE_CODE, CODEX)))


def workload(**changes: Any) -> Workload:
    planned: dict[str, Any] = dict(
        profile=contracts.Profile.QUALIFY,
        students=8,
        suite_digest=contracts.content_hash(SUITE),
        arrival_digest=digest("c"),
        protocol_digest=digest("d"),
        input_seed=7,
        schedule_seed=11,
        cache_policy=contracts.CachePolicy.FRESH_PREFIX_WARM_ENGINE,
        harnesses=(
            HarnessPin(harness=Harness.CLAUDE_CODE, version="2.1.323", profile_digest=digest("e")),
            HarnessPin(harness=Harness.CODEX, version="0.169.0", profile_digest=digest("f")),
        ),
        limits=contracts.Limits(
            trial_wall_time_ms=720_000, trial_model_turns=32, trial_generated_tokens=32_000, drain_ms=120_000
        ),
        sampling=contracts.Sampling(
            ttft_samples_min=100, stream_samples_min=100, bootstrap_draws=10_000, host_sample_interval_ms=15_000
        ),
        objectives=contracts.Objectives(
            ttft_p95_ms_max=5000,
            stream_speed_p50_tokens_per_s_min=15,
            stream_speed_p10_tokens_per_s_min=10,
            failed_share_max=0.005,
            success_share_min=0.8,
            paired_margin_points=-5,
        ),
        cells=(
            CellIdentity(cell=0, phase=Phase.CAPACITY_SWEEP, shape=Shape.HANDS_ON, agent_slots=8, repetition=0),
            CellIdentity(cell=1, phase=Phase.LIVE_LOW_LOAD, shape=Shape.HANDS_ON, agent_slots=1, repetition=0),
            CellIdentity(cell=2, phase=Phase.LIVE_CLASS_LOAD, shape=Shape.HANDS_ON, agent_slots=8, repetition=0),
        ),
        trials=(
            TrialIdentity(trial=0, cell=1, instance_id="optimization.t01.i01", harness=Harness.CLAUDE_CODE, attempt=0),
            TrialIdentity(trial=1, cell=2, instance_id="optimization.t01.i01", harness=Harness.CLAUDE_CODE, attempt=0),
        ),
    )
    return Workload(**planned | changes)


def settings(**changes: Any) -> contracts.Settings:
    declared: dict[str, Any] = dict(
        host_count=1,
        instance_type=InstanceType.G7E_2XLARGE,
        gpu_type=GpuType.RTX_PRO_6000,
        gpus_per_host=1,
        model_revision="1" * 40,
        tokenizer_revision="2" * 40,
        quantization=Quantization.BITS_8,
        engine_image_digest=digest("3"),
        vllm_version="0.11.0",
        context_limit_tokens=131_072,
        kv_dtype=contracts.KvDtype.BITS_8,
        prefix_caching=True,
        tensor_parallelism=1,
        step_budget_tokens=None,
        speculative_decoding=contracts.SpeculativeDecoding.OFF,
        renderer_digest=digest("4"),
        gateway_profile_digest=digest("5"),
    )
    return contracts.Settings(**declared | changes)


NOTHING_OBSERVED = contracts.Settings(**dict.fromkeys(settings().to_json()))


def manifest(**changes: Any) -> Manifest:
    run = contracts.Run(
        ordinal=0,
        source_commit="0" * 40,
        lock_digest=digest("6"),
        harness_image_digest=digest("7"),
        driver_image_digest=digest("8"),
        driver_cpus=16,
        driver_memory_mib=32_768,
        path_category=contracts.PathCategory.IN_REGION,
    )
    # The engine's version was not read: everything else was observed as requested.
    observed = settings(vllm_version=None)
    parts: dict[str, Any] = dict(workload=workload(), requested=settings(), run=run, observed=observed)
    return Manifest(**parts | changes)


def request(**changes: Any) -> RequestRecord:
    sent: dict[str, Any] = dict(
        request=41,
        cell=0,
        student=3,
        conversation=5,
        turn=2,
        traffic=Traffic.BACKGROUND,
        trial=None,
        route=Route.MESSAGES,
        eligible_ms=1_480,
        sent_ms=1_500,
        first_output_ms=2_250,
        first_answer_ms=2_900,
        last_output_ms=9_000,
        ended_ms=9_020,
        outcome=RequestOutcome.COMPLETED,
        finish_reason=contracts.FinishReason.STOP,
        usage=Usage(input_tokens=20_000, cached_input_tokens=18_000, output_tokens=500),
    )
    return RequestRecord(**sent | changes)


def trial(**changes: Any) -> TrialRecord:
    ran: dict[str, Any] = dict(
        execution=0,
        trial=0,
        replaces=None,
        student=0,
        started_ms=60_000,
        ended_ms=400_000,
        outcome=TrialOutcome.PASSED,
        failure=None,
        invalid=None,
        grade=Grade(properties=(True, True, True), within_limits=True),
        model_turns=14,
        retries=0,
        usage=Usage(input_tokens=250_000, cached_input_tokens=None, output_tokens=9_000),
    )
    return TrialRecord(**ran | changes)


def host_sample(**changes: Any) -> HostSample:
    read: dict[str, Any] = dict(
        cell=0, host=1, at_ms=30_000, metric=HostMetric.OUTPUT_TOKENS_TOTAL, unit=Unit.TOKENS, value=81_234
    )
    return HostSample(**read | changes)


def cell(**changes: Any) -> CellRecord:
    measured: dict[str, Any] = dict(
        cell=0,
        started_ms=10_000,
        admissions_ended_ms=910_000,
        ended_ms=1_030_000,
        status=CellStatus.COMPLETE,
        invalid_flags=(),
        conversations_planned=40,
        conversations_started=40,
        conversations_completed=39,
        conversations_abandoned=1,
        hosts_observed=1,
        driver_lag_p95_ms=12.5,
    )
    return CellRecord(**measured | changes)


KNOWN: dict[str, contracts.Document] = {
    "manifest": manifest(),
    "suite": SUITE,
    "grader_outcome": GraderOutcome(instance_id="analysis.t01.i02", grade=None, invalid=InvalidReason.GRADER_ERROR),
    "request": request(),
    "trial": trial(),
    "host_sample": host_sample(),
    "cell": cell(invalid_flags=(InvalidFlag.EXTERNAL_TRAFFIC, InvalidFlag.HOST_TURNOVER)),
}


def edited(document: contracts.Document, path: str, value: Any) -> str:
    """The document's JSON with the value at a dotted path replaced, or dropped."""
    tree = json.loads(contracts.dumps(document))
    *parents, last = path.split(".")
    node = tree
    for key in parents:
        node = node[int(key)] if key.isdigit() else node[key]
    if value is DROP:
        del node[last]
    else:
        node[int(last) if last.isdigit() else last] = value
    return json.dumps(tree)


def places(node: Any) -> Iterator[tuple[Any, Any]]:
    """Every place of a JSON tree, as its container and its key or index there."""
    keys = list(node) if isinstance(node, dict) else range(len(node)) if isinstance(node, list) else ()
    for key in keys:
        yield node, key
        yield from places(node[key])


def with_each_place(
    document: contracts.Document, value: Any, where: Callable[[Any], bool] = lambda _: True
) -> list[str]:
    """The document's JSON once for each place (that holds what `where` says), the value put there."""
    tree = json.loads(contracts.dumps(document))
    texts = []
    for container, key in places(tree):
        held = container[key]
        if where(held):
            container[key] = value
            texts.append(json.dumps(tree))
            container[key] = held
    return texts


def rewritten(text: str) -> str:
    """The same JSON as another writer would lay it out: keys in the opposite order, indented."""

    def backwards(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: backwards(node[key]) for key in sorted(node, reverse=True)}
        return [backwards(item) for item in node] if isinstance(node, list) else node

    return json.dumps(backwards(json.loads(text)), indent=4)


# --- Round trips ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KNOWN)
def test_a_document_reads_back_equal_with_the_same_bytes_and_hash(kind: str) -> None:
    document = KNOWN[kind]
    text = contracts.dumps(document)
    assert "\n" not in text

    first = contracts.parse(text)
    second = contracts.loads(rewritten(text), type(document))

    assert type(first) is type(document)
    assert first == document == second
    assert contracts.dumps(first) == text == contracts.dumps(second)
    assert contracts.content_hash(first) == contracts.content_hash(document) == contracts.content_hash(second)
    assert json.loads(text)["kind"] == kind
    assert json.loads(text)["schema_version"] == contracts.SCHEMA_VERSION


def test_another_interpreter_reads_the_same_hashes_and_identities() -> None:
    lines = [contracts.dumps(document) for document in KNOWN.values()]
    program = (
        "import sys, contracts\n"
        "for line in sys.stdin.read().splitlines():\n"
        "    document = contracts.parse(line)\n"
        "    ids = [document.workload_id, document.treatment_id, document.run_id] if document.KIND == 'manifest' else []\n"
        "    print(contracts.content_hash(document), contracts.dumps(document) == line, *ids)\n"
    )
    # Another hash seed: nothing of the bytes may rest on the order a set or a dict happens to have.
    environment = {**os.environ, "PYTHONPATH": str(TOOL), "PYTHONHASHSEED": "745"}
    printed = subprocess.run(
        [sys.executable, "-c", program],
        input="\n".join(lines),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    known = manifest()
    assert printed[0].split() == [
        contracts.content_hash(known), "True", known.workload_id, known.treatment_id, known.run_id
    ]  # fmt: skip
    assert [line.split() for line in printed[1:]] == [
        [contracts.content_hash(document), "True"] for document in list(KNOWN.values())[1:]
    ]


def test_values_keep_their_units_across_a_read() -> None:
    sample = contracts.loads(contracts.dumps(host_sample()), HostSample)
    assert (sample.metric, sample.unit, sample.value) == (HostMetric.OUTPUT_TOKENS_TOTAL, Unit.TOKENS, 81_234.0)
    # The unit is the metric's own: a sample cannot restate tokens as a ratio, built or read.
    with pytest.raises(ContractError, match="unit"):
        host_sample(unit=Unit.RATIO, value=0.5)
    with pytest.raises(ContractError, match="unit"):
        contracts.parse(edited(host_sample(), "unit", "ratio"))
    assert host_sample(metric=HostMetric.GPU_UTILIZATION, unit=Unit.RATIO, value=0.97).value == 0.97
    with pytest.raises(ContractError, match="ratio"):
        host_sample(metric=HostMetric.GPU_UTILIZATION, unit=Unit.RATIO, value=97)

    # A time stays a whole number of milliseconds and a count stays a count.
    sent = contracts.loads(contracts.dumps(request()), RequestRecord)
    assert (sent.sent_ms, sent.first_output_ms, type(sent.sent_ms)) == (1_500, 2_250, int)
    assert sent.usage is not None
    assert (sent.usage.output_tokens, type(sent.usage.output_tokens)) == (500, int)
    for wrong in (1_500.0, "1500", 1.5):
        with pytest.raises(ContractError, match="sent_ms"):
            contracts.parse(edited(request(), "sent_ms", wrong))

    # A measurement has one written form, whichever way the number was given.
    limits = contracts.loads(contracts.dumps(manifest()), Manifest).workload.objectives
    assert (limits.ttft_p95_ms_max, limits.paired_margin_points) == (5000.0, -5.0)
    whole = contracts.parse(edited(cell(), "driver_lag_p95_ms", 12))
    assert whole == cell(driver_lag_p95_ms=12.0)
    assert contracts.content_hash(whole) == contracts.content_hash(cell(driver_lag_p95_ms=12.0))
    assert contracts.dumps(cell(driver_lag_p95_ms=-0.0)) == contracts.dumps(cell(driver_lag_p95_ms=0))


# --- Refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize("version", [DROP, 0, 2, "1", 1.0, True, None], ids=repr)
@pytest.mark.parametrize("kind", ["manifest", "request"])
def test_only_the_schema_version_of_the_module_is_read(kind: str, version: Any) -> None:
    with pytest.raises(ContractError, match="schema_version"):
        contracts.parse(edited(KNOWN[kind], "schema_version", version))


@pytest.mark.parametrize("kind", KNOWN)
def test_a_field_the_format_does_not_have_is_refused_wherever_it_is_put(kind: str) -> None:
    tree = json.loads(contracts.dumps(KNOWN[kind]))
    objects = [tree, *(container[key] for container, key in places(tree) if isinstance(container[key], dict))]
    for node in objects:
        for value in (None, 1, "optimization.t01"):
            node["note"] = value
            with pytest.raises(ContractError, match="unexpected"):
                contracts.parse(json.dumps(tree))
            del node["note"]


@pytest.mark.parametrize("kind", KNOWN)
def test_a_key_left_out_is_refused_even_where_null_is_allowed(kind: str) -> None:
    tree = json.loads(contracts.dumps(KNOWN[kind]))
    for container, key in list(places(tree)):
        if isinstance(container, dict):
            held = container.pop(key)
            with pytest.raises(ContractError):
                contracts.parse(json.dumps(tree))
            container[key] = held


def test_other_text_than_one_document_is_refused() -> None:
    text = contracts.dumps(request())
    with pytest.raises(ContractError, match="kind"):
        contracts.loads(text, TrialRecord)
    with pytest.raises(ContractError, match="kind"):
        contracts.parse(edited(request(), "kind", "summary"))
    with pytest.raises(ContractError, match="twice"):
        contracts.parse(text[:-1] + ',"cell":1}')
    for broken in (text[:-1], "[" + text + "]", "", text + text):
        with pytest.raises(ContractError):
            contracts.parse(broken)


# What must never reach a public record, in the shapes it comes in. Each is a documentation
# placeholder or made up: an address, a host, a host spelled like an instance type, a path, an
# account as text and as a number, the ID of a deployed instance, an IP address, a name, a line of
# a prompt.
PRIVATE = [
    "student@example.com",
    "gateway.example.com",
    "lab1-alice.large",
    "/srv/eval/run-1/trial-3/output.json",
    "123456789012",
    123456789012,
    123456789012.0,
    "i-1234567890abcdef0",
    "10.0.0.1",
    "alice",
    "Solve the production plan and write the table.",
]


@pytest.mark.parametrize("private", PRIVATE, ids=repr)
@pytest.mark.parametrize("kind", KNOWN)
def test_no_place_of_a_document_takes_a_private_identifier(kind: str, private: Any) -> None:
    # Every place: each ID, digest, version, type, label, ordinal, count, time and measurement.
    for text in with_each_place(KNOWN[kind], private):
        with pytest.raises(ContractError) as refusal:
            contracts.parse(text)
        # The refusal goes to logs and reports: it never repeats what was put there.
        assert str(private) not in str(refusal.value)


@pytest.mark.parametrize("private", [value for value in PRIVATE if isinstance(value, str)])
def test_a_record_cannot_be_built_around_a_private_identifier(private: str) -> None:
    builds: list[Any] = [
        lambda: instance(private),
        lambda: instance("analysis.t01.i01", fixture_digest=private),
        lambda: task(private, CLAUDE_CODE, instances=(instance("analysis.t01.i01"),)),
        lambda: GraderOutcome(instance_id=private, grade=None, invalid=InvalidReason.GRADER_ERROR),
        lambda: replace(workload().trials[0], instance_id=private),
        lambda: settings(instance_type=private),
        lambda: settings(gpu_type=private),
        lambda: settings(vllm_version=private),
        lambda: settings(model_revision=private),
        lambda: settings(quantization=private),
        lambda: request(student=private),
        lambda: request(route=private),
        lambda: host_sample(host=private),
    ]
    for build in builds:
        with pytest.raises(ContractError) as refusal:
            build()
        assert private not in str(refusal.value)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
@pytest.mark.parametrize("kind", KNOWN)
def test_no_number_is_read_from_a_literal_that_is_not_finite(kind: str, literal: str) -> None:
    numbers = with_each_place(KNOWN[kind], "NUMBER", lambda held: type(held) in (int, float))
    assert numbers
    for text in numbers:
        with pytest.raises(ContractError):
            contracts.parse(text.replace('"NUMBER"', literal))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 10**400, True, "0.5"], ids=repr)
def test_a_measurement_that_is_not_a_finite_number_is_not_built(value: Any) -> None:
    with pytest.raises(ContractError, match="value"):
        host_sample(value=value)
    with pytest.raises(ContractError, match="driver_lag_p95_ms"):
        cell(driver_lag_p95_ms=value)
    with pytest.raises(ContractError, match="failed_share_max"):
        replace(workload().objectives, failed_share_max=value)


# --- What the records tell apart --------------------------------------------------------------


def test_missing_usage_is_not_zero_usage() -> None:
    missing = request(usage=None)
    zero = request(usage=Usage(input_tokens=0, cached_input_tokens=0, output_tokens=0))
    unknown_cache = request(usage=Usage(input_tokens=0, cached_input_tokens=None, output_tokens=0))

    read = [contracts.loads(contracts.dumps(record), RequestRecord) for record in (missing, zero, unknown_cache)]
    assert read == [missing, zero, unknown_cache]
    assert [record.usage_missing for record in read] == [True, False, False]
    assert read[1].usage is not None
    assert read[1].usage.output_tokens == 0
    assert len({contracts.content_hash(record) for record in read}) == 3
    assert json.loads(contracts.dumps(missing))["usage"] is None

    # A stream cut short keeps the input it reported and has no output count: not a count of zero.
    cut = request(outcome=RequestOutcome.TRUNCATED_STREAM, finish_reason=None)
    partial = contracts.loads(edited(cut, "usage.output_tokens", None), RequestRecord)
    silent = contracts.loads(edited(cut, "usage.output_tokens", 0), RequestRecord)
    assert partial.usage == Usage(input_tokens=20_000, cached_input_tokens=18_000, output_tokens=None)
    assert silent.usage is not None
    assert silent.usage.output_tokens == 0
    assert contracts.loads(contracts.dumps(partial), RequestRecord) == partial != silent
    assert contracts.content_hash(partial) != contracts.content_hash(silent)
    assert not partial.usage_missing
    # No count at all has one written form, the null usage.
    with pytest.raises(ContractError, match="null usage"):
        Usage(input_tokens=None, cached_input_tokens=None, output_tokens=None)
    with pytest.raises(ContractError, match="cached"):
        Usage(input_tokens=10, cached_input_tokens=11, output_tokens=None)

    # Missing is written: a record that says nothing about usage is not one that had none.
    for path in ("usage", "usage.cached_input_tokens", "first_output_ms", "trial"):
        with pytest.raises(ContractError, match="null"):
            contracts.parse(edited(zero, path, DROP))


def test_a_completed_failure_is_not_an_invalid_attempt() -> None:
    rejected = Grade(properties=(True, False, True), within_limits=True)
    failed = trial(outcome=TrialOutcome.FAILED, failure=FailureReason.REJECTED, grade=rejected)
    timed_out = trial(outcome=TrialOutcome.FAILED, failure=FailureReason.TIMEOUT, grade=None, model_turns=None)
    invalid = trial(outcome=TrialOutcome.INVALID, invalid=InvalidReason.GRADER_ERROR, grade=None)
    replacement = trial(execution=1, replaces=invalid.execution, started_ms=500_000, ended_ms=800_000)

    read = [
        contracts.loads(contracts.dumps(record), TrialRecord) for record in (failed, timed_out, invalid, replacement)
    ]
    assert read == [failed, timed_out, invalid, replacement]
    # A failure stays in the denominator; an invalid attempt is no attempt, and is what gets replaced.
    assert [record.counts_as_attempt for record in read] == [True, True, False, True]
    assert [record.outcome for record in read[:3]] == [TrialOutcome.FAILED, TrialOutcome.FAILED, TrialOutcome.INVALID]
    assert (read[2].failure, read[2].invalid) == (None, InvalidReason.GRADER_ERROR)
    assert (read[0].failure, read[0].invalid) == (FailureReason.REJECTED, None)
    assert (read[3].trial, read[3].replaces) == (invalid.trial, invalid.execution)

    # Neither can be written as the other, and neither as a pass.
    refused: list[dict[str, Any]] = [
        dict(outcome=TrialOutcome.INVALID, failure=FailureReason.TIMEOUT, grade=None),
        dict(
            outcome=TrialOutcome.INVALID, invalid=InvalidReason.GRADER_ERROR, failure=FailureReason.TIMEOUT, grade=None
        ),
        dict(outcome=TrialOutcome.INVALID, invalid=InvalidReason.GRADER_ERROR, grade=rejected),
        dict(outcome=TrialOutcome.FAILED, invalid=InvalidReason.DRIVER_ERROR, grade=None),
        dict(outcome=TrialOutcome.FAILED, grade=rejected),
        dict(outcome=TrialOutcome.FAILED, failure=FailureReason.REJECTED, grade=None),
        dict(outcome=TrialOutcome.FAILED, failure=FailureReason.TIMEOUT),
        dict(outcome=TrialOutcome.PASSED, grade=rejected),
        dict(outcome=TrialOutcome.PASSED, grade=None),
        dict(outcome=TrialOutcome.PASSED, failure=FailureReason.TIMEOUT),
        dict(execution=1, replaces=1),
    ]
    for fields in refused:
        with pytest.raises(ContractError):
            trial(**fields)
    with pytest.raises(ContractError, match="failure"):
        contracts.parse(edited(invalid, "failure", "timeout"))


def test_two_treatments_of_one_planned_workload_share_its_identity() -> None:
    one_host = manifest()
    two_hosts = manifest(requested=settings(host_count=2), observed=settings(host_count=2))
    four_bit = manifest(requested=settings(quantization=Quantization.BITS_4))

    assert one_host.workload_id == two_hosts.workload_id == four_bit.workload_id
    assert len({one_host.treatment_id, two_hosts.treatment_id, four_bit.treatment_id}) == 3
    assert len({one_host.run_id, two_hosts.run_id, four_bit.run_id}) == 3
    assert contracts.loads(contracts.dumps(two_hosts), Manifest).treatment_id == two_hosts.treatment_id

    # Another plan is another workload under the same treatment: a seed, a limit, a task's grader.
    regraded = Suite(version=1, tasks=(task("analysis.t01", DELEGATING, required_properties=4), *SUITE.tasks[1:]))
    longer = replace(workload().limits, trial_wall_time_ms=900_000)
    for changed in (dict(schedule_seed=12), dict(limits=longer), dict(suite_digest=contracts.content_hash(regraded))):
        other = manifest(workload=workload(**changed))
        assert other.workload_id != one_host.workload_id
        assert other.treatment_id == one_host.treatment_id
        assert other.run_id != one_host.run_id

    # A second run of the same treatment on the same workload is the same two, and another run.
    again = manifest(run=replace(one_host.run, ordinal=1))
    assert (again.workload_id, again.treatment_id) == (one_host.workload_id, one_host.treatment_id)
    assert again.run_id != one_host.run_id


# --- The manifest -----------------------------------------------------------------------------


def test_what_was_observed_is_kept_apart_from_what_was_requested() -> None:
    known = manifest()
    read = contracts.loads(contracts.dumps(known), Manifest)
    assert (read.requested.vllm_version, read.observed.vllm_version) == ("0.11.0", None)
    assert (read.requested.step_budget_tokens, read.observed.host_count) == (None, 1)
    assert json.loads(contracts.dumps(known))["observed"]["vllm_version"] is None

    # A run that observed nothing is still the same run of the same treatment: nothing is filled in.
    blind = manifest(observed=NOTHING_OBSERVED)
    assert contracts.loads(contracts.dumps(blind), Manifest).observed == NOTHING_OBSERVED
    assert (blind.workload_id, blind.treatment_id, blind.run_id) == (
        known.workload_id,
        known.treatment_id,
        known.run_id,
    )
    assert contracts.content_hash(blind) != contracts.content_hash(known)

    # The hardware is a public name of a fixed list, and a host has the GPUs of its type.
    assert (read.observed.instance_type, read.observed.gpu_type) == (InstanceType.G7E_2XLARGE, GpuType.RTX_PRO_6000)
    assert json.loads(contracts.dumps(known))["observed"]["gpu_type"] == "RTX PRO 6000"
    upgrade = settings(instance_type=InstanceType.G7E_24XLARGE, gpus_per_host=4, tensor_parallelism=4)
    assert contracts.loads(contracts.dumps(manifest(requested=upgrade)), Manifest).requested == upgrade
    for path, wrong in (("instance_type", "g7e.3xlarge"), ("gpu_type", "H200"), ("gpus_per_host", 4)):
        with pytest.raises(ContractError):
            contracts.parse(edited(known, f"observed.{path}", wrong))

    for path in ("observed.vllm_version", "requested.step_budget_tokens", "observed", "workload.limits.drain_ms"):
        with pytest.raises(ContractError, match="null"):
            contracts.parse(edited(known, path, DROP))
    # What a run is planned and identified by cannot be missing.
    for path in ("run.source_commit", "workload.suite_digest", "workload.limits.trial_model_turns", "workload.cells"):
        with pytest.raises(ContractError):
            contracts.parse(edited(known, path, None))


def test_a_plan_names_each_required_cell_and_trial_once() -> None:
    planned = contracts.loads(contracts.dumps(manifest()), Manifest).workload
    assert [cell.cell for cell in planned.cells] == [0, 1, 2]
    assert [(one.trial, one.cell, one.instance_id, one.harness, one.attempt) for one in planned.trials] == [
        (0, 1, "optimization.t01.i01", Harness.CLAUDE_CODE, 0),
        (1, 2, "optimization.t01.i01", Harness.CLAUDE_CODE, 0),
    ]
    assert planned.limits.trial_wall_time_ms == 720_000
    assert (planned.limits.trial_model_turns, planned.limits.trial_generated_tokens) == (32, 32_000)

    cells, trials = planned.cells, planned.trials
    refused: list[dict[str, Any]] = [
        dict(cells=()),
        dict(cells=cells[:2]),  # the second trial's cell is no longer in the plan
        dict(cells=(cells[1], cells[0], cells[2])),
        dict(cells=(*cells, replace(cells[0], cell=3))),  # the same cell twice
        dict(trials=(trials[1], trials[0])),
        dict(trials=(*trials, replace(trials[0], trial=2))),  # the same attempt twice
        dict(trials=(*trials, replace(trials[0], trial=2, cell=0, attempt=1))),  # a trial in the capacity sweep
        dict(trials=(replace(trials[0], harness=Harness.PI), trials[1])),  # a harness the plan does not pin
        dict(harnesses=planned.harnesses[::-1]),
    ]
    for fields in refused:
        with pytest.raises(ContractError):
            workload(**fields)

    # A live cell is in the plan for its trials: with none to run it is refused, built or read.
    for left in (trials[:1], trials[1:], ()):
        with pytest.raises(ContractError, match="live cell"):
            workload(trials=tuple(replace(one, trial=at) for at, one in enumerate(left)))
    with pytest.raises(ContractError, match="live cell"):
        contracts.parse(edited(manifest(), "workload.trials", [planned.trials[0].to_json()]))
    # A capacity or warm-up cell has none, and a plan of such cells only is a plan.
    assert workload(cells=(replace(cells[0], phase=Phase.WARM_UP), replace(cells[0], cell=1)), trials=()).trials == ()


# --- The records ------------------------------------------------------------------------------


def test_times_are_since_the_start_of_the_run_and_do_not_go_back() -> None:
    refused = [
        lambda: request(sent_ms=1_000),
        lambda: request(first_output_ms=1_000),
        lambda: request(first_answer_ms=9_010),
        lambda: request(ended_ms=8_000),
        lambda: request(sent_ms=-1),
        lambda: request(first_output_ms=None),
        lambda: request(first_output_ms=None, last_output_ms=None),
        lambda: trial(ended_ms=59_999),
        lambda: cell(admissions_ended_ms=9_000),
        lambda: cell(ended_ms=900_000),
        lambda: host_sample(at_ms=contracts.MAX_TIME_MS + 1),
    ]
    for build in refused:
        with pytest.raises(ContractError):
            build()
    # A request that failed before any output has no output time, and says so.
    cut = request(
        first_output_ms=None, first_answer_ms=None, last_output_ms=None, outcome=RequestOutcome.TRANSPORT_ERROR,
        finish_reason=None, usage=None,
    )  # fmt: skip
    assert contracts.loads(contracts.dumps(cut), RequestRecord).failed
    assert not request().failed


def test_records_hold_ordinals_labels_and_digests_only() -> None:
    refused = [
        lambda: request(student=-1),
        lambda: request(student=1.0),
        lambda: request(student=True),
        lambda: request(route="messages/v1"),
        lambda: request(traffic=Traffic.LIVE),
        lambda: request(trial=0),
        lambda: host_sample(host=contracts.MAX_COUNT),
        lambda: host_sample(value=-1),
        lambda: cell(invalid_flags=(InvalidFlag.HOST_TURNOVER, InvalidFlag.EXTERNAL_TRAFFIC)),
        lambda: cell(invalid_flags=(InvalidFlag.HOST_TURNOVER, InvalidFlag.HOST_TURNOVER)),
        lambda: cell(conversations_started=41),
        lambda: cell(conversations_completed=40),
        lambda: instance("analysis.t01.i01", grader_digest="sha256:" + "A" * 64),
        lambda: instance("analysis.t01.i01", grader_digest="a" * 64),
        lambda: instance("analysis.t01.i01", fixture_digest=digest("a") + "0"),
        lambda: settings(model_revision="main"),
        lambda: settings(vllm_version="0.11"),
    ]
    for build in refused:
        with pytest.raises(ContractError):
            build()
    assert request(traffic=Traffic.LIVE, trial=1).trial == 1
    # A flagged cell keeps what it observed; it only stops counting for qualification.
    flagged = contracts.loads(contracts.dumps(KNOWN["cell"]), CellRecord)
    assert (flagged.conversations_completed, flagged.driver_lag_p95_ms) == (39, 12.5)
    assert not flagged.valid_for_qualification
    assert cell().valid_for_qualification
    assert not cell(status=CellStatus.INCOMPLETE).valid_for_qualification
    # So does a cell that skipped an opening, whether it skipped one or all of them.
    for started in (39, 0):
        skipped = cell(conversations_started=started, conversations_completed=0, conversations_abandoned=0)
        read = contracts.loads(contracts.dumps(skipped), CellRecord)
        assert (read.conversations_planned, read.conversations_started, read.conversations_skipped) == (
            40,
            started,
            40 - started,
        )
        assert not read.valid_for_qualification
    # A scrape that gave nothing is a sample with no value, not a sample of zero.
    gap = contracts.loads(contracts.dumps(host_sample(value=None)), HostSample)
    assert gap.value is None
    assert gap != host_sample(value=0)


# --- The suite --------------------------------------------------------------------------------


def test_a_suite_keeps_the_ids_digests_and_features_its_authors_gave() -> None:
    read = contracts.loads(contracts.dumps(SUITE), Suite)
    analysis, optimization = read.tasks
    assert (analysis.task_id, analysis.family) == ("analysis.t01", contracts.Family.ANALYSIS)
    assert [one.instance_id for one in analysis.instances] == [f"analysis.t01.i0{number}" for number in (1, 2, 3)]
    assert (analysis.instances[0].fixture_digest, analysis.instances[0].grader_digest) == (digest("a"), digest("b"))
    assert {one.harness: one.features for one in analysis.harnesses} == {
        Harness.CLAUDE_CODE: (HarnessFeature.DELEGATION,)
    }
    assert {one.harness: one.features for one in optimization.harnesses} == {
        Harness.CLAUDE_CODE: (),
        Harness.CODEX: (HarnessFeature.FOLLOW_UP,),
    }

    # An ID is given, not a position: a task added before another leaves the other's ID, and what
    # a task holds is in the suite's hash.
    grown = Suite(version=1, tasks=(task("analysis.t01", DELEGATING), task("debugging.t01", CODEX), SUITE.tasks[1]))
    assert grown.tasks[2] == SUITE.tasks[1]
    regraded = instance("analysis.t01.i01", grader_digest=digest("9"))
    changed = Suite(version=1, tasks=(task("analysis.t01", DELEGATING, instances=(regraded,)), SUITE.tasks[1]))
    assert len({contracts.content_hash(suite) for suite in (SUITE, grown, changed)}) == 3


@pytest.mark.parametrize(
    "task_id",
    [
        "analysis",
        "analysis.t1",
        "analysis.t001",
        "analysis-t01",
        "Analysis.t01",
        "planning.t01",
        "analysis.t01.i01",
        "t01",
    ],
)
def test_a_task_id_is_a_family_and_a_number(task_id: str) -> None:
    with pytest.raises(ContractError, match="task_id"):
        task(task_id, CLAUDE_CODE, instances=(instance("analysis.t01.i01"),))
    with pytest.raises(ContractError, match="instance_id"):
        instance(f"{task_id}.i01")
    for instance_id in (
        "analysis.t01",
        "analysis.t01.i1",
        "analysis.t01.i001",
        "analysis.t01.01",
        "analysis.t01.i01.i02",
    ):
        with pytest.raises(ContractError, match="instance_id"):
            instance(instance_id)


def test_a_suite_lists_each_task_instance_and_harness_once() -> None:
    first, second = (instance(f"analysis.t01.i0{number}") for number in (1, 2))
    refused = [
        lambda: Suite(version=1, tasks=()),
        lambda: Suite(version=0, tasks=SUITE.tasks),
        lambda: Suite(version=1, tasks=(SUITE.tasks[0], SUITE.tasks[0])),
        lambda: Suite(version=1, tasks=SUITE.tasks[::-1]),
        lambda: task("analysis.t01", CLAUDE_CODE, instances=()),
        lambda: task("analysis.t01", CLAUDE_CODE, instances=(first, first)),
        lambda: task("analysis.t01", CLAUDE_CODE, instances=(second, first)),
        lambda: task("analysis.t01", CLAUDE_CODE, instances=(instance("analysis.t02.i01"),)),
        lambda: task("analysis.t01", CLAUDE_CODE, instances=(instance("research.t01.i01"),)),
        lambda: task("analysis.t01"),
        lambda: task("analysis.t01", CLAUDE_CODE, DELEGATING),
        lambda: task("analysis.t01", CODEX, CLAUDE_CODE),
        lambda: task("analysis.t01", CLAUDE_CODE, required_properties=0),
        lambda: HarnessAssignment(harness=Harness.PI, features=(HarnessFeature.SKILLS, HarnessFeature.DELEGATION)),
        lambda: HarnessAssignment(harness=Harness.PI, features=("delegation",)),  # ty: ignore[invalid-argument-type] - the refusal under test
    ]
    for build in refused:
        with pytest.raises(ContractError):
            build()
    with pytest.raises(ContractError, match="features"):
        contracts.parse(edited(SUITE, "tasks.0.harnesses.0.features", ["telepathy"]))


def test_a_grader_answers_with_a_grade_or_with_the_reason_there_is_none() -> None:
    accepted = Grade(properties=(True, True), within_limits=True)
    assert accepted.accepted
    assert not Grade(properties=(True, False), within_limits=True).accepted
    assert not Grade(properties=(True, True), within_limits=False).accepted

    graded = GraderOutcome(instance_id="research.t02.i03", grade=accepted, invalid=None)
    broken = GraderOutcome(instance_id="research.t02.i03", grade=None, invalid=InvalidReason.GRADER_ERROR)
    read = [contracts.loads(contracts.dumps(outcome), GraderOutcome) for outcome in (graded, broken)]
    assert read == [graded, broken]
    assert (read[0].grade, read[0].invalid) == (accepted, None)
    assert (read[1].grade, read[1].invalid) == (None, InvalidReason.GRADER_ERROR)

    with pytest.raises(ContractError, match="one of the two"):
        GraderOutcome(instance_id="research.t02.i03", grade=accepted, invalid=InvalidReason.GRADER_ERROR)
    with pytest.raises(ContractError, match="one of the two"):
        GraderOutcome(instance_id="research.t02.i03", grade=None, invalid=None)
    with pytest.raises(ContractError):
        Grade(properties=(), within_limits=True)
    with pytest.raises(ContractError, match="properties"):
        contracts.parse(edited(graded, "grade.properties", ["yes", "no"]))
