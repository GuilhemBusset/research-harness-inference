+++
id = "E7-31"
title = "Stop a capacity request first eligible at admissions close from counting as a sample"
labels = ["size:S", "kind:app"]
epic = 744
+++

## Context

`tools/evaluate/capacity.py` reads the repetition samples of a capacity cell. The replay scheduler stops admitting requests when its clock reaches the deadline, so a request whose eligibility and send time both equal `admissions_ended_ms` was never admitted as a sample. Today such a request supplies the hundredth later turn and the hundredth stream, and turns an insufficient repetition into a pass: 99 eligible later turns plus that request give 100 and the verdict `pass`.

## Deliverables

- [ ] `tools/evaluate/capacity.py` leaves a request that is eligible at or after `admissions_ended_ms` out of the later-turn and stream samples, and keeps its send in the attempt denominator.
- [ ] `tools/evaluate/tests/test_capacity.py` holds a test with 99 eligible later turns and one request eligible and sent at `admissions_ended_ms`.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_capacity.py` shows that cell reads 100 attempts, 99 later turns, 99 speeds and the verdict `insufficient_evidence`.
- [ ] `tools/evaluate/tests/test_capacity.py` shows a request eligible one millisecond before `admissions_ended_ms` still counts as a sample.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_capacity.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
