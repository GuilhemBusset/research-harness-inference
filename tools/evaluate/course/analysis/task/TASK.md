# Task: two running totals, from two agents at once, in one figure

`data.csv` holds paired measurements: for each `x`, the amount `a` of one source and the amount
`b` of the other in that step. `instance.json` names the two sources (`series_a` and `series_b`),
the labels of the figure's axes (`x_label` and `y_label`) and the comparison to report
(`comparison`). Do not change either file. Another copy of this task has other data, other names
and maybe another comparison.

## 1. Two agents, in parallel

Start two agents in parallel, in one message, with your tool that starts agents, and let both
work at the same time:

- the first writes `series_a.csv`, with the columns `x,y`: for each row of `data.csv`, in order,
  its `x` and the running total of `a`, the sum of `a` over that row and every row before it;
- the second writes `series_b.csv` in the same way, with the running total of `b`.

## 2. The figure

When both have finished, write `figure.svg`, a line chart of both series, with the Python
standard library only. Install nothing.

- Draw each series as one `<polyline>`, or one `<path>` of connected straight segments (one
  moveto, then linetos), with `id="series_a"` and `id="series_b"`, with one point for each row of
  `data.csv`, in the order of `x`. Give the two lines two different stroke colours.
- Both lines share one linear x axis, increasing to the right, and one linear y axis, increasing
  upward.
- Label the axes with a `<text>` each, holding exactly `x_label` and `y_label`: the x axis label
  below the lines, the y axis label to their left. A tick label, a `<text>` holding only a number
  below the lines or left of them, sits where that number is on its axis.
- Add a legend: for each source, a `<text>` that holds its name, from `instance.json`, beside a
  mark (a short line, a square) in the colour of its line.
- Set colours and visibility with attributes (`stroke="..."`, or `style="stroke: ..."`) on the
  elements or the groups around them, not in a `<style>` sheet.
- Keep the lines, the labels, the ticks and the legend inside what the figure shows (its `width`
  and `height`, or its `viewBox`), uncovered, and each in a colour apart from what is behind it.

## 3. The comparison

Write `result.json`, a JSON object with two fields: `comparison`, the name from `instance.json`,
and `value`, its number:

- `largest_gap`: the largest distance between the two running totals at one `x`, the largest
  absolute value of A − B;
- `final_difference`: the running total of A less that of B, at the last `x`.

Answer with one sentence saying what the figure shows and what the comparison says.

## How it is checked

The files are read after the run, by a check that is not in this folder. It establishes that the
two agents were running at the same time, that both series and every plotted point are what
`data.csv` gives, that the axes, their ticks and the legend say what the lines show, and that
`result.json` holds the comparison `data.csv` gives. A number of a series or of the comparison may
be off by 0.005; a plotted point may be off its axes by 1% of the lines' extent, and a tick label
by 5%. Any size, margin, colour and scale of the figure is accepted.
