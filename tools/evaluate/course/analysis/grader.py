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

**What is drawn.** The grader reads an SVG's geometry (`transform`, `x`, `y`, the shapes'
coordinates, and each `svg` element's viewport: `width`, `height`, `viewBox`,
`preserveAspectRatio`) and its paint, from presentation attributes and `style` attributes,
inherited as SVG inherits them; it reads no style sheet. A colour is one colour however SVG lets it
be written (a keyword, `#rgb`, `#rrggbb`, `rgb()`, `hsl()`). A line, a text or a mark that is not
displayed (`display`, `visibility`, an `opacity` of 0, no paint, a stroke of width 0, a text of
size 0) is not drawn, nor is anything inside `defs` and the other containers SVG never renders,
nor anything under a mask or a filter, which the grader does not read. What is drawn is seen where
it is inside the viewport and its clips (a `clip-path` of one `rect`; another clip hides what it
applies to), not under an opaque rectangle painted after it, and in a colour apart from what is
behind it there: the last opaque rectangle painted before it, or a white page. A series is shown
when every point of it is seen; a text or a mark where its anchor, or its middle, is.

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

import colorsys
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
    "font-size": "16",
}
# What SVG never renders, nor anything inside it.
_UNRENDERED = {"defs", "clipPath", "mask", "marker", "pattern", "symbol", "metadata", "title", "desc", "style"}
_MARKS = {"line", "rect", "circle", "ellipse", "polyline", "polygon", "path"}
# What a viewer shows behind a figure that paints no background of its own.
_PAGE = "#ffffff"

# The colour keywords of SVG and CSS, as the colour each names.
_NAMED = dict(
    re.findall(
        r"(\w+):(\w+)",
        """
    aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff beige:f5f5dc
    bisque:ffe4c4 black:000000 blanchedalmond:ffebcd blue:0000ff blueviolet:8a2be2 brown:a52a2a
    burlywood:deb887 cadetblue:5f9ea0 chartreuse:7fff00 chocolate:d2691e coral:ff7f50
    cornflowerblue:6495ed cornsilk:fff8dc crimson:dc143c cyan:00ffff darkblue:00008b darkcyan:008b8b
    darkgoldenrod:b8860b darkgray:a9a9a9 darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b
    darkmagenta:8b008b darkolivegreen:556b2f darkorange:ff8c00 darkorchid:9932cc darkred:8b0000
    darksalmon:e9967a darkseagreen:8fbc8f darkslateblue:483d8b darkslategray:2f4f4f
    darkslategrey:2f4f4f darkturquoise:00ced1 darkviolet:9400d3 deeppink:ff1493 deepskyblue:00bfff
    dimgray:696969 dimgrey:696969 dodgerblue:1e90ff firebrick:b22222 floralwhite:fffaf0
    forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc ghostwhite:f8f8ff gold:ffd700
    goldenrod:daa520 gray:808080 grey:808080 green:008000 greenyellow:adff2f honeydew:f0fff0
    hotpink:ff69b4 indianred:cd5c5c indigo:4b0082 ivory:fffff0 khaki:f0e68c lavender:e6e6fa
    lavenderblush:fff0f5 lawngreen:7cfc00 lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080
    lightcyan:e0ffff lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90 lightgrey:d3d3d3
    lightpink:ffb6c1 lightsalmon:ffa07a lightseagreen:20b2aa lightskyblue:87cefa
    lightslategray:778899 lightslategrey:778899 lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00
    limegreen:32cd32 linen:faf0e6 magenta:ff00ff maroon:800000 mediumaquamarine:66cdaa
    mediumblue:0000cd mediumorchid:ba55d3 mediumpurple:9370db mediumseagreen:3cb371
    mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc mediumvioletred:c71585
    midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1 moccasin:ffe4b5 navajowhite:ffdead
    navy:000080 oldlace:fdf5e6 olive:808000 olivedrab:6b8e23 orange:ffa500 orangered:ff4500
    orchid:da70d6 palegoldenrod:eee8aa palegreen:98fb98 paleturquoise:afeeee palevioletred:db7093
    papayawhip:ffefd5 peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd powderblue:b0e0e6
    purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f royalblue:4169e1
    saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 seashell:fff5ee
    sienna:a0522d silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd slategray:708090 slategrey:708090
    snow:fffafa springgreen:00ff7f steelblue:4682b4 tan:d2b48c teal:008080 thistle:d8bfd8
    tomato:ff6347 turquoise:40e0d0 violet:ee82ee wheat:f5deb3 white:ffffff whitesmoke:f5f5f5
    yellow:ffff00 yellowgreen:9acd32
    """,
    )
)

