"""The offline plan of an evaluation (docs/design/agent-session-evaluation.md, "Booking and
running" and "Workload and measurement protocol"): what `scripts/evaluate.sh plan` expands a
profile into, what it plans once, and the bookings it packs the cells into.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evaluate import cli, contracts, plan
from evaluate.contracts import Harness, Phase, Profile, Shape
from evaluate.plan import PlanError, Refusal, Role

REPO = Path(__file__).resolve().parents[3]
MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
SUITE = plan.load_suite()
LIVE = (Phase.LIVE_LOW_LOAD, Phase.LIVE_CLASS_LOAD)
# The replay shape of each family: of the seat that runs its tasks, and of the seats around it.
BACKGROUND = {
    contracts.Family.OPTIMIZATION: Shape.HANDS_ON,
    contracts.Family.DEBUGGING: Shape.HANDS_ON,
    contracts.Family.ANALYSIS: Shape.SEVERAL_AGENTS,
    contracts.Family.RESEARCH: Shape.SEVERAL_AGENTS,
}


def script(folder: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """`scripts/evaluate.sh plan`, from another folder, in a shell with no import path and no AWS
    credentials: none in the environment, and none in a file."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith("AWS_") and name != "PYTHONPATH"}
    absent = {name: str(folder / "absent") for name in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE")}
    command = [str(REPO / "scripts" / "evaluate.sh"), "plan", *arguments]
    return subprocess.run(command, cwd=folder, env=kept | absent, capture_output=True, text=True, check=False)


def printed(capsys: pytest.CaptureFixture[str], *arguments: str) -> dict[str, Any]:
    """The plan the command line prints as JSON, read in this process."""
    assert cli.main(["plan", *arguments, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def cells_of(planned: plan.Plan, *phases: Phase, repetition: int | None = None) -> list[plan.PlannedCell]:
    return [
        cell
        for cell in planned.cells
        if cell.identity.phase in phases and repetition in (None, cell.identity.repetition)
    ]


def test_the_command_gives_one_workload_hash_offline_and_another_schedule_for_another_seed(tmp_path: Path) -> None:
    qualify = ("--suite", "course-v1", "--profile", "qualify", "--students", "8", "--instances", "1", "--json")
    runs = [script(tmp_path, *qualify) for _time in range(2)]
    assert [run.returncode for run in runs] == [0, 0], runs[0].stderr
    first, again = (json.loads(run.stdout) for run in runs)
    assert first["workload_hash"] == again["workload_hash"]

    # The hash is the identity of the workload it prints: the `workload_id` of a run's manifest.
    workload = contracts.Workload.from_json(first["workload"], "workload")
    undeclared = contracts.Settings(**dict.fromkeys(field.name for field in dataclasses.fields(contracts.Settings)))
    ran = contracts.Run(
        ordinal=0,
        source_commit="0" * 40,
        lock_digest=workload.suite_digest,
        harness_image_digest=workload.suite_digest,
        driver_image_digest=workload.suite_digest,
        driver_cpus=1,
        driver_memory_mib=1,
        path_category=contracts.PathCategory.IN_REGION,
    )
    manifest = contracts.Manifest(workload=workload, requested=undeclared, run=ran, observed=undeclared)
    assert first["workload_hash"] == manifest.workload_id

    def arrivals(planned: dict[str, Any]) -> list[list[int]]:
        return [cell["arrivals_ms"] for cell in planned["cells"] if cell["phase"] == Phase.CLASS_OPENING]

    def identities(planned: dict[str, Any]) -> list[Any]:
        return [planned["workload"]["cells"], planned["workload"]["trials"]]

    rescheduled = json.loads(script(tmp_path, *qualify, "--schedule-seed", "5").stdout)
    assert rescheduled["workload_hash"] != first["workload_hash"]
    assert arrivals(rescheduled) != arrivals(first)
    assert [cell["schedule_seed"] for cell in rescheduled["cells"]] != [
        cell["schedule_seed"] for cell in first["cells"]
    ]
    # The same cells and trials, on another schedule.
    assert identities(rescheduled) == identities(first)

    other_input = json.loads(script(tmp_path, *qualify, "--input-seed", "5").stdout)
    assert other_input["workload_hash"] != first["workload_hash"]
    assert [cell["input_seed"] for cell in other_input["cells"]] != [cell["input_seed"] for cell in first["cells"]]
    assert arrivals(other_input) == arrivals(first)


def test_the_hosts_booked_are_a_treatment_and_not_the_workload(capsys: pytest.CaptureFixture[str]) -> None:
    one = printed(capsys, "--profile", "qualify", "--students", "8", "--instances", "1")
    two = printed(capsys, "--profile", "qualify", "--students", "8", "--instances", "2")
    assert two["workload_hash"] == one["workload_hash"]
    assert two["host_hours"] == pytest.approx(2 * one["host_hours"], abs=0.002)


def test_a_qualification_of_eight_seats_has_162_trials() -> None:
    planned = plan.expand(SUITE, Profile.QUALIFY, 8)
    trials = planned.workload.trials
    assert len(trials) == 162
    assert planned.trials_by_load == {Role.LOW_LOAD: 81, Role.CLASS_LOAD: 81}
    # 27 instance/harness pairs, each attempted three times at each load.
    attempts: dict[tuple[str, Harness, tuple[Role, ...]], list[int]] = {}
    for trial in trials:
        attempts.setdefault((trial.instance_id, trial.harness, planned.loads_of(trial)), []).append(trial.attempt)
    assert len({(instance, harness) for instance, harness, _load in attempts}) == 27
    assert len(attempts) == 54
    assert all(sorted(tried) == [0, 1, 2] for tried in attempts.values())
    # At class size the task has one seat and the replay the seven others.
    for cell in cells_of(planned, Phase.LIVE_CLASS_LOAD):
        assert cell.identities == 8
        assert cell.replay_slots == cell.identity.agent_slots * 7 // 8
    # A live cell is one family at one load in one attempt, so a booking can end between families.
    live = cells_of(planned, *LIVE)
    assert len(live) == 4 * 2 * 3
    for cell in live:
        (family,) = {contracts.Family(trials[at].instance_id.split(".")[0]) for at in cell.trials}
        assert cell.identity.shape is BACKGROUND[family]
    assert max(len(cell.trials) for cell in live) == 15


def test_one_seat_plans_the_equivalent_loads_and_sweep_cells_once() -> None:
    planned = plan.expand(SUITE, Profile.QUALIFY, 1)
    # No other seat can load a live task: each trial is planned once and counts for both loads.
    assert len(planned.workload.trials) == 81
    assert planned.trials_by_load == {Role.LOW_LOAD: 81, Role.CLASS_LOAD: 81}
    assert not cells_of(planned, Phase.LIVE_CLASS_LOAD)
    live = cells_of(planned, Phase.LIVE_LOW_LOAD)
    assert live
    assert all(cell.roles == (Role.LOW_LOAD, Role.CLASS_LOAD) and cell.replay_slots == 0 for cell in live)
    # The one-slot hands-on diagnostic is the class-size cell too: one cell, with both roles.
    for repetition in range(3):
        one_slot = [
            cell
            for cell in cells_of(planned, Phase.CAPACITY_SWEEP, repetition=repetition)
            if (cell.identity.shape, cell.identity.agent_slots) == (Shape.HANDS_ON, 1)
        ]
        assert [cell.roles for cell in one_slot] == [(Role.DIAGNOSTIC, Role.CLASS_FLOOR)]
        assert one_slot[0].admissions_ms == 15 * MINUTE_MS


def test_a_screen_has_one_replay_repetition_and_one_anchor_attempt_of_each_core_task() -> None:
    planned = plan.expand(SUITE, Profile.SCREEN, 8)
    assert {cell.identity.repetition for cell in planned.cells} == {0}
    sweep = cells_of(planned, Phase.CAPACITY_SWEEP)
    assert len(sweep) == len({(cell.identity.shape, cell.identity.agent_slots) for cell in sweep}) == 8
    assert len(cells_of(planned, Phase.CLASS_OPENING)) == 2

    trials = planned.workload.trials
    assert SUITE.core
    assert sorted(trial.instance_id.rsplit(".", 1)[0] for trial in trials) == sorted(SUITE.core)
    assert {(trial.harness, trial.attempt) for trial in trials} == {(SUITE.anchor, 0)}
    assert SUITE.anchor is Harness.CLAUDE_CODE
    # The workload pins the harnesses it schedules and no other: here the anchor alone, so the
    # version of a harness the screen does not run is not part of its identity.
    assert [pin.harness for pin in planned.workload.harnesses] == [Harness.CLAUDE_CODE]
    pins = tuple(
        dataclasses.replace(pin, version="9.9.9") if pin.harness is Harness.PI else pin for pin in SUITE.pins
    )
    other_pi = dataclasses.replace(SUITE, pins=pins)
    assert plan.expand(other_pi, Profile.SCREEN, 8).workload_hash == planned.workload_hash
    qualify = plan.expand(SUITE, Profile.QUALIFY, 8)
    assert {pin.harness for pin in qualify.workload.harnesses} == set(Harness)
    assert plan.expand(other_pi, Profile.QUALIFY, 8).workload_hash != qualify.workload_hash


@pytest.mark.parametrize("students", [8, 10])
def test_the_sweep_runs_both_shapes_at_the_diagnostic_slots_and_at_the_class_size(students: int) -> None:
    planned = plan.expand(SUITE, Profile.QUALIFY, students)
    class_slots = {Shape.HANDS_ON: students, Shape.SEVERAL_AGENTS: 2 * students}
    for repetition in range(3):
        for shape in Shape:
            cells = [
                cell
                for cell in cells_of(planned, Phase.CAPACITY_SWEEP, repetition=repetition)
                if cell.identity.shape is shape
            ]
            slots = [cell.identity.agent_slots for cell in cells]
            assert sorted(slots) == sorted({1, 4, 8, 16, class_slots[shape]})
            for cell in cells:
                agent_slots = cell.identity.agent_slots
                at_class_size = agent_slots == class_slots[shape]
                assert (Role.CLASS_FLOOR in cell.roles) == at_class_size
                assert (Role.DIAGNOSTIC in cell.roles) == (agent_slots in (1, 4, 8, 16))
                # Five minutes of admissions for a diagnostic cell, fifteen at the class size.
                assert cell.admissions_ms == (15 if at_class_size else 5) * MINUTE_MS
                # One identity for each hands-on slot; two several-agents slots share one.
                assert cell.identities == (agent_slots if shape is Shape.HANDS_ON else (agent_slots + 1) // 2)
                if at_class_size:
                    assert cell.identities == students
    # The 16-slot hands-on diagnostic needs 16 canaries, whatever the class.
    assert planned.canary_identities == 16
    assert planned.agent_slots == max(16, 2 * students)
    # No cell drains for more than two minutes, and a booking keeps that time for it.
    for cell in planned.cells:
        assert cell.drain_ms <= 2 * MINUTE_MS
        assert planned.reserved_ms(cell) >= cell.admissions_ms + cell.drain_ms
    assert all(cell.drain_ms == 2 * MINUTE_MS for cell in cells_of(planned, Phase.CAPACITY_SWEEP, *LIVE))


def test_repetitions_have_their_own_seeds_namespace_and_order() -> None:
    planned = plan.expand(SUITE, Profile.QUALIFY, 8)
    orders, seeds, namespaces = [], set(), set()
    for repetition in range(3):
        sweep = cells_of(planned, Phase.CAPACITY_SWEEP, repetition=repetition)
        orders.append([(cell.identity.shape, cell.identity.agent_slots) for cell in sweep])
        steady = sweep + cells_of(planned, *LIVE, repetition=repetition)
        (pair,) = {(cell.input_seed, cell.schedule_seed) for cell in steady}
        (namespace,) = {cell.prefix_namespace for cell in steady}
        seeds.add(pair)
        namespaces.add(namespace)
        assert all(cell.steady_state for cell in steady)
    assert len(namespaces) == 3
    assert len({input_seed for input_seed, _schedule in seeds}) == len({schedule for _input, schedule in seeds}) == 3
    # The same cells in each repetition, in another order.
    assert len({tuple(order) for order in orders}) == 3
    assert len({frozenset(order) for order in orders}) == 1

    # The warm-up comes first, and it and the openings stay out of the steady state, under a
    # prefix namespace no repetition has.
    apart = cells_of(planned, Phase.WARM_UP, Phase.CLASS_OPENING)
    assert planned.cells[0].identity.phase is Phase.WARM_UP
    assert not any(cell.steady_state for cell in apart)
    assert len({cell.prefix_namespace for cell in apart} | namespaces) == len(apart) + 3
    # A class opening starts each of its conversations inside one minute.
    for cell in cells_of(planned, Phase.CLASS_OPENING):
        assert len(cell.arrivals_ms) == cell.identity.agent_slots
        assert list(cell.arrivals_ms) == sorted(cell.arrivals_ms)
        assert 0 <= cell.arrivals_ms[0] <= cell.arrivals_ms[-1] < MINUTE_MS
        assert len(set(cell.arrivals_ms)) > 1


def test_the_suite_holds_the_assignments_and_the_limits_of_the_design() -> None:
    assert len(SUITE.pairs(Profile.QUALIFY)) == 27
    on = {task.family: {assignment.harness for assignment in task.harnesses} for task in SUITE.suite.tasks}
    everywhere = set(Harness)
    assert on[contracts.Family.OPTIMIZATION] == everywhere
    assert on[contracts.Family.DEBUGGING] == {Harness.CLAUDE_CODE, Harness.CODEX}
    assert on[contracts.Family.ANALYSIS] == on[contracts.Family.RESEARCH] == {Harness.CLAUDE_CODE}
    assert all(len(task.instances) == 3 for task in SUITE.suite.tasks)
    # Hands-on background for optimization and debugging, several-agents for analysis and research.
    assert {task.family: SUITE.background[task.task_id] for task in SUITE.suite.tasks} == BACKGROUND
    limits = plan.expand(SUITE, Profile.QUALIFY, 8).workload.limits
    assert (limits.trial_wall_time_ms, limits.trial_model_turns, limits.trial_generated_tokens) == (
        12 * MINUTE_MS,
        32,
        32_000,
    )
    assert limits.drain_ms == 2 * MINUTE_MS
    assert SUITE.cache_policy is contracts.CachePolicy.FRESH_PREFIX_WARM_ENGINE


def test_batches_hold_whole_cells_inside_the_window() -> None:
    planned = plan.expand(SUITE, Profile.QUALIFY, 8)
    window = 12 * HOUR_MS
    bookings = plan.schedule(planned, window)
    assert len(bookings.batches) > 1
    # Every cell after the warm-up is in one batch, whole, in the plan's order.
    assert [cell for batch in bookings.batches for cell in batch.cells] == list(range(1, len(planned.cells)))
    estimates = SUITE.estimates
    for batch in bookings.batches:
        held = sum(planned.reserved_ms(planned.cells[cell]) for cell in batch.cells)
        # Besides its cells, a booking keeps time for the capacity wait, its warm-up and the cleanup.
        assert batch.minimum_window_ms == bookings.overhead_ms + held <= window
    assert bookings.overhead_ms >= estimates.capacity_wait_ms + estimates.warm_up_ms + estimates.cleanup_ms
    # A cell's time has its drain and the wait for its telemetry.
    cell = planned.cells[1]
    assert planned.reserved_ms(cell) == cell.admissions_ms + cell.drain_ms + estimates.telemetry_settle_ms
    assert estimates.telemetry_settle_ms > 0

    # The screen of the design's example is one booking of three hours.
    screen = plan.schedule(plan.expand(SUITE, Profile.SCREEN, 8), 3 * HOUR_MS)
    assert len(screen.batches) == 1


def test_a_cell_that_fits_no_window_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    planned = plan.expand(SUITE, Profile.QUALIFY, 8)
    shortest = plan.schedule(planned, 12 * HOUR_MS).minimum_window_ms
    # The longest cell is the largest family: the 15 optimization trials of an attempt, each at
    # its 12-minute limit. With the booking's reserves it fits a booking of four hours.
    assert 15 * 12 * MINUTE_MS < shortest <= 4 * HOUR_MS
    assert plan.schedule(planned, shortest).batches
    with pytest.raises(PlanError) as refused:
        plan.schedule(planned, shortest - 1)
    assert refused.value.reason is Refusal.CELL_OUTSIDE_WINDOW

    # Bookings of four hours hold the whole qualification, each of its cells whole in one of them.
    arguments = ("--profile", "qualify", "--students", "8", "--instances", "1")
    booked = printed(capsys, *arguments, "--for", "4h")
    assert all(batch["minimum_window_ms"] <= 4 * HOUR_MS for batch in booked["batches"])
    assert [cell for batch in booked["batches"] for cell in batch["cells"]] == list(range(1, len(planned.cells)))
    assert sum(len(booked["cells"][cell]["trials"]) for batch in booked["batches"] for cell in batch["cells"]) == 162

    # In a shorter window the command plans nothing: no part of the cell is placed in a booking.
    assert cli.main(["plan", *arguments, "--for", f"{shortest // MINUTE_MS - 1}m"]) == 2
    said = capsys.readouterr()
    assert said.out == ""
    assert Refusal.CELL_OUTSIDE_WINDOW in said.err


def test_the_plan_prints_what_a_booking_needs(capsys: pytest.CaptureFixture[str]) -> None:
    arguments = ("--profile", "qualify", "--students", "8", "--instances", "1")
    planned = printed(capsys, *arguments)
    assert planned["counts"] == {
        "cells": len(planned["cells"]),
        "trials": 162,
        "trials_by_load": {"low_load": 81, "class_load": 81},
        "canary_identities": 16,
        "agent_slots": 16,
    }
    # The manifest in order: each cell with its ordinal, and each trial in the cell that runs it.
    assert [cell["cell"] for cell in planned["cells"]] == list(range(len(planned["cells"])))
    assert [trial for cell in planned["cells"] for trial in cell["trials"]] == list(range(162))
    assert planned["scratch_mib"] > 0
    assert planned["minimum_window_ms"] <= min(batch["minimum_window_ms"] for batch in planned["batches"])
    booked_ms = sum(batch["minimum_window_ms"] for batch in planned["batches"])
    assert planned["host_hours"] == pytest.approx(booked_ms / HOUR_MS, abs=0.001)
    # Priced at the example rate unless one is given, and labelled either way.
    price = planned["price"]
    assert price["source"] == "example"
    assert price["estimate_usd"] == round(booked_ms * price["usd_per_host_hour"] / HOUR_MS, 2)
    supplied = printed(capsys, *arguments, "--rate", "2")["price"]
    assert (supplied["source"], supplied["usd_per_host_hour"]) == ("supplied", 2.0)
    assert supplied["estimate_usd"] == round(booked_ms * 2 / HOUR_MS, 2)

    # One command for each batch: it books the batch's own window, and all but the first resume.
    commands = [batch["command"] for batch in planned["batches"]]
    for batch, command in zip(planned["batches"], commands, strict=True):
        assert command.startswith("scripts/evaluate.sh run ")
        assert f"--for {-(-batch['minimum_window_ms'] // MINUTE_MS)}m" in command
        assert ("--resume" in command) == (batch["batch"] > 0)

    # The text an operator reads has the same hash and the same commands.
    assert cli.main(["plan", *arguments]) == 0
    text = capsys.readouterr().out
    assert planned["workload_hash"] in text
    assert all(command in text for command in commands)
