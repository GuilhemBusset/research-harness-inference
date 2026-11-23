+++
id = "E3-50"
title = "Spike S5 counts the engine's counters with queries Amazon Managed Prometheus accepts"
labels = ["size:M", "kind:test"]
epic = 5
+++

## Context

Spike S5's first run on the coursework pool (2026-11-23, run `8bit-baseline-1`) replayed its
first 600-second load, then failed reading what the engine measured: the stage's metrics workspace,
Amazon Managed Prometheus, answers HTTP 400 to every query that uses the range modifier
`anchored` ("parse error: anchored modifier is experimental and not enabled"). The job stopped
before it posted anything.

`reads()` in `spikes/s5_capacity.py` builds every counter's read as
`sum by (...) (increase(<counter>[<range>] anchored))`: 15 of the job's 22 queries for a vllm pool
(the input and cached tokens, the first-token and per-token histograms, output tokens, decode and
prefill times, preemptions, the input and output distributions). Plain `increase` is not a
substitute: it extrapolates to the range's edges, which the record's counts must not do.

The job already reads, for each of these counters, its raw samples over the run's window (`raw`)
and the value each series held at the run's start (`there`), and `counted_of` combines them with
the anchored answer. The increase the record needs can be computed from those two reads alone:
from the last sample at or before the run's start to the last sample of the window, a counter
reset counted as the counter restarting from zero, as `anchored` defines it. Nothing else in the
repository uses the modifier.

The stand-in workspace of `tests/unit/spikes/test_s5_runs.py` evaluates `anchored` itself, which
is how the tests passed while the real workspace refuses the query.

## Deliverables

- [ ] `spikes/s5_capacity.py` sends the workspace no query that holds the anchored modifier, and computes each counter's increase over a run from the samples it reads: from the last sample at or before the run's start to the window's last sample, with each counter reset counted as a restart from zero.
- [ ] The stand-in workspace of `tests/unit/spikes/test_s5_runs.py` refuses, as Amazon Managed Prometheus does, any query that holds the anchored modifier.

## Acceptance criteria

- [ ] `tests/unit/spikes/test_s5_runs.py` holds that no query the job sends holds the anchored modifier, and that the counts of a run with a counter reset and a series that starts inside the window are those the anchored increase gave.
- [ ] `uv run --locked pytest tests/unit/spikes/test_s5.py tests/unit/spikes/test_s5_runs.py` passes.
- [ ] A build of `rhi-spike-s5_capacity` on the coursework pool reads every run's counters with no error from the workspace and posts its runs (needs gpu:capacity).

## References

- `spikes/s5_capacity.py`
- `docs/measurements/s5.md`
- `docs/design/system-design.md`, section 7.2
