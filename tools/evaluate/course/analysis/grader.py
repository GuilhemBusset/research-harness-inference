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
  each series as one painted, connected line (the element of id `series_a`, and of `series_b`)
  whose points, in the order of `x`, are the series under one linear map of each axis, with `x`
  to the right and `y` upward; the label of the x axis is written below the plotted lines and the
  one of the y axis to their left; every number written below the lines or left of them (a tick)
  sits where its value sits on that axis; and a legend names each series beside a mark in the
  colour of its line, or in that colour itself, the two lines being of two colours;
- *comparison*: the comparison `result.json` reports is the one the instance names, and its value
  is what the input gives.

It runs nothing a run submitted and compares what a run wrote with the instance, never with the
reference artifacts: a figure of another size, scale, margin or drawing (a polyline or a path,
in a transformed group, a label in a `tspan`) is accepted when it shows the same.

**What is drawn.** The grader reads an SVG's geometry (`transform`, `x`, `y` and the shapes'
coordinates) and its paint, from presentation attributes and `style` attributes, inherited as SVG
inherits them; it reads no style sheet. A line, a text or a mark that is not displayed (`display`,
`visibility`, an `opacity` of 0, no stroke, a stroke of width 0) is not drawn, nor is anything
inside `defs` and the other containers SVG never renders.

**Lifecycle evidence** is the trusted side's, never a file of the run: what `adapters.delegation`
read from Claude Code's output (a `Delegation`, `Trial.delegation`: how many workers it started
and whether two were alive at once), or the span of each worker, from its first event to its last,
on one clock of the run (`Span`). It is bounded: a trial has at most 32 model turns across its
agent tree, and each worker takes at least one, so evidence of more than `MAX_WORKERS` workers is
no evidence. Evidence that is not evidence (a span not finite, negative or ending before it
starts; a negative count; one worker overlapping) is the trusted side's error, and raises.

**Tolerances.** A number of a series is a rounded one: it may differ from the running total by
`SERIES_TOLERANCE` (absolute). Each axis is the linear map from data to the figure that keeps
every plotted point nearest it; a point may sit off it by `PLOT_TOLERANCE` of the extent of the
plotted points on that axis. A tick may sit off the place of its value by `TICK_TOLERANCE` of that
extent: a text is placed by its anchor, and half the distance between ten ticks still tells one
from the next. The reported value may differ from the input's by `COMPARISON_TOLERANCE`
(absolute).

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
from itertools import combinations
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from evaluate.adapters import Delegation

SERIES_A = "series_a.csv"
SERIES_B = "series_b.csv"
FIGURE = "figure.svg"
RESULT = "result.json"
LINE_IDS = ("series_a", "series_b")

SERIES_TOLERANCE = 0.005
PLOT_TOLERANCE = 0.01
TICK_TOLERANCE = 0.05
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
        one, other = instance.name_a.casefold(), instance.name_b.casefold()
        if not one.strip() or not other.strip() or one in other or other in one:
            raise ValueError("an instance names its series apart: neither name holds the other")
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


Evidence = Delegation | Sequence[Span] | None
"""The lifecycle of the workers a run delegated to, as the trusted side observed it."""


def grade(instance: Instance, artifacts: Path, workers: Evidence) -> Report:
    """Grade what a folder holds, and the workers the run delegated to, against an instance.
    `workers` is what the trial's adapter read (`Trial.delegation`) or the spans of the workers,
    and None when the trial has no lifecycle evidence."""
    faults = [(Property.WORKERS, fault) for fault in _worker_faults(instance, delegation_of(workers))]
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