# A rectangle of user space: (left, top, right, bottom).
Rect = tuple[float, float, float, float]
EVERYWHERE: Rect = (-math.inf, -math.inf, math.inf, math.inf)
# Room for the rounding of a coordinate written with few digits.
_EPSILON = 1e-6


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


def _inverse(affine: Affine) -> Affine | None:
    """The map that undoes an affine one; None for one that flattens the plane."""
    a, b, c, d, e, f = affine
    determinant = a * d - b * c
    if abs(determinant) < 1e-12:
        return None
    a2, b2, c2, d2 = d / determinant, -b / determinant, -c / determinant, a / determinant
    return (a2, b2, c2, d2, -(a2 * e + c2 * f), -(b2 * e + d2 * f))


def _inside(rect: Rect, point: Point, *, strictly: bool = False) -> bool:
    left, top, right, bottom = rect
    x, y = point
    if strictly:
        return left + _EPSILON < x < right - _EPSILON and top + _EPSILON < y < bottom - _EPSILON
    return left - _EPSILON <= x <= right + _EPSILON and top - _EPSILON <= y <= bottom + _EPSILON


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


def _rgba(text: str, paint: Mapping[str, str]) -> tuple[str, float] | None:
    """A paint as one spelling of its colour (`#rrggbb`, whichever way SVG lets it be written)
    and its alpha; None for no paint. A paint the grader does not know (a gradient) is itself."""
    value = text.strip().casefold()
    if value == "currentcolor":
        value = paint["color"].strip().casefold()
    if value in ("none", "transparent", ""):
        return None
    channels: list[float] | None = None
    alpha = 1.0
    if value in _NAMED:
        return "#" + _NAMED[value], 1.0
    if re.fullmatch(r"#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})", value):
        digits = value[1:] if len(value) > 5 else "".join(digit * 2 for digit in value[1:])
        channels = [int(digits[at : at + 2], 16) / 255 for at in (0, 2, 4)]
        alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
    elif call := re.fullmatch(r"(rgba?|hsla?)\((.*)\)", value):
        parts = [part for part in re.split(r"[\s,/]+", call.group(2).strip()) if part]
        if len(parts) not in (3, 4):
            return value, 1.0
        try:
            numbers = [float(part.removesuffix("%").removesuffix("deg")) for part in parts]
        except ValueError:
            return value, 1.0
        percent = [part.endswith("%") for part in parts]
        if call.group(1).startswith("rgb"):
            channels = [number / 100 if is_percent else number / 255 for number, is_percent in zip(numbers, percent)]
            channels = channels[:3]
        else:
            red, green, blue = colorsys.hls_to_rgb((numbers[0] % 360) / 360, numbers[2] / 100, numbers[1] / 100)
            channels = [red, green, blue]
        if len(parts) == 4:
            alpha = numbers[3] / 100 if percent[3] else numbers[3]
    if channels is None:
        return value, 1.0
    alpha = min(max(alpha, 0.0), 1.0)
    if not alpha > 0:
        return None
    return "#" + "".join(f"{round(min(max(one, 0.0), 1.0) * 255):02x}" for one in channels), alpha


def _colour(text: str, paint: Mapping[str, str]) -> str | None:
    """A paint as one spelling of its colour; None for no paint."""
    rgba = _rgba(text, paint)
    return rgba[0] if rgba else None


@dataclass(frozen=True)
class _Drawn:
    """How an element is placed and painted, and the regions of the figure it may show in: each
    a rectangle of a user space (the viewport of an `svg`, a clip) and the map from that space to
    the figure."""

    affine: Affine
    paint: Mapping[str, str]
    opacity: float
    shown: bool
    clips: tuple[tuple[Affine, Rect], ...] = ()

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

    def opaque_fill(self) -> str | None:
        """The colour it is filled with when nothing shows through the fill, else None."""
        rgba = _rgba(self.paint["fill"], self.paint)
        if not self.visible() or rgba is None or rgba[0].startswith("url"):
            return None
        return rgba[0] if min(rgba[1], self.opacity, _fraction(self.paint["fill-opacity"])) >= 1 else None

    def visible(self) -> bool:
        return self.shown and self.opacity > 0 and self.paint["visibility"].strip() == "visible"

    def lettered(self) -> bool:
        """Whether its text has a size to be seen at."""
        size = _length(self.paint["font-size"])
        return self.visible() and (size is None or size > 0)

    def shows(self, point: Point) -> bool:
        """Whether a point of the figure is inside every region this element may show in."""
        for affine, rect in self.clips:
            inverse = _inverse(affine)
            if inverse is None or not _inside(rect, _apply(inverse, *point)):
                return False
        return True


