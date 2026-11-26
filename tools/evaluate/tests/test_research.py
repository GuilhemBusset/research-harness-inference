"""The grounded-research task of the course suite and its grader (`evaluate.course.research`).

The grader reads what a run wrote, `answer.json`, so most tests here change a committed reference
answer in one place, write it and grade it. Nothing here reaches a network: `offline` makes every
attempt of this process raise, and the search tool and the packet's builder run in it, from the
frozen files. One test runs the tool's launcher as a trial does, by its name.
"""

from __future__ import annotations

import copy
import json
import os
import runpy
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evaluate import contracts
from evaluate import plan as planning
from evaluate.course import research
from evaluate.course.research import INSTANCE_IDS, PROPERTIES, Property, grader

EXEMPLAR, CHANGED_CONDITION, CHANGED_PANEL = INSTANCE_IDS
BUILD_PACKET = Path(research.__file__).with_name("build_packet.py")
SEARCH = research.TOOLS["search"]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test of this module reaches a network: any attempt to resolve or connect raises."""

    def refused(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a test of the research task tried to reach a network")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)


def reference(instance_id: str) -> dict[str, Any]:
    return json.loads((research.reference_artifacts(instance_id) / "answer.json").read_text(encoding="utf-8"))


def written(folder: Path, answer: dict[str, Any] | str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    (folder / "answer.json").write_text(text, encoding="utf-8")
    return folder


def failed(report: research.Report) -> set[Property]:
    return {one for one in PROPERTIES if not report.holds(one)}


def changed(instance_id: str, change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """The reference answer of an instance with one change made to it."""
    answer = copy.deepcopy(reference(instance_id))
    change(answer)
    return answer


# --- The family: its instances, what a trial is given, and the suite ---------------------------


def test_the_suite_names_the_instances_and_the_files_they_are_made_of() -> None:
    suite = planning.load_suite("course-v1").suite
    task = next(task for task in suite.tasks if task.task_id == research.TASK_ID)

    assert task.required_properties == len(PROPERTIES)
    assert [(one.instance_id, one.fixture_digest, one.grader_digest) for one in task.instances] == [
        (one, research.fixture_digest(one), research.grader_digest(one)) for one in INSTANCE_IDS
    ]
    assert len({research.fixture_digest(one) for one in INSTANCE_IDS}) == len(INSTANCE_IDS)


def test_each_key_answers_its_question_and_the_instances_change_the_condition_and_the_panel() -> None:
    keys = {one: research.load_key(one) for one in INSTANCE_IDS}
    for instance_id, key in keys.items():
        name = instance_id.rpartition(".")[2]
        question = json.loads((research.HERE / "instances" / f"{name}.json").read_text(encoding="utf-8"))
        assert set(question["facts"]) == set(key.facts) == set(key.result.rests_on)
        assert question["result"]["fact"] == key.result.fact_id
        # The result is the thermal resistance times the conductivity times the density.
        conductivity, density = (key.facts[one].value for one in key.result.rests_on)
        assert key.result.value == pytest.approx(question["thermal_resistance"]["value"] * conductivity * density)

    exemplar, condition, panel = (set(keys[one].facts) for one in INSTANCE_IDS)
    assert len(exemplar & condition) == 1  # the same panel's density, another conductivity
    assert not exemplar & panel


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_workspace_holds_the_task_and_the_packet_and_nothing_sealed(tmp_path: Path, instance_id: str) -> None:
    research.materialize(instance_id, tmp_path)

    given = {str(file.relative_to(tmp_path)) for file in tmp_path.rglob("*") if file.is_file()}
    assert given == {"TASK.md", "instance.json", "packet/S1.pdf", "packet/S2.md", "packet/S3.md", "packet/S4.md"}
    sources = [*research.workspace_files(instance_id).values(), *(SEARCH / one for one in research.SEARCH_FILES)]
    assert not any(source.is_relative_to(sealed) for source in sources for sealed in research.SEALED)


def test_the_packet_and_its_index_are_built_again_byte_for_byte(tmp_path: Path) -> None:
    built = runpy.run_path(str(BUILD_PACKET))
    built["build"](tmp_path / "packet", tmp_path / "index.json")

    for file in research.workspace_files(EXEMPLAR):
        if file.startswith("packet/"):
            assert (tmp_path / file).read_bytes() == (research.PACKET / file.removeprefix("packet/")).read_bytes()
    assert (tmp_path / "index.json").read_bytes() == research.INDEX.read_bytes()
    pdf = (research.PACKET / "S1.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-") and pdf.count(b"/Type /Page ") == 3


# --- The search tool ---------------------------------------------------------------------------


def test_the_search_tool_finds_a_fact_in_the_frozen_packet_and_always_the_same() -> None:
    tool = runpy.run_path(str(SEARCH / "search.py"))
    packet = tool["load"]()

    found = tool["search"]("cork conductivity relative humidity", packet)

    assert tool["where"](found[0]) == "S1, page 2, Table 2"
    assert found == tool["search"]("cork conductivity relative humidity", packet)


def test_the_search_launcher_runs_by_its_name_on_the_path(tmp_path: Path) -> None:
    # As the trial's worker runs it: its folder on the PATH, and a `python3` (here, this one).
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").symlink_to(sys.executable)
    path = os.pathsep.join([str(tmp_path / "bin"), str(SEARCH), os.environ.get("PATH", "")])

    result = subprocess.run(
        ["search", "oven-dry", "density"],
        env={"PATH": path},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[S2, Table 1] Table 1. Oven-dry density" in result.stdout


# --- The references ----------------------------------------------------------------------------


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_committed_reference_is_accepted(instance_id: str) -> None:
    report = research.grade(instance_id, research.reference_artifacts(instance_id))

    assert report.accepted, report.faults
    assert contracts.Grade(properties=report.properties, within_limits=True).accepted


# --- What is rejected --------------------------------------------------------------------------


def relabelled(answer: dict[str, Any], instance_id: str) -> dict[str, Any]:
    """An answer with the identifiers of another instance: what a copy that only renamed gives."""
    key = research.load_key(instance_id)
    names = dict(zip(sorted({row["fact"] for row in answer["evidence"]}), sorted(key.facts), strict=True))
    for row in [*answer["evidence"], *answer["conclusion"]["citations"]]:
        row["fact"] = names[row["fact"]]
    answer["conclusion"]["fact"] = key.result.fact_id
    return answer


@pytest.mark.parametrize("instance_id", [CHANGED_CONDITION, CHANGED_PANEL])
def test_an_answer_copied_to_a_changed_instance_is_rejected(tmp_path: Path, instance_id: str) -> None:
    copied = research.grade(instance_id, research.reference_artifacts(EXEMPLAR))
    renamed = research.grade(instance_id, written(tmp_path, relabelled(reference(EXEMPLAR), instance_id)))

    assert not copied.accepted
    # Even under this instance's identifiers, the exemplar's values are not this instance's facts.
    assert Property.FACT in failed(renamed)


def cite_the_neighbouring_table(answer: dict[str, Any]) -> None:
    # Table 2 is on the same page as Table 1, with the same panels in the same unit.
    answer["evidence"][0]["table"] = "2"


def cite_the_next_page(answer: dict[str, Any]) -> None:
    answer["evidence"][0] |= {"page": 3, "table": "3"}


def cite_only_another_page(answer: dict[str, Any]) -> None:
    del answer["evidence"][0]["table"]
    answer["evidence"][0]["page"] = 1


def cite_the_neighbouring_table_in_the_conclusion(answer: dict[str, Any]) -> None:
    answer["conclusion"]["citations"][0]["table"] = "2"


def cite_the_density_from_the_moisture_table(answer: dict[str, Any]) -> None:
    answer["conclusion"]["citations"][1]["table"] = "2"


@pytest.mark.parametrize(
    "misplaced",
    [
        cite_the_neighbouring_table,
        cite_the_next_page,
        cite_only_another_page,
        cite_the_neighbouring_table_in_the_conclusion,
        cite_the_density_from_the_moisture_table,
    ],
)
def test_a_plausible_wrong_location_is_rejected_on_the_location_alone(
    tmp_path: Path, misplaced: Callable[[dict[str, Any]], None]
) -> None:
    report = research.grade(EXEMPLAR, written(tmp_path, changed(EXEMPLAR, misplaced)))

    assert failed(report) == {Property.LOCATION}


@pytest.mark.parametrize(
    "wrong_unit",
    [
        # A U-value's unit, for a conductivity: the right number in the wrong unit.
        lambda answer: answer["evidence"][0].update(unit="W/(m²·K)"),
        lambda answer: answer["evidence"][1].update(unit="kg/m²"),
        # The thermal resistance's unit, for the result.
        lambda answer: answer["conclusion"].update(unit="m²·K/W"),
        # Kelvin multiplied, not divided: the signs that group a unit count.
        lambda answer: answer["evidence"][0].update(unit="(W/m)·K"),
        lambda answer: answer["evidence"][0].update(unit="W·K/m"),
        # A parenthesized divisor ends with its parenthesis: kg/(m²)·m is kg/m, no density.
        lambda answer: answer["evidence"][1].update(unit="kg/(m²) × m"),
        # A prefix binds to its symbol: mg/m³ is a mass per volume, not a metre-gram per volume.
        lambda answer: answer["conclusion"].update(unit="mg/m³"),
    ],
)
def test_a_wrong_unit_is_rejected_on_the_unit_alone(
    tmp_path: Path, wrong_unit: Callable[[dict[str, Any]], None]
) -> None:
    for instance_id in INSTANCE_IDS:
        report = research.grade(instance_id, written(tmp_path / instance_id, changed(instance_id, wrong_unit)))

        assert failed(report) == {Property.UNIT}


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        # The humid conductivity of the same panel, given for the dry one: a wrong fact.
        (lambda answer: answer["evidence"][0].update(value=0.043), Property.FACT),
        # The result of another panel, with the right value: the conclusion states another fact.
        (lambda answer: answer["conclusion"].update(fact="mass_per_area.wood_fibre.dry"), Property.FACT),
        (lambda answer: answer["conclusion"].update(value=12.1), Property.FACT),
        # The review's and the supplier's tables are where the same table number gives a value.
        (lambda answer: answer["evidence"][0].update(source="S3", page=None), Property.SOURCE),
        (lambda answer: answer["conclusion"]["citations"][1].update(source="S4"), Property.SOURCE),
    ],
)
def test_a_property_is_answered_on_its_own(
    tmp_path: Path, change: Callable[[dict[str, Any]], object], broken: Property
) -> None:
    report = research.grade(EXEMPLAR, written(tmp_path, changed(EXEMPLAR, change)))

    assert failed(report) == {broken}


def test_a_row_of_a_fact_the_question_does_not_name_is_rejected_and_its_source_held_to_the_packet(
    tmp_path: Path,
) -> None:
    def add_a_row(answer: dict[str, Any]) -> None:
        answer["evidence"].append(
            {"fact": "density.cork.nominal", "value": 120, "unit": "kg/m³", "source": "S9", "table": "1"}
        )

    report = research.grade(EXEMPLAR, written(tmp_path, changed(EXEMPLAR, add_a_row)))

    assert failed(report) == {Property.FACT, Property.SOURCE}


def test_the_conclusion_shown_is_written_from_what_was_graded_and_no_prose_of_it_is_taken(tmp_path: Path) -> None:
    accepted = research.grade(EXEMPLAR, research.reference_artifacts(EXEMPLAR))
    with_prose = changed(
        EXEMPLAR,
        lambda answer: answer["conclusion"].update(statement="One square metre needs 110 kg of oven-dry panel."),
    )

    assert accepted.conclusion is not None
    assert accepted.conclusion.startswith("mass_per_area.cork.dry = 11 kg/m²")
    assert failed(research.grade(EXEMPLAR, written(tmp_path, with_prose))) == {Property.FACT}


def test_a_conclusion_that_cites_nothing_for_a_fact_it_rests_on_is_rejected(tmp_path: Path) -> None:
    uncited = changed(EXEMPLAR, lambda answer: answer["conclusion"]["citations"].pop())

    assert failed(research.grade(EXEMPLAR, written(tmp_path, uncited))) == {Property.SOURCE, Property.LOCATION}


@pytest.mark.parametrize(
    "text",
    [
        None,
        "not an answer",
        json.dumps({"evidence": [], "conclusion": {}}),
        json.dumps({"evidence": [{"fact": "density.cork", "value": "110", "unit": "kg/m³", "source": "S2"}]}),
        '{"evidence": [{"fact": "density.cork", "value": NaN, "unit": "kg/m³", "source": "S2", "table": "1"}]}',
    ],
)
def test_a_missing_or_unreadable_answer_is_a_rejection_of_every_property(tmp_path: Path, text: str | None) -> None:
    if text is not None:
        written(tmp_path, text)

    assert failed(research.grade(EXEMPLAR, tmp_path)) == set(PROPERTIES)


def test_a_page_that_is_no_number_is_a_rejection_not_an_error(tmp_path: Path) -> None:
    superscript = changed(EXEMPLAR, lambda answer: answer["evidence"][0].update(page="³"))

    assert not research.grade(EXEMPLAR, written(tmp_path, superscript)).accepted


def test_a_key_the_packet_does_not_bear_out_is_refused() -> None:
    raw = json.loads((research.KEYS / "i01.json").read_text(encoding="utf-8"))
    raw["facts"]["conductivity.cork.dry"]["locations"] = [{"page": 2, "table": "2"}]

    with pytest.raises(ValueError, match="does not print"):
        grader.Key.parse(raw, research.load_packet())


# --- What is accepted besides the reference ----------------------------------------------------


def in_other_units_and_notations(answer: dict[str, Any]) -> None:
    conductivity, density = answer["evidence"]
    conductivity |= {"value": conductivity["value"] * 1000, "unit": "mW m⁻¹ K⁻¹", "table": "Table 2"}
    del conductivity["page"]
    density |= {"value": density["value"] / 1000, "unit": "g/cm^3"}
    answer["conclusion"] |= {"value": round(answer["conclusion"]["value"] * 1000, -1), "unit": "g/m²"}
    answer["conclusion"]["citations"][0] = {"fact": conductivity["fact"], "source": "S1", "page": "2"}


@pytest.mark.parametrize(
    ("row", "unit"),
    [
        (1, "kg·m^−3"),  # a minus sign, U+2212
        (1, "kg m-3"),
        (1, "g/L"),
        (1, "mg/cm³"),  # a milligram per cubic centimetre is a kilogram per cubic metre
        (0, "W·m⁻¹·K⁻¹"),
        (0, "W / (m K)"),
        (0, "W/m/K"),
        (0, "W/mK"),
        (0, "mW/(m·mK)"),  # a milliwatt per metre per millikelvin: the prefixes cancel
        (None, "kg/(m³) × m"),  # a density times a thickness is a mass per area
    ],
)
def test_a_unit_in_another_notation_is_the_same_unit(tmp_path: Path, row: int | None, unit: str) -> None:
    def respelled(answer: dict[str, Any]) -> None:
        (answer["conclusion"] if row is None else answer["evidence"][row])["unit"] = unit

    report = research.grade(EXEMPLAR, written(tmp_path, changed(EXEMPLAR, respelled)))

    assert report.accepted, report.faults


def test_an_equivalent_answer_in_other_units_and_notations_is_accepted(tmp_path: Path) -> None:
    report = research.grade(
        CHANGED_CONDITION, written(tmp_path, changed(CHANGED_CONDITION, in_other_units_and_notations))
    )

    assert report.accepted, report.faults
