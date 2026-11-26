"""The parallel-analysis task of the course suite and its grader (`evaluate.course.analysis`).

The grader reads what a run wrote (`series_a.csv`, `series_b.csv`, `figure.svg`, `result.json`)
and the evidence of the run's workers (the spans of each, or what the trial's adapter read), so
most tests copy the committed reference of an instance, change one thing (`graded_with_edit`
edits its figure) and grade it. `chart` draws a figure of an instance as a run might, in another
way than the reference: an equivalent figure is accepted, and a figure that misplaces a series, a
label or a tick is not. One test runs the reference solution in a scratch workspace.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import pytest

from evaluate import adapters, contracts
from evaluate import plan as planning
from evaluate.course import analysis
from evaluate.course.analysis import INSTANCE_IDS, PROPERTIES, Property, Span, grader

EXEMPLAR, CHANGED_DATA, CHANGED_COMPARISON = INSTANCE_IDS

# Two workers alive at once, as the trusted side observed them.
OVERLAPPING = (Span(0, 10), Span(2, 12))

SVG = "{http://www.w3.org/2000/svg}"
ElementTree.register_namespace("", SVG[1:-1])
COLOURS = ("#1f77b4", "#d62728")


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
    """A line chart of an instance, its legend in the colours of the lines and a tick at each end
    of the y axis: 500 by 300 units of plot at (100, 50), times `scale`, its lines and ticks in a
    group of transform `group`, with these ids and axis labels. `nudge` moves every point but the
    first and the last right by that part of the x extent, and the middle one left by it."""
    series = (instance.series_a, instance.series_b)
    low, high = min(min(one) for one in series), max(max(one) for one in series)
    x0, x1 = instance.x[0], instance.x[-1]

    def point(x: float, y: float) -> tuple[float, float]:
        return (scale * (100 + 500 * (x - x0) / (x1 - x0)), scale * (350 - 300 * (y - low) / (high - low)))

    drawn = []
    for identifier, values, colour in zip(lines, series, COLOURS, strict=True):
        points = [point(x, y) for x, y in zip(instance.x, values, strict=True)]
        middle = len(points) // 2
        for index in range(1, len(points) - 1):
            moved = nudge * 500 * scale * (-1 if index == middle else 1)
            points[index] = (points[index][0] + moved, points[index][1])
        paint = f'stroke="{colour}" fill="none"'
        if path:
            d = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in points)
            drawn.append(f'<path id="{identifier}" d="{d}" {paint}/>')
        else:
            coordinates = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
            drawn.append(f'<polyline id="{identifier}" points="{coordinates}" {paint}/>')
    x_label, y_label = labels or (instance.x_label, instance.y_label)
    ticks = [f'<text x="{90 * scale}" y="{point(x0, y)[1] + 4 * scale}">{y:g}</text>' for y in (low, high)]
    return "\n".join(
        [
            '<svg xmlns="http://www.w3.org/2000/svg">',
            f'<g transform="{group}">',
            *drawn,
            *ticks,
            "</g>",
            f'<text x="{620 * scale}" y="{60 * scale}" fill="{COLOURS[0]}">{instance.name_a}</text>',
            f'<text x="{620 * scale}" y="{80 * scale}" style="fill: {COLOURS[1]}">{instance.name_b}</text>',
            f'<text x="{350 * scale}" y="{390 * scale}">{x_label}</text>',
            f'<text transform="translate({30 * scale} {200 * scale}) rotate(-90)">{y_label}</text>',
            "</svg>",
        ]
    )


def graded_with_figure(folder: Path, instance_id: str, svg: str) -> analysis.Report:
    produced = reference_copy(folder, instance_id)
    (produced / "figure.svg").write_text(svg, encoding="utf-8")
    return analysis.grade(instance_id, produced, OVERLAPPING)


def graded_with_edit(folder: Path, instance_id: str, edit: Callable[[ElementTree.Element, grader.Instance], None]):
    """The grade of the reference of an instance, its figure edited in place."""
    produced = reference_copy(folder, instance_id)
    tree = ElementTree.parse(produced / "figure.svg")
    edit(tree.getroot(), analysis.load_instance(instance_id))
    tree.write(produced / "figure.svg", encoding="unicode")
    return analysis.grade(instance_id, produced, OVERLAPPING)


def texts(root: ElementTree.Element) -> dict[str, ElementTree.Element]:
    return {element.text or "": element for element in root.iter(SVG + "text")}


def series_lines(root: ElementTree.Element) -> list[ElementTree.Element]:
    return [element for element in root.iter() if element.get("id") in grader.LINE_IDS]


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


def claude_output(*events: tuple[str, str]) -> str:
    """Claude Code's output of workers starting and ending (`adapters.delegation` reads it)."""
    return "\n".join(
        json.dumps({"type": "system", "subtype": kind, "task_type": adapters.AGENT_TASK, "tool_use_id": call})
        for kind, call in events
    )


