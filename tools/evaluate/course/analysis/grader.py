"""The trusted grader of the parallel-analysis task: what a run produced, against its instance.

It reads four files a run wrote, `series_a.csv` and `series_b.csv` (the two computed series),
`figure.svg` (their line chart) and `result.json` (the named comparison), with the lifecycle of
the workers the run delegated to, and answers the three properties of `PROPERTIES`, in that
order, each on its own:

- *workers*: when the instance's prompt requires delegation, the evidence shows at least two
  workers alive at once: each started before the other ended. Workers one after the other,
  however close, a single worker, no evidence and evidence beyond `MAX_WORKERS` are faults. An
  instance that requires no delegation holds it without evidence;
- *data*: each series holds, for every `x` of the input, its running total; the figure draws
  each series as one line (the element of id `series_a`, and of `series_b`) whose points, in the
  order of `x`, are the series under one linear map of each axis, with `x` to the right and `y`
  upward; the label of the x axis is written below the plotted lines and the one of the y axis
  to their left; and a legend names both series;
- *comparison*: the comparison `result.json` reports is the one the instance names, and its value
  is what the input gives.

It runs nothing a run submitted and compares what a run wrote with the instance, never with the
reference artifacts: a figure of another size, scale, margin or drawing (a polyline or a path,
in a transformed group) is accepted when it shows the same.

**Lifecycle evidence** is the trusted side's: the span of each delegated worker, from its first
event to its last, on one clock of the run (`Span`), as `adapters.delegation` reads them from
Claude Code's output. It is bounded: a trial has at most 32 model turns across its agent tree, and
each worker takes at least one, so evidence of more than `MAX_WORKERS` workers is no evidence. A
span that is not a span (not finite, negative, ending before it starts) is the trusted side's
error, and raises.

**Tolerances.** A number of a series is a rounded one: it may differ from the running total by
`SERIES_TOLERANCE` (absolute). A plotted point may sit off the linear map of an axis by
`PLOT_TOLERANCE` of the extent of the plotted points on that axis. The reported value may differ
from the input's by `COMPARISON_TOLERANCE` (absolute).

A file that is missing or cannot be read is a rejection, with the fault said: `grade` raises only
on what the trusted side gave it.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

SERIES_A = "series_a.csv"
SERIES_B = "series_b.csv"
FIGURE = "figure.svg"
RESULT = "result.json"
LINE_IDS = ("series_a", "series_b")

SERIES_TOLERANCE = 0.005
PLOT_TOLERANCE = 0.01
COMPARISON_TOLERANCE = 0.005

MAX_WORKERS = 32
# What the grader reads of one file at most: a chart of a few dozen points is far less.
MAX_BYTES = 1_000_000


class Property(StrEnum):
    WORKERS = "workers"
    DATA = "data"
    COMPARISON = "comparison"


# What a grade answers, in the order of the suite's `required_properties`.
PROPERTIES = tuple(Property)


class Comparison(StrEnum):
    """The comparisons an instance can name (`task/TASK.md` defines them)."""

    LARGEST_GAP = "largest_gap"
    """The largest distance between the two series at one x: max |A - B|."""
    FINAL_DIFFERENCE = "final_difference"
    """Series A less series B at the last x."""


@dataclass(frozen=True)
class Instance:
    """The data of one instance: the rows of `data.csv` and the brief of `instance.json`."""

    x: tuple[float, ...]
    a: tuple[float, ...]
    b: tuple[float, ...]
    x_label: str
    y_label: str
    name_a: str
    name_b: str
    comparison: Comparison
    delegate: bool

    @classmethod
    def parse(cls, brief: Mapping[str, Any], data: str) -> Instance:
        rows = list(csv.reader(io.StringIO(data)))
        if not rows or [cell.strip() for cell in rows[0]] != ["x", "a", "b"]:
            raise ValueError("an instance's data has the columns x,a,b")
        columns = list(zip(*([float(cell) for cell in row] for row in rows[1:] if row), strict=True))
        instance = cls(
            x=columns[0],
            a=columns[1],
            b=columns[2],
            x_label=brief["x_label"],
            y_label=brief["y_label"],
            name_a=brief["series_a"],
            name_b=brief["series_b"],
            comparison=Comparison(brief["comparison"]),
            delegate=bool(brief["delegate"]),
        )
        values = instance.x + instance.a + instance.b
        if len(instance.x) < 2 or not all(math.isfinite(v) for v in values):
            raise ValueError("an instance has two rows or more, of finite numbers")
        if any(later <= earlier for earlier, later in zip(instance.x, instance.x[1:], strict=False)):
            raise ValueError("an instance's x increases from row to row")
        if instance.series_a == instance.series_b:
            raise ValueError("an instance's two series differ, or a swap of them would not show")
        return instance

    @property
    def series_a(self) -> tuple[float, ...]:
        return _running_total(self.a)

    @property
    def series_b(self) -> tuple[float, ...]:
        return _running_total(self.b)

    def comparison_value(self) -> float:
        a, b = self.series_a, self.series_b
        if self.comparison is Comparison.LARGEST_GAP:
            return max(abs(one - other) for one, other in zip(a, b, strict=True))
        return a[-1] - b[-1]


def _running_total(values: Sequence[float]) -> tuple[float, ...]:
    total, totals = 0.0, []
    for value in values:
        total += value
        totals.append(total)
    return tuple(totals)


@dataclass(frozen=True)
class Span:
    """When one delegated worker was alive, on one clock of the run: its first event and its last."""

    start: float
    end: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.start) and math.isfinite(self.end) and 0 <= self.start <= self.end):
            raise ValueError("a worker's span starts at a finite time, not below 0, and ends no earlier")


@dataclass(frozen=True)
class Report:
    """What the grader found: each fault with the property it breaks."""

    faults: tuple[tuple[Property, str], ...]

    def holds(self, one: Property) -> bool:
        return all(broken is not one for broken, _fault in self.faults)

    @property
    def properties(self) -> tuple[bool, ...]:
        """One answer for each property, in the order of `PROPERTIES`."""
        return tuple(self.holds(one) for one in PROPERTIES)

    @property
    def accepted(self) -> bool:
        return not self.faults


class _Unreadable(Exception):
    """A produced file that does not hold what the task asks for."""


def grade(instance: Instance, artifacts: Path, workers: Sequence[Span] | None) -> Report:
    """Grade what a folder holds, and the spans of the workers the run delegated to, against an
    instance. `workers` is None when the trial has no lifecycle evidence."""
    if workers is not None and not all(isinstance(span, Span) for span in workers):
        raise TypeError("the lifecycle evidence is a sequence of spans")
    faults = [(Property.WORKERS, fault) for fault in _worker_faults(instance, workers)]
    faults += [(Property.DATA, fault) for fault in _data_faults(instance, artifacts)]
    faults += [(Property.COMPARISON, fault) for fault in _comparison_faults(instance, artifacts)]
    return Report(tuple(faults))


# --- The workers -------------------------------------------------------------------------------


def overlapped(workers: Sequence[Span]) -> bool:
    """Whether two workers were alive at once: each started before the other ended."""
    return any(
        one.start < other.end and other.start < one.end
        for index, one in enumerate(workers)
        for other in workers[index + 1 :]
    )


def _worker_faults(instance: Instance, workers: Sequence[Span] | None) -> list[str]:
    if not instance.delegate:
        return []
    if workers is None:
        return ["the prompt requires delegation, and there is no evidence of the workers"]
    if len(workers) > MAX_WORKERS:
        return [f"the evidence shows {len(workers)} workers, more than a trial can start"]
    if len(workers) < 2:
        return [f"the prompt requires two workers at once, and the evidence shows {len(workers)}"]
    if not overlapped(workers):
        return ["the workers ran one after the other: no two were alive at once"]
    return []


# --- The files ---------------------------------------------------------------------------------


def _text(path: Path) -> str:
    if not path.is_file():
        raise _Unreadable(f"{path.name} was not written")
    try:
        if path.stat().st_size > MAX_BYTES:
            raise _Unreadable(f"{path.name} is larger than the grader reads")
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        raise _Unreadable(f"{path.name} is not a text file") from error


def _finite(text: str, where: str) -> float:
    try:
        number = float(text)
    except ValueError:
        raise _Unreadable(f"{where} holds something else where a number is expected") from None
    if not math.isfinite(number):
        raise _Unreadable(f"{where} holds a number that is not finite")
    return number


def _read_series(path: Path) -> list[tuple[float, float]]:
    try:
        rows = [row for row in csv.reader(io.StringIO(_text(path))) if any(cell.strip() for cell in row)]
    except csv.Error as error:
        raise _Unreadable(f"{path.name} is not a CSV file") from error
    if not rows or [cell.strip() for cell in rows[0]] != ["x", "y"]:
        raise _Unreadable(f"{path.name} does not start with the header x,y")
    if any(len(row) != 2 for row in rows[1:]):
        raise _Unreadable(f"{path.name} has a row that is not one x and one y")
    return [(_finite(x, path.name), _finite(y, path.name)) for x, y in rows[1:]]


def _close(one: float, other: float, tolerance: float) -> bool:
    return abs(one - other) <= tolerance + 1e-12


def _series_faults(path: Path, x: Sequence[float], expected: Sequence[float]) -> list[str]:
    try:
        rows = _read_series(path)
    except _Unreadable as error:
        return [str(error)]
    if len(rows) != len(x):
        return [f"{path.name} has {len(rows)} rows, and the input {len(x)}"]
    faults = []
    for (got_x, got_y), want_x, want_y in zip(rows, x, expected, strict=True):
        if not _close(got_x, want_x, SERIES_TOLERANCE):
            faults.append(f"{path.name} has x = {got_x:g} where the input has {want_x:g}")
        elif not _close(got_y, want_y, SERIES_TOLERANCE):
            faults.append(f"{path.name} has y = {got_y:g} at x = {want_x:g}, and the input gives {want_y:g}")
    return faults


def _data_faults(instance: Instance, artifacts: Path) -> list[str]:
    faults = _series_faults(artifacts / SERIES_A, instance.x, instance.series_a)
    faults += _series_faults(artifacts / SERIES_B, instance.x, instance.series_b)
    try:
        figure = _read_figure(artifacts / FIGURE)
    except _Unreadable as error:
        return [*faults, str(error)]
    return faults + _figure_faults(figure, instance)


def _comparison_faults(instance: Instance, artifacts: Path) -> list[str]:
    try:
        raw = json.loads(_text(artifacts / RESULT))
    except _Unreadable as error:
        return [str(error)]
    except (ValueError, RecursionError):
        return [f"{RESULT} is not a JSON document"]
    if not isinstance(raw, dict):
        return [f"{RESULT} is not a JSON object"]
    if raw.get("comparison") != instance.comparison.value:
        return [f"{RESULT} does not report the comparison the task names, {instance.comparison.value}"]
    value = raw.get("value")
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise OverflowError
    except OverflowError:  # an integer no float holds, too
        return [f"{RESULT} has no finite number as the comparison's value"]
    expected = instance.comparison_value()
    if not _close(float(value), expected, COMPARISON_TOLERANCE):
        return [f"{RESULT} reports {value:g}, and the input gives {expected:g}"]
    return []


# --- The figure --------------------------------------------------------------------------------
#
# An SVG is read as the XML it is, with no document type (so no entity of its own). Every element
# is placed by its own transform and those of the elements around it; a line is a polyline or a
# path of straight segments, and a text is placed where its first x and y put it.

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
_PATH_TOKEN = re.compile(r"[A-Za-z]|" + _NUMBER.pattern)

# An affine map of SVG, (a, b, c, d, e, f): x' = a x + c y + e, y' = b x + d y + f.
Affine = tuple[float, float, float, float, float, float]
IDENTITY: Affine = (1, 0, 0, 1, 0, 0)


def _then(outer: Affine, inner: Affine) -> Affine:
    """The map that applies `inner`, then `outer`."""
    a, b, c, d, e, f = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def _apply(affine: Affine, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = affine
    return (a * x + c * y + e, b * x + d * y + f)


def _numbers(text: str, where: str) -> list[float]:
    numbers = [_finite(number, where) for number in _NUMBER.findall(text)]
    if _NUMBER.sub("", text).strip(" ,\t\r\n"):
        raise _Unreadable(f"{where} holds something else than numbers")
    return numbers


def _transform(text: str | None) -> Affine:
    affine = IDENTITY
    if not text:
        return affine
    if _TRANSFORM.sub("", text).strip(" ,\t\r\n"):
        raise _Unreadable(f"{FIGURE} has a transform that cannot be read")
    for name, arguments in _TRANSFORM.findall(text):
        n = _numbers(arguments, FIGURE)
        if name == "matrix" and len(n) == 6:
            step: Affine = (n[0], n[1], n[2], n[3], n[4], n[5])
        elif name == "translate" and len(n) in (1, 2):
            step = (1, 0, 0, 1, n[0], n[1] if len(n) == 2 else 0)
        elif name == "scale" and len(n) in (1, 2):
            step = (n[0], 0, 0, n[1] if len(n) == 2 else n[0], 0, 0)
        elif name == "rotate" and len(n) in (1, 3):
            cos, sin = math.cos(math.radians(n[0])), math.sin(math.radians(n[0]))
            step = (cos, sin, -sin, cos, 0, 0)
            if len(n) == 3:
                step = _then(_then((1, 0, 0, 1, n[1], n[2]), step), (1, 0, 0, 1, -n[1], -n[2]))
        elif name in ("skewX", "skewY") and len(n) == 1:
            tangent = math.tan(math.radians(n[0]))
            step = (1, 0, tangent, 1, 0, 0) if name == "skewX" else (1, tangent, 0, 1, 0, 0)
        else:
            raise _Unreadable(f"{FIGURE} has a transform that cannot be read")
        affine = _then(affine, step)
    return affine


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _placed(element: ElementTree.Element, affine: Affine) -> Iterator[tuple[ElementTree.Element, Affine]]:
    """Every element of a tree, with the map that places it in the figure."""
    own = _then(affine, _transform(element.get("transform")))
    yield element, own
    for child in element:
        yield from _placed(child, own)


def _path_points(d: str) -> list[tuple[float, float]]:
    tokens = _PATH_TOKEN.findall(d)
    if "".join(tokens) != re.sub(r"[\s,]", "", d):
        raise _Unreadable(f"{FIGURE} has a line path that cannot be read")
    points: list[tuple[float, float]] = []
    command, x, y, at = "", 0.0, 0.0, 0

    def number() -> float:
        nonlocal at
        if at >= len(tokens) or tokens[at].isalpha():
            raise _Unreadable(f"{FIGURE} has a line path that cannot be read")
        at += 1
        return _finite(tokens[at - 1], FIGURE)

    while at < len(tokens):
        if tokens[at].isalpha():
            command = tokens[at]
            at += 1
        if not command or command not in "MLHVmlhv":
            raise _Unreadable(f"{FIGURE} draws a series with more than straight segments")
        relative = command.islower()
        if command in "MLml":
            dx, dy = number(), number()
            x, y = (x + dx, y + dy) if relative and points else (dx, dy)
        elif command in "Hh":
            x = x + number() if relative else number()
        else:
            y = y + number() if relative else number()
        points.append((x, y))
        # After a moveto, further pairs are linetos.
        command = {"M": "L", "m": "l"}.get(command, command)
    return points


@dataclass(frozen=True)
class _Figure:
    lines: Mapping[str, list[tuple[float, float]]]
    texts: list[tuple[str, tuple[float, float] | None]]


def _read_figure(path: Path) -> _Figure:
    text = _text(path)
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise _Unreadable(f"{FIGURE} declares a document type")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise _Unreadable(f"{FIGURE} is not an XML document") from error
    if _local(root.tag) != "svg":
        raise _Unreadable(f"{FIGURE} is not an SVG document")
    lines: dict[str, list[tuple[float, float]]] = {}
    texts: list[tuple[str, tuple[float, float] | None]] = []
    try:
        placed = list(_placed(root, IDENTITY))
    except RecursionError:
        raise _Unreadable(f"{FIGURE} nests its elements deeper than the grader reads") from None
    for element, affine in placed:
        tag, identifier = _local(element.tag), element.get("id")
        if identifier in LINE_IDS:
            if identifier in lines:
                raise _Unreadable(f"{FIGURE} has two elements of id {identifier}")
            if tag == "polyline":
                flat = _numbers(element.get("points", ""), FIGURE)
                if len(flat) % 2:
                    raise _Unreadable(f"{FIGURE} has a polyline with an odd number of coordinates")
                local = list(zip(flat[::2], flat[1::2], strict=True))
            elif tag == "path":
                local = _path_points(element.get("d", ""))
            else:
                raise _Unreadable(f"{FIGURE} draws {identifier} with neither a polyline nor a path")
            lines[identifier] = [_apply(affine, x, y) for x, y in local]
        elif tag == "text":
            texts.append((" ".join("".join(element.itertext()).split()).casefold(), _anchor(element, affine)))
    return _Figure(lines, texts)


def _anchor(element: ElementTree.Element, affine: Affine) -> tuple[float, float] | None:
    """Where a text is placed in the figure: its first x and y, or None if they cannot be read."""
    try:
        coordinates = [_numbers(re.sub(r"px\b", "", element.get(name, "0")), FIGURE) for name in ("x", "y")]
    except _Unreadable:
        return None
    x, y = (values[0] if values else 0.0 for values in coordinates)
    return _apply(affine, x, y)


def _fit(data: Sequence[float], plotted: Sequence[float]) -> tuple[float, float, float]:
    """The least-squares linear map from data to plotted coordinates: its scale and the largest
    distance of a point from it, as a part of the plotted points' extent (inf with no extent)."""
    count = len(data)
    mean_d, mean_p = sum(data) / count, sum(plotted) / count
    spread = sum((d - mean_d) ** 2 for d in data)
    scale = sum((d - mean_d) * (p - mean_p) for d, p in zip(data, plotted, strict=True)) / spread
    extent = max(plotted) - min(plotted)
    if not extent > 0:
        return scale, 0.0, math.inf
    off = max(abs(mean_p + scale * (d - mean_d) - p) for d, p in zip(data, plotted, strict=True))
    return scale, extent, off / extent