def _declared(element: ElementTree.Element) -> dict[str, str]:
    """The paint an element sets: its presentation attributes, then its `style` attribute."""
    names = (*_INHERITED, "display", "opacity", "clip-path", "mask", "filter")
    declared = {name: value for name in names if (value := element.get(name))}
    for declaration in (element.get("style") or "").split(";"):
        name, colon, value = declaration.partition(":")
        if colon:
            declared[name.strip().casefold()] = value.replace("!important", "").strip()
    return declared


def _size(text: str | None) -> float | None:
    """A width or a height in user units; None when it is not set or is relative (`100%`)."""
    if text is None or not re.fullmatch(_NUMBER.pattern + r"(?:px)?", text.strip()):
        return None
    return _finite(text.strip().removesuffix("px"), FIGURE)


def _viewport(element: ElementTree.Element, nested: bool) -> tuple[Affine, Rect] | None:
    """What an `svg` element shows: the map from its user space to the space around it, and the
    rectangle of its user space its viewport shows (`viewBox` and `preserveAspectRatio` as SVG
    reads them, a size it does not set as large as its content); None when it shows nothing."""
    x = (_length(element.get("x")) or 0.0) if nested else 0.0
    y = (_length(element.get("y")) or 0.0) if nested else 0.0
    width, height = _size(element.get("width")), _size(element.get("height"))
    if (width is not None and not width > 0) or (height is not None and not height > 0):
        return None
    try:
        box = _numbers(element.get("viewBox") or "", FIGURE)
    except _Unreadable:
        box = []
    if len(box) != 4:  # no viewBox, or one SVG ignores
        right = x + width if width is not None else math.inf
        bottom = y + height if height is not None else math.inf
        return (1, 0, 0, 1, x, y), (0.0, 0.0, right - x, bottom - y)
    box_x, box_y, box_width, box_height = box
    if not (box_width > 0 and box_height > 0):
        return None
    if width is None:
        width = box_width if height is None else height * box_width / box_height
    if height is None:
        height = width * box_height / box_width
    words = [word for word in (element.get("preserveAspectRatio") or "").split() if word != "defer"]
    align = words[0].casefold() if words else "xmidymid"
    if align == "none":
        scale_x, scale_y, at_x, at_y = width / box_width, height / box_height, 0.0, 0.0
    else:
        fit = max if words[1:2] == ["slice"] else min
        scale_x = scale_y = fit(width / box_width, height / box_height)
        at_x = (width - box_width * scale_x) * {"xmin": 0.0, "xmax": 1.0}.get(align[:4], 0.5)
        at_y = (height - box_height * scale_y) * {"ymin": 0.0, "ymax": 1.0}.get(align[4:], 0.5)
    affine: Affine = (scale_x, 0, 0, scale_y, x + at_x - scale_x * box_x, y + at_y - scale_y * box_y)
    shown = (box_x - at_x / scale_x, box_y - at_y / scale_y)
    return affine, (*shown, shown[0] + width / scale_x, shown[1] + height / scale_y)


def _clip(value: str | None, ids: Mapping[str, ElementTree.Element]) -> Rect | None:
    """The rectangle a `clip-path` lets an element show in, of its user space: everywhere for
    none, None for a clip the grader does not read (anything but one plain `rect`)."""
    if value is None or value.strip() in ("", "none"):
        return EVERYWHERE
    reference = re.fullmatch(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)", value.strip())
    target = ids.get(reference.group(1)) if reference else None
    if (
        target is None
        or _local(target.tag) != "clipPath"
        or target.get("transform")
        or target.get("clipPathUnits", "userSpaceOnUse") != "userSpaceOnUse"
    ):
        return None
    shapes = list(target)
    if len(shapes) != 1 or _local(shapes[0].tag) != "rect" or shapes[0].get("transform"):
        return None
    left, top, width, height = (_length(shapes[0].get(name, "0")) for name in ("x", "y", "width", "height"))
    if left is None or top is None or width is None or height is None:
        return None
    return (left, top, left + width, top + height)


def _placed(
    element: ElementTree.Element,
    around: _Drawn,
    ids: Mapping[str, ElementTree.Element],
    nested: bool = False,
) -> Iterator[tuple[ElementTree.Element, _Drawn]]:
    """Every element of a tree, in the order SVG paints them, with how each is placed and painted
    in the figure."""
    declared = _declared(element)
    tag = _local(element.tag)
    paint = dict(around.paint)
    paint.update({name: value for name, value in declared.items() if name in _INHERITED and value != "inherit"})
    shown = (
        around.shown
        and declared.get("display", "").strip() != "none"
        and tag not in _UNRENDERED
        # A mask or a filter may hide what it applies to, and the grader does not read either.
        and all(declared.get(name, "none").strip() in ("", "none") for name in ("mask", "filter"))
    )
    affine = _then(around.affine, _transform(element.get("transform")))
    clips = around.clips
    clip = _clip(declared.get("clip-path"), ids)
    if clip is None:
        shown = False
    elif clip != EVERYWHERE:
        clips = (*clips, (affine, clip))
    if tag == "svg":
        viewport = _viewport(element, nested)
        if viewport is None:
            shown = False
        else:
            affine = _then(affine, viewport[0])
            clips = (*clips, (affine, viewport[1]))
    own = _Drawn(affine, paint, around.opacity * _fraction(declared.get("opacity")), shown, clips)
    yield element, own
    for child in element:
        yield from _placed(child, own, ids, nested=True)


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
    seen: bool
    """Whether every point of it shows: inside the figure, uncovered, apart from what is behind."""


