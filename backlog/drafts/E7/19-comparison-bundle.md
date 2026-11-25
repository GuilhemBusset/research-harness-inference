+++
id = "E7-19"
title = "Render and export reproducible scorecards from saved evidence"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-02", "E7-08", "E7-18"]
+++

## Context

A fresh checkout must explain the limiting rows from public evidence offline. The publisher later posts the same results to the experiment issue.

## Deliverables

- [ ] `tools/evaluate/report.py` reads the versioned manifest and append-only observations to regenerate summary JSON, Markdown and a digest, showing capacity and quality by shape, harness and load plus configured versus observed conditions.
- [ ] `tools/evaluate/cli.py` exposes offline compare, prints the treatment diff first and refuses incompatible workloads, floors or sample budgets.
- [ ] The report recommends only tested host counts meeting both quality checks and capacity for each applicable shape.
- [ ] Explicit export validates and hygiene-scans the complete numeric evidence bundle for review under `docs/evaluations/`, using only run-relative times and allow-listed fields.
- [ ] The report compares reference replay with live task distributions by shape for input/output, cached share, tools, fan-out, route mix and turns, and shows task duration and completions per hour across tokenizers.
- [ ] `tools/evaluate/tests/test_report.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_report.py` reads a saved fixture offline, reproduces its scorecard and digest, and identifies different limiting rows for hands-on and several-agents workloads.
- [ ] `tools/evaluate/tests/test_report.py` compares genuinely paired treatments, refuses a changed floor or missing provenance for a flags-only treatment and reports insufficient evidence without extrapolating capacity.
- [ ] `tools/evaluate/tests/test_report.py` exports a safe bundle and rejects a private label, raw model text or path before writing a public export.

## References

- `docs/design/agent-session-evaluation.md`, Reading a result; Reproducing and comparing runs