@pytest.mark.parametrize(
    ("events", "accepted"),
    [
        ([("task_started", "a"), ("task_started", "b"), ("task_notification", "a"), ("task_notification", "b")], True),
        ([("task_started", "a"), ("task_notification", "a"), ("task_started", "b"), ("task_notification", "b")], False),
    ],
)
def test_the_delegation_a_trial_read_is_graded(events: list[tuple[str, str]], accepted: bool) -> None:
    evidence = adapters.delegation(claude_output(*events))

    report = analysis.grade(EXEMPLAR, analysis.reference_artifacts(EXEMPLAR), evidence)

    assert failed(report) == (set() if accepted else {Property.WORKERS})
    beyond = adapters.Delegation(grader.MAX_WORKERS + 1, overlapped=True)
    assert failed(analysis.grade(EXEMPLAR, analysis.reference_artifacts(EXEMPLAR), beyond)) == {Property.WORKERS}


@pytest.mark.parametrize(("workers", "overlapped"), [(1, True), (-1, False)])
def test_a_delegation_that_is_none_is_the_trusted_sides_error(workers: int, overlapped: bool) -> None:
    with pytest.raises(ValueError, match="delegation"):
        analysis.grade(EXEMPLAR, analysis.reference_artifacts(EXEMPLAR), adapters.Delegation(workers, overlapped))


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

    def swap_legend(root: ElementTree.Element, instance: grader.Instance) -> None:
        named = texts(root)
        named[instance.name_a].text, named[instance.name_b].text = instance.name_b, instance.name_a

    assert failed(graded_with_edit(tmp_path / "legend", instance_id, swap_legend)) == {Property.DATA}


def test_a_legend_that_cannot_tell_the_lines_apart_is_rejected(tmp_path: Path) -> None:
    def one_colour(root: ElementTree.Element, _instance: grader.Instance) -> None:
        for element in root.iter():
            if element.get("stroke") == COLOURS[1]:
                element.set("stroke", COLOURS[0])

    assert failed(graded_with_edit(tmp_path, EXEMPLAR, one_colour)) == {Property.DATA}


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("display", "none"),
        ("visibility", "hidden"),
        ("stroke", "none"),
        ("opacity", "0"),
        ("style", "stroke-width: 0"),
    ],
)
def test_a_series_that_is_not_drawn_is_rejected(tmp_path: Path, attribute: str, value: str) -> None:
    def hide(root: ElementTree.Element, _instance: grader.Instance) -> None:
        for line in series_lines(root):
            line.set(attribute, value)

    assert failed(graded_with_edit(tmp_path, EXEMPLAR, hide)) == {Property.DATA}