def delegation_of(workers: Evidence) -> Delegation | None:
    """The evidence as a count of workers and whether two were alive at once. Raises on evidence
    that is not evidence: it is the trusted side's."""
    if workers is None:
        return None
    if isinstance(workers, Delegation):
        count, both = workers.workers, workers.overlapped
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(both, bool):
            raise ValueError("a delegation is a count of workers, not below 0, and whether two overlapped")
        if both and count < 2:
            raise ValueError("a delegation in which workers overlapped has two workers or more")
        return workers
    spans = tuple(workers)
    if not all(isinstance(span, Span) for span in spans):
        raise TypeError("the lifecycle evidence is a delegation or a sequence of spans")
    return Delegation(len(spans), overlapped(spans))


def _worker_faults(instance: Instance, workers: Delegation | None) -> list[str]:
    if not instance.delegate:
        return []
    if workers is None:
        return ["the prompt requires delegation, and there is no evidence of the workers"]
    if workers.workers > MAX_WORKERS:
        return [f"the evidence shows {workers.workers} workers, more than a trial can start"]
    if workers.workers < 2:
        return [f"the prompt requires two workers at once, and the evidence shows {workers.workers}"]
    if not workers.overlapped:
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
# is placed by its own transform and those of the elements around it, and painted by its own
# paint and the paint it inherits. A line is a polyline, or a path of connected straight segments;
# a text is placed where its first character is; a mark is any other painted shape.

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
_PATH_TOKEN = re.compile(r"[A-Za-z]|" + _NUMBER.pattern)
# A number written as a tick: digits, maybe signed (with the typographic minus too).
_TICK = re.compile(r"[-+−]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")

# An affine map of SVG, (a, b, c, d, e, f): x' = a x + c y + e, y' = b x + d y + f.
Affine = tuple[float, float, float, float, float, float]
IDENTITY: Affine = (1, 0, 0, 1, 0, 0)
Point = tuple[float, float]

# The paint an element inherits, and what it is when nothing sets it.
_INHERITED = {
    "stroke": "none",
    "fill": "black",
    "color": "black",
    "visibility": "visible",
    "stroke-opacity": "1",
    "fill-opacity": "1",
    "stroke-width": "1",
}
# What SVG never renders, nor anything inside it.
_UNRENDERED = {"defs", "clipPath", "mask", "marker", "pattern", "symbol", "metadata", "title", "desc", "style"}
_MARKS = {"line", "rect", "circle", "ellipse", "polyline", "polygon", "path"}


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


def _apply(affine: Affine, x: float, y: float) -> Point:
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


def _length(text: str | None) -> float | None:
    """A coordinate or a length: its first number, in user units (`px` too); None if it has none."""
    if text is None:
        return None
    try:
        numbers = _numbers(re.sub(r"px\b", "", text), FIGURE)
    except _Unreadable:
        return None
    return numbers[0] if numbers else None


def _fraction(text: str | None) -> float:
    """An opacity, from 0 to 1: a number or a percentage; one that cannot be read is SVG's default, 1."""
    if text is None:
        return 1.0
    percent = text.strip().endswith("%")
    number = _length(text.strip().removesuffix("%"))
    if number is None:
        return 1.0
    return min(max(number / 100 if percent else number, 0.0), 1.0)


def _colour(text: str, paint: Mapping[str, str]) -> str | None:
    """A paint as one spelling of its colour; None for no paint."""
    value = text.strip().casefold()
    if value == "currentcolor":
        value = paint["color"].strip().casefold()
    if value in ("none", "transparent", ""):
        return None
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(digit * 2 for digit in value[1:])
    channels = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", value)
    if channels:
        return "#" + "".join(f"{min(int(one), 255):02x}" for one in channels.groups())
    return value


@dataclass(frozen=True)
class _Drawn:
    """How an element is placed and painted."""

    affine: Affine
    paint: Mapping[str, str]
    opacity: float
    shown: bool

    def stroke(self) -> str | None:
        """The colour its outline is drawn in, None if it is drawn in none."""
        width = _length(self.paint["stroke-width"])
        if (width is not None and width <= 0) or not _fraction(self.paint["stroke-opacity"]) > 0:
            return None
        return _colour(self.paint["stroke"], self.paint) if self.visible() else None

    def fill(self) -> str | None:
        """The colour it is filled with, None if it is filled with none."""
        if not _fraction(self.paint["fill-opacity"]) > 0:
            return None
        return _colour(self.paint["fill"], self.paint) if self.visible() else None

    def visible(self) -> bool:
        return self.shown and self.opacity > 0 and self.paint["visibility"].strip() == "visible"