@dataclass(frozen=True)
class _Text:
    content: str
    """What it says, its spaces folded, in lower case."""
    at: Point
    colours: frozenset[str]


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


def _paints(drawn: _Drawn, *, line: bool = False) -> frozenset[str]:
    return frozenset(one for one in (drawn.stroke(), None if line else drawn.fill()) if one)


def _drawing(root: ElementTree.Element) -> _Figure:
    """The lines, texts and marks an SVG draws, each where it shows: inside the viewport and its
    clips, not under a rectangle painted later and opaque, and in a colour apart from what is
    painted behind it (the last such rectangle painted before it, or a white page)."""
    ids = {identifier: element for element in root.iter() if (identifier := element.get("id"))}
    placed = list(_placed(root, _Drawn(IDENTITY, _INHERITED, 1.0, shown=True), ids))
    drawn_of = {id(element): drawn for element, drawn in placed}
    # The opaque rectangles, axis-aligned in the figure, that hide what is under them.
    covers: list[tuple[int, Rect, str]] = []
    for index, (element, drawn) in enumerate(placed):
        _a, b, c, _d, _e, _f = drawn.affine
        colour = drawn.opaque_fill()
        if _local(element.tag) != "rect" or colour is None or b or c or len(drawn.clips) > 1:
            continue
        corners = [_apply(drawn.affine, x, y) for x, y in _mark_points(element)]
        if corners:
            xs, ys = [x for x, _y in corners], [y for _x, y in corners]
            covers.append((index, (min(xs), min(ys), max(xs), max(ys)), colour))

    def seen(index: int, drawn: _Drawn, colours: frozenset[str], point: Point) -> bool:
        if not drawn.shows(point):
            return False
        if any(later > index and _inside(rect, point, strictly=True) for later, rect, _colour in covers):
            return False
        behind = next(
            (colour for earlier, rect, colour in reversed(covers) if earlier < index and _inside(rect, point)), _PAGE
        )
        return bool(colours - {behind})

    def lettering(text: ElementTree.Element) -> frozenset[str]:
        """The colours a text's characters are painted in, from the elements that hold them."""
        colours: set[str] = set()
        for holder in text.iter():
            if (holder.text or "").strip() and drawn_of[id(holder)].lettered():
                colours |= _paints(drawn_of[id(holder)])
            for child in holder:
                if (child.tail or "").strip() and drawn_of[id(holder)].lettered():
                    colours |= _paints(drawn_of[id(holder)])
        return frozenset(colours)

    lines: dict[str, _Line] = {}
    texts: list[_Text] = []
    marks: list[_Mark] = []
    for index, (element, drawn) in enumerate(placed):
        tag, identifier = _local(element.tag), element.get("id")
        if identifier in LINE_IDS:
            if identifier in lines:
                raise _Unreadable(f"{FIGURE} has two elements of id {identifier}")
            points = [_apply(drawn.affine, x, y) for x, y in _line_points(element, identifier)]
            colours = _paints(drawn, line=True)
            shows = all(seen(index, drawn, colours, point) for point in points)
            lines[identifier] = _Line(points, drawn.stroke(), shows)
        elif tag == "text" and drawn.visible():
            content = " ".join("".join(element.itertext()).split()).casefold()
            at = _apply(drawn.affine, *_first_character(element, None, None))
            colours = lettering(element)
            if colours and seen(index, drawn, colours, at):
                texts.append(_Text(content, at, colours))
        elif tag in _MARKS and drawn.visible():
            colours = _paints(drawn, line=tag == "line")
            points = [_apply(drawn.affine, x, y) for x, y in _mark_points(element)]
            if colours and points:
                xs, ys = [x for x, _y in points], [y for _x, y in points]
                middle = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
                if seen(index, drawn, colours, middle):
                    marks.append(_Mark(middle, colours))
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
            return set(text.colours)
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
        elif not line.seen:
            faults.append(f"{FIGURE} does not show {identifier} at every point: outside the figure, or covered")
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