def cover_plot(root: ElementTree.Element, _instance: grader.Instance) -> None:
    """An opaque rectangle, the colour of the page, painted over the plot after everything."""
    ElementTree.SubElement(root, SVG + "rect", {"x": "60", "y": "20", "width": "460", "height": "330", "fill": "#fff"})


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda root, _instance: root.set("viewBox", "640 400 640 400"), id="viewport-elsewhere"),
        pytest.param(cover_plot, id="covered"),
        pytest.param(
            lambda root, _instance: [line.set("stroke", "white") for line in series_lines(root)],
            id="in-the-background-colour",
        ),
    ],
)
def test_a_series_the_figure_does_not_show_is_rejected(tmp_path: Path, edit: Callable) -> None:
    assert failed(graded_with_edit(tmp_path, EXEMPLAR, edit)) == {Property.DATA}


@pytest.mark.parametrize("size", [("640", "400"), ("100%", "100%"), ("50%", "100%")])
@pytest.mark.parametrize("covered", [False, True])
def test_a_rectangle_sized_in_percent_is_the_size_it_is_drawn(tmp_path: Path, covered: bool, size: tuple) -> None:
    """A full-canvas rectangle hides the plot when painted over it, and is the background of light
    lettering when painted under it, however its size is written; half of it is half."""
    whole = size[0] != "50%"

    def paint(root: ElementTree.Element, _instance: grader.Instance) -> None:
        if covered:
            rect = ElementTree.SubElement(root, SVG + "rect", {"fill": "white"})
        else:
            rect = next(root.iter(SVG + "rect"))
            rect.set("fill", "black")
            root.set("fill", "white")
            for line in root.iter(SVG + "line"):
                if line.get("stroke") == "black":
                    line.set("stroke", "white")
        rect.set("width", size[0])
        rect.set("height", size[1])

    # Half the canvas covers some of the plot, or leaves some of the white lettering on white.
    assert failed(graded_with_edit(tmp_path, EXEMPLAR, paint)) == ({Property.DATA} if covered or not whole else set())


@pytest.mark.parametrize(
    "paint",
    [
        {"fill": "none", "stroke": "none"},
        {"style": "fill-opacity: 0"},
        {"font-size": "0"},
        {"fill": "rgb(255, 255, 255)"},
    ],
)
def test_an_axis_label_that_is_not_seen_is_rejected(tmp_path: Path, paint: dict[str, str]) -> None:
    def unseen(root: ElementTree.Element, instance: grader.Instance) -> None:
        texts(root)[instance.x_label].attrib.update(paint)

    assert failed(graded_with_edit(tmp_path, EXEMPLAR, unseen)) == {Property.DATA}


@pytest.mark.parametrize(
    ("line", "mark"),
    [("blue", "#0000ff"), ("rgb(0, 0, 255)", "#00F"), ("hsl(240, 100%, 50%)", "Blue"), ("#0000ffff", "blue")],
)
def test_one_colour_spelt_two_ways_is_one_colour(tmp_path: Path, line: str, mark: str) -> None:
    def respell(root: ElementTree.Element, _instance: grader.Instance) -> None:
        for element in root.iter():
            if element.get("stroke") == COLOURS[0]:
                element.set("stroke", line if element.get("id") == "series_a" else mark)

    report = graded_with_edit(tmp_path, EXEMPLAR, respell)

    assert report.accepted, report.faults


@pytest.mark.parametrize(
    "viewport",
    [
        {"viewBox": "0 0 640 400", "width": "1280", "height": "800"},
        {"viewBox": "-20 -20 700 440", "preserveAspectRatio": "xMinYMin slice", "width": "640", "height": "400"},
        {"width": "100%"},
    ],
)
def test_a_viewport_that_shows_the_whole_chart_is_accepted(tmp_path: Path, viewport: dict[str, str]) -> None:
    report = graded_with_edit(tmp_path, EXEMPLAR, lambda root, _instance: root.attrib.update(viewport))

    assert report.accepted, report.faults


def nest(root: ElementTree.Element, size: dict[str, str]) -> ElementTree.Element:
    """Moves everything the figure draws into an `svg` of this size inside it."""
    inner = ElementTree.Element(SVG + "svg", size)
    inner.extend(list(root))
    root[:] = [inner]
    return inner