def _declared(element: ElementTree.Element) -> dict[str, str]:
    """The paint an element sets: its presentation attributes, then its `style` attribute."""
    declared = {name: value for name in (*_INHERITED, "display", "opacity") if (value := element.get(name))}
    for declaration in (element.get("style") or "").split(";"):
        name, colon, value = declaration.partition(":")
        if colon:
            declared[name.strip().casefold()] = value.replace("!important", "").strip()
    return declared


def _placed(element: ElementTree.Element, around: _Drawn) -> Iterator[tuple[ElementTree.Element, _Drawn]]:
    """Every element of a tree, with how it is placed and painted in the figure."""
    declared = _declared(element)
    paint = dict(around.paint)
    paint.update({name: value for name, value in declared.items() if name in _INHERITED and value != "inherit"})
    shown = around.shown and declared.get("display", "").strip() != "none" and _local(element.tag) not in _UNRENDERED
    own = _Drawn(
        _then(around.affine, _transform(element.get("transform"))),
        paint,
        around.opacity * _fraction(declared.get("opacity")),
        shown,
    )
    yield element, own
    for child in element:
        yield from _placed(child, own)


def _path_points(d: str, identifier: str) -> list[Point]:
    """The points of a path of connected straight segments: one moveto, then linetos."""
    tokens = _PATH_TOKEN.findall(d)
    if "".join(tokens) != re.sub(r"[\s,]", "", d):
        raise _Unreadable(f"{FIGURE} has a path for {identifier} that cannot be read")
    if not tokens or tokens[0] not in "Mm":
        raise _Unreadable(f"{FIGURE} has a path for {identifier} that does not start with a moveto")
    points: list[Point] = []
    command, x, y, at = "", 0.0, 0.0, 0

    def number() -> float:
        nonlocal at
        if at >= len(tokens) or tokens[at].isalpha():
            raise _Unreadable(f"{FIGURE} has a path for {identifier} that cannot be read")
        at += 1
        return _finite(tokens[at - 1], FIGURE)

    while at < len(tokens):
        if tokens[at].isalpha():
            command = tokens[at]
            at += 1
            if command in "Mm" and points:
                raise _Unreadable(f"{FIGURE} draws {identifier} in pieces: its path moves the pen again")
        if command not in "MLHVmlhv":
            raise _Unreadable(f"{FIGURE} draws {identifier} with more than straight segments")
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


def _line_points(element: ElementTree.Element, identifier: str) -> list[Point]:
    tag = _local(element.tag)
    if tag == "polyline":
        flat = _numbers(element.get("points", ""), FIGURE)
        if len(flat) % 2:
            raise _Unreadable(f"{FIGURE} has a polyline with an odd number of coordinates")
        return list(zip(flat[::2], flat[1::2], strict=True))
    if tag == "path":
        return _path_points(element.get("d", ""), identifier)
    raise _Unreadable(f"{FIGURE} draws {identifier} with neither a polyline nor a path")