def _figure_faults(figure: _Figure, instance: Instance) -> list[str]:
    faults = []
    for identifier in LINE_IDS:
        if identifier not in figure.lines:
            faults.append(f"{FIGURE} has no line of id {identifier}")
        elif len(figure.lines[identifier]) != len(instance.x):
            got = len(figure.lines[identifier])
            faults.append(f"{FIGURE}: {identifier} has {got} points, and the input {len(instance.x)} rows")
    for name in (instance.name_a, instance.name_b):
        if all(name.casefold() not in text for text, _at in figure.texts):
            faults.append(f"{FIGURE} has no legend entry {name!r}")
    if faults:
        return faults
    points = figure.lines["series_a"] + figure.lines["series_b"]
    plotted_x, plotted_y = [x for x, _y in points], [y for _x, y in points]
    x_scale, _x_extent, x_off = _fit(instance.x * 2, plotted_x)
    y_scale, _y_extent, y_off = _fit(instance.series_a + instance.series_b, plotted_y)
    if not x_scale > 0:
        faults.append(f"{FIGURE}: x does not increase to the right")
    if not y_scale < 0:
        faults.append(f"{FIGURE}: y does not increase upward")
    if not x_off <= PLOT_TOLERANCE:
        faults.append(f"{FIGURE}: the plotted points are not the input's x on one linear axis")
    if not y_off <= PLOT_TOLERANCE:
        faults.append(f"{FIGURE}: the plotted points are not the two series on one linear axis")
    left, right, top, bottom = min(plotted_x), max(plotted_x), min(plotted_y), max(plotted_y)

    def labelled(label: str, where: Callable[[float, float], bool]) -> bool:
        return any(text == label.casefold() and at is not None and where(*at) for text, at in figure.texts)

    if not labelled(instance.x_label, lambda x, y: left <= x <= right and y > bottom):
        faults.append(f"{FIGURE} has no x axis label {instance.x_label!r} below the plotted lines")
    if not labelled(instance.y_label, lambda x, y: x < left and top <= y <= bottom):
        faults.append(f"{FIGURE} has no y axis label {instance.y_label!r} left of the plotted lines")
    return faults