@pytest.mark.parametrize("where", ["outer-width", "outer-height", "nested-width"])
def test_a_viewport_zero_percent_wide_or_high_shows_nothing(tmp_path: Path, where: str) -> None:
    def zero(root: ElementTree.Element, _instance: grader.Instance) -> None:
        svg = nest(root, {}) if where.startswith("nested") else root
        svg.set(where.partition("-")[2], "0%")

    assert failed(graded_with_edit(tmp_path, EXEMPLAR, zero)) == {Property.DATA}


@pytest.mark.parametrize("clip", ["100%", "50%"])
def test_a_nested_viewport_that_sets_no_size_is_the_viewport_around_it(tmp_path: Path, clip: str) -> None:
    """Its percentages, the background's and those of a clip of the lines, are of the viewport
    around it, as with an explicit `100%`: a clip of half its width hides some of the plot."""

    def graded(size: dict[str, str]) -> analysis.Report:
        def nested(root: ElementTree.Element, _instance: grader.Instance) -> None:
            inner = nest(root, size)
            inner[0].attrib.update({"width": "100%", "height": "100%"})
            clip_path = ElementTree.SubElement(ElementTree.SubElement(inner, SVG + "defs"), SVG + "clipPath")
            clip_path.set("id", "plot")
            ElementTree.SubElement(clip_path, SVG + "rect", {"width": clip, "height": "100%"})
            for line in series_lines(inner):
                line.set("clip-path", "url(#plot)")

        return graded_with_edit(tmp_path / str(len(size)), EXEMPLAR, nested)

    explicit, unset = graded({"width": "100%", "height": "100%"}), graded({})

    assert unset.faults == explicit.faults
    assert failed(unset) == (set() if clip == "100%" else {Property.DATA})


def test_a_plot_clipped_to_its_area_is_accepted(tmp_path: Path) -> None:
    def clipped(root: ElementTree.Element, _instance: grader.Instance) -> None:
        clip = ElementTree.SubElement(ElementTree.SubElement(root, SVG + "defs"), SVG + "clipPath", {"id": "plot"})
        ElementTree.SubElement(clip, SVG + "rect", {"x": "70", "y": "20", "width": "440", "height": "330"})
        for line in series_lines(root):
            line.set("clip-path", "url(#plot)")

    report = graded_with_edit(tmp_path, EXEMPLAR, clipped)

    assert report.accepted, report.faults


def test_a_path_of_points_not_joined_is_rejected(tmp_path: Path) -> None:
    def unjoined(root: ElementTree.Element, _instance: grader.Instance) -> None:
        for line in series_lines(root):
            line.tag = SVG + "path"
            line.set("d", " ".join("M" + pair for pair in line.attrib.pop("points").split()))

    assert failed(graded_with_edit(tmp_path, EXEMPLAR, unjoined)) == {Property.DATA}


@pytest.mark.parametrize("instance_id", INSTANCE_IDS)
def test_ticks_that_misstate_an_axis_are_rejected(tmp_path: Path, instance_id: str) -> None:
    def tenfold(root: ElementTree.Element, _instance: grader.Instance) -> None:
        for element in root.iter(SVG + "text"):
            if element.text and element.text.isdecimal():
                element.text = str(10 * int(element.text))

    assert failed(graded_with_edit(tmp_path, instance_id, tenfold)) == {Property.DATA}


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


def test_a_label_placed_by_its_tspan_is_accepted(tmp_path: Path) -> None:
    def in_tspan(root: ElementTree.Element, instance: grader.Instance) -> None:
        label = texts(root)[instance.x_label]
        position = {key: label.attrib.pop(key) for key in ("x", "y")}
        ElementTree.SubElement(label, SVG + "tspan", position).text = label.text
        label.text = None

    report = graded_with_edit(tmp_path, EXEMPLAR, in_tspan)

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
    assert graded("plot", 0.011) == {Property.DATA}


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
