+++
id = "E4-12"
title = "Dashboard: inference performance, each pool's speed and first token against what was predicted"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-61", "E2-68", "E2-74", "E2-84", "E3-06", "E3-10", "E4-31"]
+++

## Context

Requirement R9 asks what the chosen model and machines serve, in tokens per second and the other
numbers that matter, measured. Section 4.7 estimates it, and the inference performance dashboard
shows the measurement beside the prediction, with one row per pool (section 7.3). This issue builds
the dashboard and its speed and latency panels; the cache, the load and the economics of each pool
are issue E4-29, and the session view issue E4-28.

Each quantity has two sources where it can (section 7.2). The engine sees its own work; its metric
names come from the per-engine table, at the row of the pool's engine (vLLM's at version 0.33 in
issue E3-06; the mock's in issue E2-82). The gateway sees what the student got, which adds the wait
in front of the engine, the hold and the network: its histograms per pool and wire format, never
labelled by student, are issues E2-74's and E2-84's, and their metric names are the ones
`services/gateway/rhi_gateway/experience.py` emits. The panels of a row, from section
7.2:

- Output speed per stream. The engine's mean, as output tokens over decode time, is the headline,
  because a ratio of two sums needs no buckets; the median and the slowest tenth come from the
  engine's time per output token; the gateway's histogram of tokens per second for each request
  sits beside them. The instance's computed-input rate is plotted on the same panel, since that is
  what slows a stream.
- Output throughput, per pool and per instance: the engine's output tokens, and the gateway's output
  tokens settled.
- Time to first token: the engine's median and 95th percentile, with its two parts, queue time and
  prefill time; the gateway's, labelled warm or held and first turn or later, and the time to the
  first token that is not reasoning, which is when a student sees an answer begin.
- Input throughput, with the part actually computed beside it: the engine's input counter includes
  cached tokens, so "input tokens per second" is not work done (section 7.2, fifth rule). The
  gateway's rate of input tokens settled is its second source.
- Prefill speed: tokens computed over prefill time.
- Transcript size and output per turn, the two assumptions under the estimate, from the engine and
  from the gateway.
- Cached share of input: cached input tokens over input tokens. When the request profile fails, the
  share falls to about a third; when affinity fails, to about half (section 7.3).

**What a pool's engine does not give.** Several of these series read quantities an engine's row may
leave out (issue E3-06): the computed input, the queue, prefill and decode times, and the input and
output tokens per request. vLLM's row names them; the mock's does not. So in a pool's row, a series
whose quantity the row of the pool's engine leaves out is not drawn, and a panel left with no series
is not shown. Under the example configuration the coursework pool's row holds the seven panels; the
mock pool's row has no prefill-speed panel (its two quantities are both left out), and its other
panels carry the mock's series that exist and the gateway's.

Each pool's configuration may carry its expected values for speed per stream, time to first token
and cache capacity, each as a low and a high bound, in the pool's field `expected`, whose keys are
`stream_tokens_per_second`, `time_to_first_token_seconds` and `cache_tokens` (section 4.7; issue
E2-68); the coursework pool's are section 4.7's estimates, 15 to 22 tokens per second and 0.3 to 1.5
seconds for a cached turn.
They are shown as reference lines on the speed and first-token panels, so that a pool that serves
less than predicted is visible at a glance. The provisional floors and the cached-share flag come
from configuration (issue E4-31). The example configuration's GPU pool is the coursework pool
(issue E3-10).

## Deliverables

- [ ] `dashboards/rhi_dashboards/inference.py` defines the inference performance dashboard with one row per pool of the configuration, holding the panels of the list above, each with the engine's series and the gateway's where the list names both; a series whose quantity the row of the pool's engine leaves out is not drawn, and a panel left with no series is not shown.
- [ ] The speed panel shows the low and high bounds of the pool's expected speed per stream and the two speed floors as reference lines, and the first-token panel the bounds of the pool's expected time to first token and the 5-second floor, all read from the configuration; a pool that gives no expected values shows only the floors.
- [ ] The cached-share panel flags the pool when its share falls below the configured threshold, 85% by default.
- [ ] Every engine query takes its names from the per-engine table at the pool's engine, every percentile is shown with its request count, and every rate is taken per instance and then summed, through the helpers of issue E2-61.
- [ ] `tests/unit/dashboards/test_inference.py` builds the dashboard from the example configuration, whose pools run the mock and vLLM, and checks its panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_inference.py` passes: one row per pool; the coursework pool's row holds the seven panels, its input panel carrying the computed part beside the input rate and the gateway's settled input; the mock pool's row has no prefill-speed panel and no series on a quantity the mock's row leaves out; and no engine query is on a name outside the table.
- [ ] `tests/unit/dashboards/test_inference.py::test_the_prediction_is_on_the_panel` changes the bounds of the coursework pool's expected speed per stream and expected time to first token in the configuration and finds the new values as the reference lines of its speed and first-token panels, and finds no expected line in the mock pool's row.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the inference performance dashboard in the package.
- [ ] In the account, during a session on the coursework pool, the dashboard shows the pool's measured speed per stream and time to first token beside its expected values (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 1, 4.1, 4.7, 7.2, 7.3
- `docs/design/evidence.md`, E3
