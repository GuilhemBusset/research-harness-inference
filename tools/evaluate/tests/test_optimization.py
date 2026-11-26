"""The production-planning task of the course suite and its grader (`evaluate.course.optimization`).

The grader reads what a run wrote, `plan.json` and `plan.tex`, so most tests here write those two
files and grade them. Two run a model, in a scratch copy of a workspace and in a second
interpreter: the reference fix, which must be accepted on every instance, and the task as given,
which must not. `TWIN` is a small instance whose optimum is plain and which has several optimal
plans: a property that is broken alone, a tolerance and an equivalent plan are shown on it.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evaluate import contracts
from evaluate import plan as planning
from evaluate.course import optimization
from evaluate.course.optimization import INSTANCE_IDS, PROPERTIES, Property, grader

EXEMPLAR, CHANGED_DEMAND, CHANGED_CAPACITY = INSTANCE_IDS

# Ten bells to make at 2 each, with nothing else to pay: every plan that meets the demand within
# the machine's hours costs 20. The first week has hours for seven bells and their setup.
TWIN = grader.Instance.parse(
    {
        "weeks": 2,
        "machine_hours_per_week": [8, 20],
        "products": {
            "bell": {
                "demand": [5, 5],
                "initial_stock": 0,
                "unit_cost": 2,
                "holding_cost": 0,
                "setup_cost": 0,
                "hours_per_unit": 1,
                "setup_hours": 1,
            }
        },
    }
)
TWIN_OPTIMUM = 20.0


def reference_plan(instance_id: str) -> dict[str, Any]:
    return json.loads((optimization.reference_artifacts(instance_id) / "plan.json").read_text(encoding="utf-8"))


def twin_plan(made: list[float], kept: list[float], set_up: list[float]) -> dict[str, Any]:
    return {"cost": 2 * sum(made), "production": {"bell": made}, "stock": {"bell": kept}, "setups": {"bell": set_up}}


def table(plan: dict[str, Any], cost: object, *, headers: list[str] | None = None) -> str:
    """A plan's table in the layout of TASK.md, with this cost and, if given, these column headers."""
    products = list(plan["production"])
    weeks = range(len(plan["production"][products[0]]))
    rows = [" & ".join(["Week", *(headers or products)])]
    rows += [" & ".join([str(t + 1), *(str(plan["production"][p][t]) for p in products)]) for t in weeks]
    rows.append(f"Total cost & {cost}" + " &" * (len(products) - 1))
    return "\\begin{tabular}{l" + "r" * len(products) + "}\n" + " \\\\\n".join(rows) + " \\\\\n\\end{tabular}\n"


