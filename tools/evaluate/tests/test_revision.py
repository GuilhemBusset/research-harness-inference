"""The debug-and-revise task of the course suite and its grader (`evaluate.course.revision`).

The grader reads what the final program wrote on each sealed case, so most tests here run a
program on every case of an instance, in a scratch folder and in a second interpreter as a worker
would, and grade the folder of its outputs. The programs are the reference repairs, which must be
accepted, the solvers as given, which must not, and small solvers by enumeration that each get one
thing wrong.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evaluate import contracts
from evaluate import plan as planning
from evaluate.course import revision
from evaluate.course.revision import INSTANCE_IDS, PROPERTIES, Property, grader

# A solver by enumeration, with LIMIT replaced by the expression of the limit it applies.
ENUMERATION = """\
import itertools, json, sys
case = json.load(open(sys.argv[1]))
limit = LIMIT
best = (0, [])
for count in range(len(case["parcels"]) + 1):
    for combo in itertools.combinations(case["parcels"], count):
        if (limit is None or count <= limit) and sum(p["weight"] for p in combo) <= case["capacity"]:
            if sum(p["fee"] for p in combo) > best[0]:
                best = (sum(p["fee"] for p in combo), [p["id"] for p in reversed(combo)])
json.dump({"chosen": best[1], "value": best[0]}, open(sys.argv[2], "w"))
"""


def enumeration(folder: Path, limit: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "solver.py").write_text(ENUMERATION.replace("LIMIT", limit), encoding="utf-8")
    return folder / "solver.py"


def run(instance_id: str, solver: Path, scratch: Path) -> Path:
    """Run a program on every case of an instance, as a worker would: the folder of its outputs."""
    inputs, workspace, outputs = scratch / "inputs", scratch / "workspace", scratch / "outputs"
    workspace.mkdir(parents=True)
    outputs.mkdir()
    shutil.copyfile(solver, workspace / revision.SOLVER)
    for name, case in revision.write_inputs(instance_id, inputs).items():
        _python, *arguments = revision.command(str(case), str(outputs / f"{name}.json"))
        subprocess.run(
            [sys.executable, *arguments], cwd=workspace, capture_output=True, text=True, timeout=60, check=False
        )
    return outputs


def failed(report: revision.Report) -> set[Property]:
    return {one for one in PROPERTIES if not report.holds(one)}


def best(case: revision.Case) -> float:
    """The optimum of a case, by trying every choice of its parcels."""
    return max(
        sum(one.fee for one in combo)
        for count in range(len(case.parcels) + 1)
        if case.max_items is None or count <= case.max_items
        for combo in itertools.combinations(case.parcels, count)
        if sum(one.weight for one in combo) <= case.capacity
    )


# --- The family: its instances, what a trial is given and asked, and the suite -----------------


def test_the_suite_names_the_instances_and_the_files_they_are_made_of() -> None:
    task = next(one for one in planning.load_suite("course-v1").suite.tasks if one.task_id == revision.TASK_ID)

    assert task.required_properties == len(PROPERTIES)
    assert [(one.instance_id, one.fixture_digest, one.grader_digest) for one in task.instances] == [
        (one, revision.fixture_digest(one), revision.grader_digest(one)) for one in INSTANCE_IDS
    ]
    assert len({revision.fixture_digest(one) for one in INSTANCE_IDS}) == len(INSTANCE_IDS)


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_workspace_holds_the_broken_solver_and_the_exemplar_and_nothing_sealed(
    tmp_path: Path, instance_id: str
) -> None:
    revision.materialize(instance_id, tmp_path)

    assert {file.name for file in tmp_path.iterdir()} == {"solver.py", "case.json"}
    assert (tmp_path / "solver.py").read_bytes() != revision.reference_solver(instance_id).read_bytes()
    exemplar = revision.Case.parse(json.loads((tmp_path / "case.json").read_text(encoding="utf-8")))
    assert exemplar == revision.load_cases(instance_id)["original"].case
    sources = revision.workspace_files(instance_id).values()
    assert not any(source.is_relative_to(sealed) for source in sources for sealed in revision.SEALED)


def test_the_follow_up_is_asked_after_the_fix() -> None:
    fix, follow_up = revision.prompts()

    assert "max_items" not in fix
    assert "max_items" in follow_up


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_each_committed_optimum_is_the_best_choice_and_the_follow_up_binds(instance_id: str) -> None:
    cases = revision.load_cases(instance_id)

    for name, sealed in cases.items():
        assert sealed.optimum == pytest.approx(best(sealed.case)), name
    assert cases["changed"].case != cases["original"].case
    # The limit changes the answer on both cases: a solver that ignores it is wrong there.
    for unlimited in ("original", "changed"):
        limited = cases[f"follow-up-{unlimited}"]
        assert limited.case.max_items is not None
        assert limited.case.parcels == cases[unlimited].case.parcels
        assert limited.optimum < cases[unlimited].optimum


# --- The references and the solvers as given ---------------------------------------------------


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_reference_repair_is_accepted(tmp_path: Path, instance_id: str) -> None:
    report = revision.grade(instance_id, run(instance_id, revision.reference_solver(instance_id), tmp_path))

    assert report.accepted, report.faults
    assert contracts.Grade(properties=report.properties, within_limits=True).accepted


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_solver_as_given_is_rejected_on_the_original_case(tmp_path: Path, instance_id: str) -> None:
    given = revision.workspace_files(instance_id)["solver.py"]

    assert Property.ORIGINAL in failed(revision.grade(instance_id, run(instance_id, given, tmp_path)))


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_another_optimal_choice_from_another_program_is_accepted(tmp_path: Path, instance_id: str) -> None:
    solver = enumeration(tmp_path / "program", 'case.get("max_items")')

    report = revision.grade(instance_id, run(instance_id, solver, tmp_path / "run"))

    assert report.accepted, report.faults


# --- What is rejected --------------------------------------------------------------------------


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_an_original_only_repair_is_rejected_on_the_follow_up(tmp_path: Path, instance_id: str) -> None:
    solver = enumeration(tmp_path / "program", "None")

    assert failed(revision.grade(instance_id, run(instance_id, solver, tmp_path / "run"))) == {Property.FOLLOW_UP}


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_hard_coded_exemplar_is_rejected_on_the_changed_data(tmp_path: Path, instance_id: str) -> None:
    # The reference's answers on the exemplar, with and without the limit, whatever the case.
    exemplar = run(instance_id, revision.reference_solver(instance_id), tmp_path / "exemplar")
    answers = {
        limited: json.loads((exemplar / f"{name}.json").read_text(encoding="utf-8"))
        for limited, name in ((True, "follow-up-original"), (False, "original"))
    }
    program = tmp_path / "program" / "solver.py"
    program.parent.mkdir()
    program.write_text(
        f"import json, sys\nanswers = {answers!r}\n"
        "json.dump(answers['max_items' in json.load(open(sys.argv[1]))], open(sys.argv[2], 'w'))\n",
        encoding="utf-8",
    )

    report = revision.grade(instance_id, run(instance_id, program, tmp_path / "run"))

    assert report.holds(Property.ORIGINAL)
    assert not report.holds(Property.CHANGED)


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_follow_up_that_breaks_the_original_case_is_rejected(tmp_path: Path, instance_id: str) -> None:
    # The follow-up's limit applied to every case, also to one that carries none.
    limit = revision.load_cases(instance_id)["follow-up-original"].case.max_items
    solver = enumeration(tmp_path / "program", f'case.get("max_items", {limit})')

    report = revision.grade(instance_id, run(instance_id, solver, tmp_path / "run"))

    assert Property.ORIGINAL in failed(report)
    assert report.holds(Property.FOLLOW_UP)


def test_the_value_is_recomputed_from_the_parcels_chosen(tmp_path: Path) -> None:
    instance_id = INSTANCE_IDS[0]
    outputs = run(instance_id, revision.reference_solver(instance_id), tmp_path)
    cases = revision.load_cases(instance_id)
    # The optimum stated, with one parcel fewer than earns it; and a case left with no output.
    answer = json.loads((outputs / "changed.json").read_text(encoding="utf-8"))
    answer["chosen"].pop()
    (outputs / "changed.json").write_text(json.dumps(answer | {"value": cases["changed"].optimum}), encoding="utf-8")
    (outputs / "follow-up-changed.json").unlink()

    report = grader.grade(cases, outputs)

    assert failed(report) == {Property.CHANGED, Property.FOLLOW_UP}
    assert len([fault for broken, fault in report.faults if broken is Property.CHANGED]) == 2