def _mark_points(element: ElementTree.Element) -> list[Point]:
    """The points that outline a mark, in its own coordinates; none when they cannot be read."""
    tag = _local(element.tag)
    try:
        if tag in ("polyline", "polygon"):
            flat = _numbers(element.get("points", ""), FIGURE)
            return list(zip(flat[::2], flat[1::2], strict=False))
        if tag == "path":
            return _path_points(element.get("d", ""), "a mark")
    except _Unreadable:
        return []
    names = {
        "line": ("x1", "y1", "x2", "y2"),
        "rect": ("x", "y", "width", "height"),
        "circle": ("cx", "cy", "r", "r"),
        "ellipse": ("cx", "cy", "rx", "ry"),
    }[tag]
    values = [_length(element.get(name, "0")) for name in names]
    if any(value is None for value in values):
        return []
    one, two, three, four = (value or 0.0 for value in values)
    if tag == "line":
        return [(one, two), (three, four)]
    if tag == "rect":
        return [(one, two), (one + three, two), (one, two + four), (one + three, two + four)]
    return [(one - three, two - four), (one + three, two + four)]


def _first_character(element: ElementTree.Element, x: float | None, y: float | None) -> tuple[float, float]:
    """Where a text's first character is placed: the `x` and `y` of the innermost element that
    holds it and sets them, as SVG places a `tspan`."""
    x = own if (own := _length(element.get("x"))) is not None else x
    y = own if (own := _length(element.get("y"))) is not None else y
    if not (element.text or "").strip():
        for child in element:
            if "".join(child.itertext()).strip():
                return _first_character(child, x, y)
    return (x or 0.0, y or 0.0)


@dataclass(frozen=True)
class _Line:
    points: list[Point]
    colour: str | None
    """The colour it is drawn in; None when it is not drawn."""


@dataclass(frozen=True)
class _Text:
    content: str
    """What it says, its spaces folded, in lower case."""
    at: Point
    colour: str | None


@dataclass(frozen=True)
class _Mark:
    at: Point
    """The middle of what it covers."""
    colours: frozenset[str]


@dataclass(frozen=True)
class _Figure:
    lines: Mapping[str, _Line]
    texts: list[_Text]
    marks: list[_Mark]


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
    try:
        return _drawing(root)
    except RecursionError:
        raise _Unreadable(f"{FIGURE} nests its elements deeper than the grader reads") from None


def _drawing(root: ElementTree.Element) -> _Figure:
    """The lines, texts and marks an SVG draws."""
    lines: dict[str, _Line] = {}
    texts: list[_Text] = []
    marks: list[_Mark] = []
    for element, drawn in list(_placed(root, _Drawn(IDENTITY, _INHERITED, 1.0, shown=True))):
        tag, identifier = _local(element.tag), element.get("id")
        if identifier in LINE_IDS:
            if identifier in lines:
                raise _Unreadable(f"{FIGURE} has two elements of id {identifier}")
            local = _line_points(element, identifier)
            lines[identifier] = _Line([_apply(drawn.affine, x, y) for x, y in local], drawn.stroke())
        elif tag == "text" and drawn.visible():
            content = " ".join("".join(element.itertext()).split()).casefold()
            at = _apply(drawn.affine, *_first_character(element, None, None))
            texts.append(_Text(content, at, drawn.fill() or drawn.stroke()))
        elif tag in _MARKS and drawn.visible():
            colours = frozenset(one for one in (drawn.stroke(), None if tag == "line" else drawn.fill()) if one)
            points = [_apply(drawn.affine, x, y) for x, y in _mark_points(element)]
            if colours and points:
                xs, ys = [x for x, _y in points], [y for _x, y in points]
                marks.append(_Mark(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), colours))
    return _Figure(lines, texts, marks)


def _fit(data: Sequence[float], plotted: Sequence[float]) -> tuple[float, float, float]:
    """The linear map from data to plotted coordinates that keeps the farthest point nearest it
    (the minimax fit): its scale, its offset, and that point's distance from it, as a part of
    the plotted points' extent (inf with no extent). The distance falls as the scale nears its
    best on either side, and turns only where the map runs through two of the points: the best
    scale is one of the slopes between two points."""
    pairs = list(zip(data, plotted, strict=True))

    def residuals(scale: float) -> list[float]:
        return [p - scale * d for d, p in pairs]

    slopes = {(p2 - p1) / (d2 - d1) for (d1, p1), (d2, p2) in combinations(pairs, 2) if d2 != d1}
    scale = min(slopes, key=lambda one: max(residuals(one)) - min(residuals(one)))
    off = residuals(scale)
    extent = max(plotted) - min(plotted)
    if not extent > 0:
        return scale, 0.0, math.inf
    return scale, (max(off) + min(off)) / 2, (max(off) - min(off)) / 2 / extent


