+++
id = "E7-03"
title = "Freeze course-shaped conversations before replay"
labels = ["size:M", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

The S5 synthetic generator appends actual responses. Controlled comparisons need canonical synthetic assistant/tool turns while preserving the production request path.

## Deliverables

- [ ] `tools/evaluate/traces.py` and `tools/evaluate/fixtures/replay/` build deterministic scrubbed Messages conversations compatible with the recorded-session reader, including a common opening, growing context, tool definitions, calls, results and pauses.
- [ ] The generator applies the S5 sizes, per-repetition prefix isolation and paired prefix bytes, recording requested lengths and a trace hash separately from observed usage.
- [ ] `tools/evaluate/tests/test_traces.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_traces.py` varies a mocked reply while verifying later request bytes, tool turns and pauses from the same seed stay identical.
- [ ] `tools/evaluate/tests/test_traces.py` checks growing prefixes, the shared opening, fresh repetition namespace and changed trace with a different seed, and passes generated fixtures through the repository hygiene check.

## References

- `docs/design/agent-session-evaluation.md`, Workload and measurement protocol
- `spikes/loadgen.py`
- `spikes/s5_capacity.py`