def produced(folder: Path, plan: dict[str, Any], latex: str | None = None) -> Path:
    """A folder as a run leaves it: the plan's numbers, and its own table unless another is given."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (folder / "plan.tex").write_text(table(plan, plan["cost"]) if latex is None else latex, encoding="utf-8")
    return folder


def failed(report: optimization.Report) -> set[Property]:
    return {one for one in PROPERTIES if not report.holds(one)}


def run_model(workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "model.py"], cwd=workspace, capture_output=True, text=True, timeout=120, check=False
    )


# --- The family: its instances, what a trial is given, and the suite ---------------------------


def test_the_suite_names_the_instances_and_the_files_they_are_made_of() -> None:
    suite = planning.load_suite("course-v1").suite
    task = next(task for task in suite.tasks if task.task_id == optimization.TASK_ID)

    # A grade answers the properties the suite requires, and the suite names what is committed
    # here: an instance or a grader that changes is another digest, so another suite.
    assert task.required_properties == len(PROPERTIES)
    assert [(one.instance_id, one.fixture_digest, one.grader_digest) for one in task.instances] == [
        (one, optimization.fixture_digest(one), optimization.grader_digest(one)) for one in INSTANCE_IDS
    ]
    assert len({optimization.fixture_digest(one) for one in INSTANCE_IDS}) == len(INSTANCE_IDS)


def test_the_instances_change_the_demand_and_the_capacity_of_the_exemplar() -> None:
    exemplar, demand, capacity = (optimization.load_instance(one) for one in INSTANCE_IDS)

    assert demand.products != exemplar.products
    assert demand.machine_hours == exemplar.machine_hours
    assert capacity.machine_hours != exemplar.machine_hours
    assert capacity.products == exemplar.products


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_workspace_holds_the_task_and_nothing_of_the_reference(tmp_path: Path, instance_id: str) -> None:
    optimization.materialize(instance_id, tmp_path / "workspace")

    assert {file.name for file in (tmp_path / "workspace").iterdir()} == {"TASK.md", "model.py", "instance.json"}
    assert (tmp_path / "workspace" / "model.py").read_bytes() != optimization.REFERENCE_MODEL.read_bytes()
    sources = optimization.workspace_files(instance_id).values()
    assert not any(source.is_relative_to(optimization.SEALED) for source in sources)


# --- The references ----------------------------------------------------------------------------


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_committed_reference_is_accepted(instance_id: str) -> None:
    report = optimization.grade(instance_id, optimization.reference_artifacts(instance_id))

    assert report.accepted, report.faults
    # One answer for each required property: what a grade of the run's public format holds.
    assert contracts.Grade(properties=report.properties, within_limits=True).accepted


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_reference_model_solves_a_workspace_to_the_committed_optimum(tmp_path: Path, instance_id: str) -> None:
    optimization.materialize(instance_id, tmp_path)
    shutil.copyfile(optimization.REFERENCE_MODEL, tmp_path / "model.py")

    result = run_model(tmp_path)

    assert result.returncode == 0, result.stderr
    report = optimization.grade(instance_id, tmp_path)
    assert report.accepted, report.faults
    solved = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))["cost"]
    assert solved == pytest.approx(optimization.reference_optimum(instance_id))


def test_the_task_as_given_is_rejected(tmp_path: Path) -> None:
    optimization.materialize(EXEMPLAR, tmp_path)

    run_model(tmp_path)

    assert not optimization.grade(EXEMPLAR, tmp_path).accepted


# --- What is rejected --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instance_id", "broken"),
    [(CHANGED_DEMAND, Property.FEASIBILITY), (CHANGED_CAPACITY, Property.CAPACITY)],
)
def test_the_exemplar_answer_is_rejected_where_demand_or_capacity_changed(instance_id: str, broken: Property) -> None:
    # What a hard-coded answer gives on every instance: the exemplar's own plan and table.
    report = optimization.grade(instance_id, optimization.reference_artifacts(EXEMPLAR))

    assert not report.accepted
    # Its table still agrees with it: the plan is what does not answer this instance.
    assert failed(report) == {broken, Property.OBJECTIVE}


def with_an_idle_setup(instance_id: str) -> dict[str, Any]:
    """The reference plan with one more setup, in a week with hours for it, and what that costs:
    nothing else changes, so it is feasible, whole and within capacity, and not the cheapest."""
    instance, plan = optimization.load_instance(instance_id), reference_plan(instance_id)
    for t, hours in enumerate(instance.machine_hours):
        used = sum(
            one.hours_per_unit * plan["production"][one.name][t] + one.setup_hours * plan["setups"][one.name][t]
            for one in instance.products
        )
        for one in instance.products:
            if plan["setups"][one.name][t] == 0 and used + one.setup_hours <= hours:
                plan["setups"][one.name][t] = 1
                plan["cost"] += one.setup_cost
                return plan
    raise AssertionError("no week has hours for one more setup")


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_feasible_plan_with_a_wrong_optimum_is_rejected_on_the_objective(tmp_path: Path, instance_id: str) -> None:
    costlier = with_an_idle_setup(instance_id)
    # The cheapest plan, said to cost what it does not, and its table agreeing with what it says.
    misstated = reference_plan(instance_id) | {"cost": optimization.reference_optimum(instance_id) - 1}

    assert failed(optimization.grade(instance_id, produced(tmp_path / "costlier", costlier))) == {Property.OBJECTIVE}
    report = optimization.grade(instance_id, produced(tmp_path / "misstated", misstated))
    assert failed(report) == {Property.OBJECTIVE}


def other_cost(plan: dict[str, Any]) -> str:
    return table(plan, plan["cost"] + 1)


def negated_cost(plan: dict[str, Any]) -> str:
    return table(plan, f"$-${plan['cost']}")


def negated_cost_in_words(plan: dict[str, Any]) -> str:
    return table(plan, f"\\textminus {plan['cost']}")


def other_units(plan: dict[str, Any]) -> str:
    changed = copy.deepcopy(plan)
    changed["production"][next(iter(plan["production"]))][2] += 1
    return table(changed, plan["cost"])


def swapped_columns(plan: dict[str, Any]) -> str:
    return table(plan, plan["cost"], headers=list(plan["production"])[::-1])


def missing_week(plan: dict[str, Any]) -> str:
    rows = table(plan, plan["cost"]).splitlines()
    return "\n".join(rows[:2] + rows[3:])


def cost_in_another_row(plan: dict[str, Any]) -> str:
    # The plan's cost under another name, and another cost as the total.
    rows = f"Budget & {plan['cost']} \\\\\nTotal cost & {plan['cost'] + 1}"
    return table(plan, plan["cost"]).replace(f"Total cost & {plan['cost']}", rows)


def total_beside_the_columns(plan: dict[str, Any]) -> str:
    # The label of the total spans every column the table has: none is left for the cost.
    label = f"\\multicolumn{{{len(plan['production']) + 1}}}{{l}}{{Total cost}}"
    return table(plan, plan["cost"]).replace("Total cost", label)


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
@pytest.mark.parametrize(
    "disagreeing",
    [
        other_cost,
        negated_cost,
        negated_cost_in_words,
        other_units,
        swapped_columns,
        missing_week,
        cost_in_another_row,
        total_beside_the_columns,
    ],
)
def test_a_table_that_disagrees_with_the_plan_is_rejected(tmp_path: Path, instance_id: str, disagreeing: Any) -> None:
    plan = reference_plan(instance_id)

    report = optimization.grade(instance_id, produced(tmp_path, plan, disagreeing(plan)))

    assert failed(report) == {Property.TABLE}


@pytest.mark.parametrize(
    ("plan", "broken"),
    [
        # Five bells made and five sold, and one said to be in stock.
        (twin_plan([5, 5], [1, 0], [1, 1]), Property.FEASIBILITY),
        # Bells made in a week the machine is not set up for them.
        (twin_plan([5, 5], [0, 0], [1, 0]), Property.FEASIBILITY),
        (twin_plan([5.5, 4.5], [0.5, 0], [1, 1]), Property.INTEGRALITY),
        (twin_plan([5, 5], [0, 0], [1, 2]), Property.INTEGRALITY),
        # Ten bells and a setup in a week of eight hours.
        (twin_plan([10, 0], [5, 0], [1, 0]), Property.CAPACITY),
    ],
)
def test_a_property_is_answered_on_its_own(tmp_path: Path, plan: dict[str, Any], broken: Property) -> None:
    assert failed(grader.grade(TWIN, TWIN_OPTIMUM, produced(tmp_path, plan))) == {broken}


@pytest.mark.parametrize(
    ("plan_json", "latex", "unanswered"),
    [
        (None, None, set(PROPERTIES)),
        ("not a plan", None, set(PROPERTIES)),
        (
            '{"cost": NaN, "production": {"bell": [5, 5]}, "stock": {"bell": [0, 0]}, "setups": {"bell": [1, 1]}}',
            None,
            set(PROPERTIES),
        ),
        (
            '{"cost": 20, "production": {"bell": [5, "5"]}, "stock": {"bell": [0, 0]}, "setups": {"bell": [1, 1]}}',
            None,
            set(PROPERTIES),
        ),
        ('{"cost": 20, "production": {"bell": [5, 5]}, "stock": {"bell": [0]}, "setups": {}}', None, set(PROPERTIES)),
        (json.dumps(twin_plan([5, 5], [0, 0], [1, 1])), None, {Property.TABLE}),
        (json.dumps(twin_plan([5, 5], [0, 0], [1, 1])), "bell: 5 and 5, cost 20", {Property.TABLE}),
        # Numbers too large to compare: of the table, and of the plan, whose cost is then none.
        (
            json.dumps(twin_plan([5, 5], [0, 0], [1, 1])),
            table(twin_plan([5, 5], [0, 0], [1, 1]), "9" * 400),
            {Property.TABLE},
        ),
        (json.dumps(twin_plan([1e308, -1e308], [1e308, 0], [1, 1])), None, set(PROPERTIES) - {Property.INTEGRALITY}),
    ],
)
def test_a_missing_or_unreadable_file_is_a_rejection(
    tmp_path: Path, plan_json: str | None, latex: str | None, unanswered: set[Property]
) -> None:
    for name, text in (("plan.json", plan_json), ("plan.tex", latex)):
        if text is not None:
            (tmp_path / name).write_text(text, encoding="utf-8")

    assert failed(grader.grade(TWIN, TWIN_OPTIMUM, tmp_path)) == unanswered


# --- What is accepted besides the reference ----------------------------------------------------


@pytest.mark.parametrize("made", [[5, 5], [6, 4], [7, 3]])
def test_every_optimal_plan_is_accepted(tmp_path: Path, made: list[float]) -> None:
    plan = twin_plan(made, [made[0] - 5, 0], [1, 1])

    report = grader.grade(TWIN, TWIN_OPTIMUM, produced(tmp_path, plan))

    assert report.accepted, report.faults


def test_a_solvers_rounding_is_within_the_tolerances_and_a_part_of_a_unit_is_not(tmp_path: Path) -> None:
    rounded = twin_plan([5.0000004, 4.9999997], [0.0000003, 0], [0.9999995, 1])
    part = twin_plan([5.001, 4.999], [0.001, 0], [1, 1])

    assert grader.grade(TWIN, TWIN_OPTIMUM, produced(tmp_path / "rounded", rounded)).accepted
    assert failed(grader.grade(TWIN, TWIN_OPTIMUM, produced(tmp_path / "part", part))) == {Property.INTEGRALITY}


def test_a_number_of_the_table_may_differ_by_the_table_tolerance_and_no_more(tmp_path: Path) -> None:
    plan = twin_plan([5, 5], [0, 0], [1, 1])
    at_the_tolerance = table({"production": {"bell": [5.005, 4.995]}}, "20.005")
    beyond = table({"production": {"bell": [5.006, 5]}}, "20")

    assert grader.grade(TWIN, TWIN_OPTIMUM, produced(tmp_path / "at", plan, at_the_tolerance)).accepted
    assert failed(grader.grade(TWIN, TWIN_OPTIMUM, produced(tmp_path / "beyond", plan, beyond))) == {Property.TABLE}


def test_the_table_shows_the_cost_the_plan_states_within_the_table_tolerance(tmp_path: Path) -> None:
    # A solver's cost: within the cost tolerance of what the weeks add up to, beyond the table's.
    plan = reference_plan(EXEMPLAR)
    plan["cost"] += 0.010
    weeks_cost = plan["cost"] - 0.010

    as_stated = optimization.grade(EXEMPLAR, produced(tmp_path / "stated", plan, table(plan, plan["cost"] - 0.004)))
    # Within the tolerance of what the weeks add up to, and not of what plan.json states.
    off = optimization.grade(EXEMPLAR, produced(tmp_path / "off", plan, table(plan, f"{weeks_cost - 0.004:.3f}")))

    assert as_stated.accepted, as_stated.faults
    assert failed(off) == {Property.TABLE}


def test_a_table_in_another_notation_is_accepted(tmp_path: Path) -> None:
    plan = reference_plan(EXEMPLAR)
    made = plan["production"]
    # Each way a row ends, the third with a comment against it.
    ends = [r"\\", r"\tabularnewline", r"\\% week 3", r"\\[2pt]", r"\\*", r"\\"]
    # The first week's number braced, after a rule that takes no argument.
    weeks = [f"\\hline {{{t + 1}}}" if t == 0 else str(t + 1) for t in range(6)]
    rows = [
        f"{weeks[t]} & {made['saddle'][t]} & $ {made['frame'][t]} $ & {made['wheel'][t]}.0 {ends[t]}" for t in range(6)
    ]
    # Other columns, rules, type and separators than the reference writes, and a heading above.
    latex = "\n".join(
        [
            r"\begin{table} \begin{tabular}[t]{@{}l *{2}{r} p{2cm}@{}} \toprule",
            r"& \multicolumn{3}{c}{Units made} \\",
            r"\textbf{Week} & \textbf{Saddles} & \textbf{Frames} & \textbf{Wheels} \\ \cmidrule(lr){2-4}",
            *rows,
            r"\midrule % the cost was 99999 in a draft",
            r"\multicolumn{3}{@{}l}{Total cost} & 10\,846 \\[2pt] \bottomrule",
            r"\end{tabular} \end{table}",
        ]
    )

    report = optimization.grade(EXEMPLAR, produced(tmp_path, plan, latex))

    assert report.accepted, report.faults
