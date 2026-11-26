+++
id = "E7-30"
title = "Grade research units by what they measure and never raise on a bad exponent"
labels = ["size:S", "kind:app"]
epic = 744
+++

## Context

`tools/evaluate/course/research/grader.py` parses a conclusion's unit into the quantity it measures. Two units misgrade. `kg/mm` leaves all four properties passing against a reference of `kg/m²`, because after the millimetre reading fails to match a graded quantity the parser splits `mm` into two metre symbols; `kg/mm` is mass per length. `kg/km^200` raises `OverflowError` while the readings of the unit are generated, outside the `try` of `_unit`, so grading returns no report.

## Deliverables

- [ ] `tools/evaluate/course/research/grader.py` never reads a prefixed unit as the product of its parts when the prefixed unit is a symbol, so `kg/mm` is mass per length and fails the unit property against `kg/m²`.
- [ ] `tools/evaluate/course/research/grader.py` returns a report that rejects a unit whose exponent or factor is out of range, and raises nothing.
- [ ] `tools/evaluate/tests/test_research.py` holds a test for each of the two units.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_research.py` shows changing only the first reference conclusion's unit to `kg/mm` fails the unit property alone.
- [ ] `tools/evaluate/tests/test_research.py` shows changing only that unit to `kg/km^200` returns a report that fails the unit property alone.
- [ ] `tools/evaluate/tests/test_research.py` shows the units in other notations that the existing tests accept are still accepted.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_research.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