def _legend_faults(figure: _Figure, instance: Instance) -> list[str]:
    """A legend names each series in the colour of its line: beside a mark in that colour (the
    nearest mark in the colour of either line is in it), or, where no mark is in either, in a text
    of that colour."""
    colours = {identifier: figure.lines[identifier].colour for identifier in LINE_IDS}
    if colours["series_a"] == colours["series_b"]:
        return [f"{FIGURE} draws both series in one colour: no legend can tell them apart"]
    keys = [mark for mark in figure.marks if mark.colours & set(colours.values())]

    def colour_of(text: _Text) -> set[str]:
        if not keys:
            return {text.colour} if text.colour else set()
        nearest = min(keys, key=lambda mark: math.dist(mark.at, text.at))
        return set(nearest.colours)

    faults = []
    names = {"series_a": instance.name_a.casefold(), "series_b": instance.name_b.casefold()}
    for identifier, other in (LINE_IDS, LINE_IDS[::-1]):
        named = [
            text for text in figure.texts if names[identifier] in text.content and names[other] not in text.content
        ]
        if not named:
            faults.append(f"{FIGURE} has no legend entry {names[identifier]!r}")
        elif not any(
            colours[identifier] in (shown := colour_of(text)) and colours[other] not in shown for text in named
        ):
            faults.append(f"{FIGURE}: the legend entry {names[identifier]!r} is not in the colour of {identifier}")
    return faults


def _figure_faults(figure: _Figure, instance: Instance) -> list[str]:
    faults = []
    for identifier in LINE_IDS:
        line = figure.lines.get(identifier)
        if line is None:
            faults.append(f"{FIGURE} has no line of id {identifier}")
        elif line.colour is None:
            faults.append(f"{FIGURE} does not draw {identifier}: it is hidden, or painted in nothing")
        elif len(line.points) != len(instance.x):
            faults.append(f"{FIGURE}: {identifier} has {len(line.points)} points, and the input {len(instance.x)} rows")
    if faults:
        return faults
    points = figure.lines["series_a"].points + figure.lines["series_b"].points
    plotted_x, plotted_y = [x for x, _y in points], [y for _x, y in points]
    x_scale, x_offset, x_off = _fit(instance.x * 2, plotted_x)
    y_scale, y_offset, y_off = _fit(instance.series_a + instance.series_b, plotted_y)
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
        return any(text.content == label.casefold() and where(*text.at) for text in figure.texts)

    if not labelled(instance.x_label, lambda x, y: left <= x <= right and y > bottom):
        faults.append(f"{FIGURE} has no x axis label {instance.x_label!r} below the plotted lines")
    if not labelled(instance.y_label, lambda x, y: x < left and top <= y <= bottom):
        faults.append(f"{FIGURE} has no y axis label {instance.y_label!r} left of the plotted lines")
    # A number below the lines is a tick of the x axis, one left of them of the y axis.
    for text in figure.texts:
        if not _TICK.fullmatch(text.content):
            continue
        value, (x, y) = float(text.content.replace("−", "-")), text.at
        on_x = y > bottom and abs(x_offset + x_scale * value - x) <= TICK_TOLERANCE * (right - left)
        on_y = x < left and abs(y_offset + y_scale * value - y) <= TICK_TOLERANCE * (bottom - top)
        if (y > bottom or x < left) and not (on_x or on_y):
            faults.append(f"{FIGURE}: the tick {text.content} is not where its value is on its axis")
    return faults + _legend_faults(figure, instance)
