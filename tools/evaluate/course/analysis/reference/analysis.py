"""The reference solution of the parallel-analysis task, with the standard library only.

Run in a workspace (`data.csv` and `instance.json` beside it, or in the current folder), it
writes what `TASK.md` asks for: the two running totals, `series_a.csv` and `series_b.csv`, their
line chart, `figure.svg`, and the named comparison, `result.json`. A trial's two workers would
write one series each; here one program writes both.

    python analysis.py [workspace]
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

WIDTH, HEIGHT = 640, 400
LEFT, RIGHT, TOP, BOTTOM = 80, 140, 30, 60
COLOURS = ("#1f77b4", "#d62728")


def running_total(values: list[float]) -> list[float]:
    totals, total = [], 0.0
    for value in values:
        total += value
        totals.append(total)
    return totals


def number(value: float) -> str:
    return f"{round(value, 6):g}"


def write_series(path: Path, x: list[float], y: list[float]) -> None:
    path.write_text("x,y\n" + "".join(f"{number(i)},{number(j)}\n" for i, j in zip(x, y, strict=True)), "utf-8")


def figure(x: list[float], series: list[list[float]], brief: dict) -> str:
    low, high = min(min(one) for one in series), max(max(one) for one in series)
    low, high = min(low, 0.0), high if high > low else low + 1

    def at(i: float, j: float) -> str:
        sx = LEFT + (i - x[0]) / (x[-1] - x[0]) * (WIDTH - LEFT - RIGHT)
        sy = HEIGHT - BOTTOM - (j - low) / (high - low) * (HEIGHT - TOP - BOTTOM)
        return f"{sx:.2f},{sy:.2f}"

    names = (brief["series_a"], brief["series_b"])
    middle_x, middle_y = (LEFT + WIDTH - RIGHT) / 2, (TOP + HEIGHT - BOTTOM) / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" font-family="sans-serif">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>',
        f'<line x1="{LEFT}" y1="{HEIGHT - BOTTOM}" x2="{WIDTH - RIGHT}" y2="{HEIGHT - BOTTOM}" stroke="black"/>',
        f'<line x1="{LEFT}" y1="{TOP}" x2="{LEFT}" y2="{HEIGHT - BOTTOM}" stroke="black"/>',
    ]
    for i in x:
        sx, sy = at(i, low).split(",")
        parts.append(f'<text x="{sx}" y="{float(sy) + 16:.2f}" font-size="11" text-anchor="middle">{number(i)}</text>')
    for index, (one, name) in enumerate(zip(series, names, strict=True)):
        points = " ".join(at(i, j) for i, j in zip(x, one, strict=True))
        parts.append(
            f'<polyline id="series_{"ab"[index]}" points="{points}" fill="none" stroke="{COLOURS[index]}" '
            'stroke-width="2"/>'
        )
        legend_y = TOP + 20 * index + 10
        parts.append(
            f'<line x1="{WIDTH - RIGHT + 15}" y1="{legend_y}" x2="{WIDTH - RIGHT + 35}" y2="{legend_y}" '
            f'stroke="{COLOURS[index]}" stroke-width="2"/>'
        )
        parts.append(f'<text x="{WIDTH - RIGHT + 40}" y="{legend_y + 4}" font-size="12">{escape(name)}</text>')
    parts += [
        (
            f'<text x="{middle_x:.2f}" y="{HEIGHT - 15}" font-size="13" text-anchor="middle">'
            f"{escape(brief['x_label'])}</text>"
        ),
        (
            f'<text x="25" y="{middle_y:.2f}" font-size="13" text-anchor="middle" '
            f'transform="rotate(-90 25 {middle_y:.2f})">{escape(brief["y_label"])}</text>'
        ),
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def comparison(name: str, a: list[float], b: list[float]) -> float:
    if name == "largest_gap":
        return max(abs(i - j) for i, j in zip(a, b, strict=True))
    if name == "final_difference":
        return a[-1] - b[-1]
    raise ValueError(f"no comparison {name!r}")


def main(workspace: Path) -> None:
    with (workspace / "data.csv").open(encoding="utf-8", newline="") as data:
        rows = [{key: float(value) for key, value in row.items()} for row in csv.DictReader(data)]
    brief = json.loads((workspace / "instance.json").read_text(encoding="utf-8"))
    x = [row["x"] for row in rows]
    a, b = running_total([row["a"] for row in rows]), running_total([row["b"] for row in rows])
    write_series(workspace / "series_a.csv", x, a)
    write_series(workspace / "series_b.csv", x, b)
    (workspace / "figure.svg").write_text(figure(x, [a, b], brief), encoding="utf-8")
    result = {"comparison": brief["comparison"], "value": round(comparison(brief["comparison"], a, b), 6)}
    (workspace / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd())
