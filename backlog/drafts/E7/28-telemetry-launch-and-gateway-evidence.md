+++
id = "E7-28"
title = "Read launch identity and gateway baselines as evidence, not as presence"
labels = ["size:M", "kind:app"]
epic = 744
+++

## Context

Four readings in `tools/evaluate/telemetry.py` accept evidence that does not show what they claim. A gateway counter that exists before the window with an unreadable baseline is counted from zero, so one cohort counter reading NaN for the first minute and then 24 reconciles as measured traffic. `preflight` takes any valid launch fingerprint in its lookback, so a host whose latest observation is `unavailable` still passes. A host whose agent says `unavailable` throughout the warm-up and names its engine only after the window began is marked warmed from the warm-up's tokens, with nothing connecting that work to the engine that served the window. A host that says `unavailable` for longer than the scrape allowance in the middle of a window, with no launch fingerprint requested, leaves the cell with no reason.

## Deliverables

- [ ] `tools/evaluate/telemetry.py` counts a gateway identity from zero only when its series first appears inside the window, and an identity that exists before the window with an unreadable baseline leaves the gateway coverage incomplete and the counters unreconciled.
- [ ] `tools/evaluate/telemetry.py` makes `preflight` use each host's latest launch observation in its lookback, and report launch provenance as missing when that observation is `unavailable`.
- [ ] `tools/evaluate/telemetry.py` keeps the warm-up's observed token increase and marks the host unwarmed when no launch fingerprint seen by the window's first scrape connects the warm-up's work to the engine that served the window.
- [ ] `tools/evaluate/telemetry.py` marks a cell's evidence incomplete when a host's launch identity is `unavailable` for longer than the scrape allowance inside the window, whether or not a launch fingerprint was requested.
- [ ] `tools/evaluate/tests/test_telemetry.py` holds one test for each of the four cases.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_telemetry.py` shows a cohort counter that reads NaN for the first minute of the window after existing before it reports incomplete gateway coverage and unreconciled counters.
- [ ] `tools/evaluate/tests/test_telemetry.py` shows a preflight at 1,000 seconds with a valid fingerprint at 973 seconds and `unavailable` at 988 seconds lists the launch provenance as missing.
- [ ] `tools/evaluate/tests/test_telemetry.py` shows a host that says `unavailable` through the warm-up and names its engine 3 seconds into the window keeps 600 warm-up tokens and is not warmed.
- [ ] `tools/evaluate/tests/test_telemetry.py` shows a 45 second `unavailable` gap in a host's launch identity gives the cell a coverage reason with no requested fingerprint.
- [ ] `tools/evaluate/tests/test_telemetry.py` shows an identity whose series begins inside the window still counts from its first request and a clean window still reconciles.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_telemetry.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
