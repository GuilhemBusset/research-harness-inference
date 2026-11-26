+++
id = "E7-29"
title = "Read SVG percentage sizes against the viewport they refer to"
labels = ["size:S", "kind:app"]
epic = 744
+++

## Context

The analysis grader reads the figure's geometry in `tools/evaluate/course/analysis/grader.py`. A width or height of `0%` on the outermost `svg` is read as unknown, because the grader has no window size, so the figure earns full credit for plotted data although it shows nothing. A nested `svg` that sets no width and height is given an unset viewport size, so its percentage-sized children raise "no viewport of set size", although SVG sizes it at 100% of the viewport around it, and the same figure with explicit `100%` passes.

## Deliverables

- [ ] `tools/evaluate/course/analysis/grader.py` reads a zero percent width or height of any `svg` as zero, so the viewport shows nothing and the plotted-data property fails.
- [ ] `tools/evaluate/course/analysis/grader.py` gives a nested `svg` that sets no width or height the size of the viewport around it, as it would with `100%`.
- [ ] `tools/evaluate/tests/test_analysis.py` holds a test for each of the two cases, built from the reference figure.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_analysis.py` shows changing only the reference figure's outer width to `0%` fails the plotted-data property.
- [ ] `tools/evaluate/tests/test_analysis.py` shows a nested `svg` with explicit `100%` width and height and one with both attributes removed give the same grade.
- [ ] `tools/evaluate/tests/test_analysis.py` shows the committed reference figure is still accepted.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_analysis.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
