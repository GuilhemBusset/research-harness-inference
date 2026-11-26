"""The parallel-analysis task of the course suite and its grader (`evaluate.course.analysis`).

The grader reads what a run wrote (`series_a.csv`, `series_b.csv`, `figure.svg`, `result.json`)
and the spans of the run's workers, so most tests copy the committed reference of an instance,
change one thing and grade it. `chart` draws a figure of an instance as a run might, in another
way than the reference: an equivalent figure is accepted, and a figure that misplaces a series or
a label is not. One test runs the reference solution in a scratch workspace.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evaluate import contracts
from evaluate import plan as planning
from evaluate.course import analysis
from evaluate.course.analysis import INSTANCE_IDS, PROPERTIES, Property, Span, grader

EXEMPLAR, CHANGED_DATA, CHANGED_COMPARISON = INSTANCE_IDS

# Two workers alive at once, as the trusted side observed them.
OVERLAPPING = (Span(0, 10), Span(2, 12))


def failed(report: analysis.Report) -> set[Property]:
    return {one for one in PROPERTIES if not report.holds(one)}


def reference_copy(folder: Path, instance_id: str) -> Path:
    shutil.copytree(analysis.reference_artifacts(instance_id), folder, dirs_exist_ok=True)
    return folder


def chart(
    instance: grader.Instance,
    *,
    path: bool = False,
    scale: float = 1.0,
    group: str = "",
    lines: tuple[str, str] = ("series_a", "series_b"),
    labels: tuple[str, str] | None = None,
    nudge: float = 0.0,
) -> str:
    """A line chart of an instance: 500 by 300 units of plot at (100, 50), times `scale`, its lines
    in a group of transform `group`, with these ids and axis labels; `nudge` moves the third point
    of series_a down by that part of the y extent of the plotted lines."""
    series = (instance.series_a, instance.series_b)
    low, high = min(min(one) for one in series), max(max(one) for one in series)
    x0, x1 = instance.x[0], instance.x[-1]

    def point(x: float, y: float) -> tuple[float, float]:
        return (scale * (100 + 500 * (x - x0) / (x1 - x0)), scale * (350 - 300 * (y - low) / (high - low)))

    drawn = []
    for identifier, values in zip(lines, series, strict=True):
        points = [point(x, y) for x, y in zip(instance.x, values, strict=True)]
        if identifier == "series_a":
            points[2] = (points[2][0], points[2][1] + nudge * 300 * scale)
        if path:
            d = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in points)
            drawn.append(f'<path id="{identifier}" d="{d}"/>')
        else:
            drawn.append(f'<polyline id="{identifier}" points="{" ".join(f"{x:.3f},{y:.3f}" for x, y in points)}"/>')
    x_label, y_label = labels or (instance.x_label, instance.y_label)
    return "\n".join(
        [
            '<svg xmlns="http://www.w3.org/2000/svg">',
            f'<g transform="{group}">',
            *drawn,
            "</g>",
            f'<text x="{620 * scale}" y="{60 * scale}">{instance.name_a}</text>',
            f'<text x="{620 * scale}" y="{80 * scale}">{instance.name_b}</text>',
            f'<text x="{350 * scale}" y="{390 * scale}">{x_label}</text>',
            f'<text transform="translate({30 * scale} {200 * scale}) rotate(-90)">{y_label}</text>',
            "</svg>",
        ]
    )


def graded_with_figure(folder: Path, instance_id: str, svg: str) -> analysis.Report:
    produced = reference_copy(folder, instance_id)
    (produced / "figure.svg").write_text(svg, encoding="utf-8")
    return analysis.grade(instance_id, produced, OVERLAPPING)


# --- The family: its instances, what a trial is given, and the suite ---------------------------


def test_the_suite_names_the_instances_and_the_files_they_are_made_of() -> None:
    suite = planning.load_suite("course-v1").suite
    task = next(task for task in suite.tasks if task.task_id == analysis.TASK_ID)

    assert task.required_properties == len(PROPERTIES)
    assert [(one.instance_id, one.fixture_digest, one.grader_digest) for one in task.instances] == [
        (one, analysis.fixture_digest(one), analysis.grader_digest(one)) for one in INSTANCE_IDS
    ]
    assert len({analysis.fixture_digest(one) for one in INSTANCE_IDS}) == len(INSTANCE_IDS)


def test_the_exemplar_is_the_s3_figure_and_the_other_instances_change_it() -> None:
    exemplar, data, comparison = (analysis.load_instance(one) for one in INSTANCE_IDS)

    assert exemplar.series_a == tuple(x * x for x in exemplar.x)
    assert exemplar.series_b == tuple(10 * x for x in exemplar.x)
    assert all(one.delegate for one in (exemplar, data, comparison))
    assert data.series_a != exemplar.series_a and data.comparison is exemplar.comparison
    assert comparison.series_a != exemplar.series_a and comparison.comparison is not exemplar.comparison


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_a_workspace_holds_the_task_and_nothing_of_the_reference(tmp_path: Path, instance_id: str) -> None:
    analysis.materialize(instance_id, tmp_path)

    assert {file.name for file in tmp_path.iterdir()} == {"TASK.md", "data.csv", "instance.json"}
    assert not any(source.is_relative_to(analysis.SEALED) for source in analysis.workspace_files(instance_id).values())


# --- The references ----------------------------------------------------------------------------


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_committed_reference_is_accepted(instance_id: str) -> None:
    report = analysis.grade(instance_id, analysis.reference_artifacts(instance_id), OVERLAPPING)

    assert report.accepted, report.faults
    assert contracts.Grade(properties=report.properties, within_limits=True).accepted


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_the_reference_solution_makes_the_committed_artifacts(tmp_path: Path, instance_id: str) -> None:
    analysis.materialize(instance_id, tmp_path)

    result = subprocess.run(
        [sys.executable, "-I", str(analysis.REFERENCE), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for name in analysis.PRODUCED:
        assert (tmp_path / name).read_bytes() == (analysis.reference_artifacts(instance_id) / name).read_bytes()


@pytest.mark.parametrize("instance_id", [CHANGED_DATA, CHANGED_COMPARISON])
def test_the_exemplar_answer_is_rejected_where_the_input_changed(instance_id: str) -> None:
    report = analysis.grade(instance_id, analysis.reference_artifacts(EXEMPLAR), OVERLAPPING)

    assert failed(report) == {Property.DATA, Property.COMPARISON}


# --- The workers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workers",
    [
        pytest.param((Span(0, 5), Span(5, 9)), id="back-to-back"),
        pytest.param((Span(0, 4), Span(6, 9), Span(9.5, 12)), id="serial"),
        pytest.param((Span(0, 12),), id="one-worker"),
        pytest.param((), id="no-worker"),
        pytest.param(None, id="no-evidence"),
        pytest.param((Span(0, 10),) * (grader.MAX_WORKERS + 1), id="beyond-the-bound"),
    ],
)
def test_workers_that_were_not_alive_at_once_are_rejected(workers: tuple[Span, ...] | None) -> None:
    report = analysis.grade(EXEMPLAR, analysis.reference_artifacts(EXEMPLAR), workers)

    assert failed(report) == {Property.WORKERS}


def test_overlap_is_found_among_serial_workers_and_up_to_the_bound() -> None:
    workers = (Span(0, 3), Span(4, 6), Span(5, 8)) + (Span(10, 11),) * (grader.MAX_WORKERS - 3)

    assert analysis.grade(EXEMPLAR, analysis.reference_artifacts(EXEMPLAR), workers).accepted


def test_a_prompt_without_delegation_needs_no_workers() -> None:
    alone = dataclasses.replace(analysis.load_instance(EXEMPLAR), delegate=False)

    assert grader.grade(alone, analysis.reference_artifacts(EXEMPLAR), None).accepted


@pytest.mark.parametrize(("start", "end"), [(5, 4), (-1, 2), (0, float("nan")), (0, float("inf"))])
def test_a_span_that_is_none_is_the_trusted_sides_error(start: float, end: float) -> None:
    with pytest.raises(ValueError, match="span"):
        Span(start, end)


# --- The data, the figure and the comparison --------------------------------------------------


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_swapped_series_are_rejected(tmp_path: Path, instance_id: str) -> None:
    csvs = reference_copy(tmp_path / "csv", instance_id)
    (csvs / "series_a.csv").rename(csvs / "swap")
    (csvs / "series_b.csv").rename(csvs / "series_a.csv")
    (csvs / "swap").rename(csvs / "series_b.csv")
    instance = analysis.load_instance(instance_id)

    assert failed(analysis.grade(instance_id, csvs, OVERLAPPING)) == {Property.DATA}
    swapped_lines = chart(instance, lines=("series_b", "series_a"))
    assert failed(graded_with_figure(tmp_path / "svg", instance_id, swapped_lines)) == {Property.DATA}


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_swapped_axis_labels_are_rejected(tmp_path: Path, instance_id: str) -> None:
    instance = analysis.load_instance(instance_id)
    swapped = chart(instance, labels=(instance.y_label, instance.x_label))

    assert failed(graded_with_figure(tmp_path, instance_id, swapped)) == {Property.DATA}


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
@pytest.mark.parametrize("conflict", ["value", "comparison"])
def test_a_reported_comparison_that_conflicts_with_the_input_is_rejected(
    tmp_path: Path, instance_id: str, conflict: str
) -> None:
    produced = reference_copy(tmp_path, instance_id)
    reported = json.loads((produced / "result.json").read_text(encoding="utf-8"))
    if conflict == "value":
        reported["value"] += 1
    else:
        reported["comparison"] = next(one.value for one in grader.Comparison if one != reported["comparison"])
    (produced / "result.json").write_text(json.dumps(reported), encoding="utf-8")

    assert failed(analysis.grade(instance_id, produced, OVERLAPPING)) == {Property.COMPARISON}


@pytest.mark.parametrize(
    "figure",
    [
        pytest.param({"path": True}, id="path"),
        pytest.param({"scale": 2.5}, id="another-scale"),
        pytest.param({"group": "translate(60 30) scale(0.9)"}, id="transformed-group"),
    ],
)
@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_an_equivalent_figure_is_accepted(tmp_path: Path, instance_id: str, figure: dict) -> None:
    report = graded_with_figure(tmp_path, instance_id, chart(analysis.load_instance(instance_id), **figure))

    assert report.accepted, report.faults


def test_an_axis_turned_around_is_rejected(tmp_path: Path) -> None:
    # The lines mirrored about their middle: y now grows downward.
    upside_down = chart(analysis.load_instance(EXEMPLAR), group="translate(0 400) scale(1 -1)")

    assert failed(graded_with_figure(tmp_path, EXEMPLAR, upside_down)) == {Property.DATA}


def test_the_tolerances_of_a_series_a_plotted_point_and_the_comparison(tmp_path: Path) -> None:
    instance = analysis.load_instance(EXEMPLAR)

    def graded(name: str, off: float) -> set[Property]:
        produced = reference_copy(tmp_path / f"{name}{off}", EXEMPLAR)
        if name == "series":
            rows = [f"{x:g},{y + off:g}" for x, y in zip(instance.x, instance.series_a, strict=True)]
            (produced / "series_a.csv").write_text("x,y\n" + "\n".join(rows) + "\n", encoding="utf-8")
        elif name == "comparison":
            result = {"comparison": instance.comparison.value, "value": instance.comparison_value() + off}
            (produced / "result.json").write_text(json.dumps(result), encoding="utf-8")
        else:
            (produced / "figure.svg").write_text(chart(instance, nudge=off), encoding="utf-8")
        return failed(analysis.grade(EXEMPLAR, produced, OVERLAPPING))

    assert graded("series", 0.004) == set()
    assert graded("series", 0.006) == {Property.DATA}
    assert graded("comparison", -0.004) == set()
    assert graded("comparison", 0.006) == {Property.COMPARISON}
    assert graded("plot", 0.009) == set()
    assert graded("plot", 0.02) == {Property.DATA}


@pytest.mark.parametrize(
    ("name", "text", "unanswered"),
    [
        ("figure.svg", None, {Property.DATA}),
        ("figure.svg", "<svg><polyline", {Property.DATA}),
        ("figure.svg", '<!DOCTYPE svg [<!ENTITY a "a">]><svg/>', {Property.DATA}),
        ("figure.svg", '<svg><path id="series_a" d="M 0 0 C 1 1 2 2 3 3"/></svg>', {Property.DATA}),
        ("series_b.csv", None, {Property.DATA}),
        ("figure.svg", "<svg>" + "<g>" * 5000 + "</g>" * 5000 + "</svg>", {Property.DATA}),
        ("series_b.csv", "x,y\n0,nan\n", {Property.DATA}),
        ("series_b.csv", "x,y\n0," + "1" * 200_000 + "\n", {Property.DATA}),
        ("result.json", None, {Property.COMPARISON}),
        ("result.json", "a sentence", {Property.COMPARISON}),
        ("result.json", '{"comparison": "largest_gap", "value": true}', {Property.COMPARISON}),
        ("result.json", '{"comparison": "largest_gap", "value": 1' + "0" * 400 + "}", {Property.COMPARISON}),
    ],
)
def test_a_missing_or_unreadable_file_is_a_rejection(
    tmp_path: Path, name: str, text: str | None, unanswered: set[Property]
) -> None:
    produced = reference_copy(tmp_path, EXEMPLAR)
    (produced / name).unlink()
    if text is not None:
        (produced / name).write_text(text, encoding="utf-8")

    assert failed(analysis.grade(EXEMPLAR, produced, OVERLAPPING)) == unanswered
