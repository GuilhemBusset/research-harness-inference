+++
id = "E4-29"
title = "Dashboard: inference performance, each pool's cache, load and cost per token"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-66", "E2-68", "E2-79", "E3-05", "E4-01", "E4-12"]
+++

## Context

The inference performance dashboard answers R9 for each pool, one row per pool (section 7.3). Issue
E4-12 built it with the speed and latency panels; this issue adds the rest of each row, from the
table of section 7.2:

- Cache use against capacity: the engine's cache use and its preemptions, per instance; the capacity
  in tokens that the engine logs at start, which the lifecycle agent exports as a number because
  vLLM's own metric carries it only as a label (issue E2-66), shown beside the pool's expected
  cache capacity from configuration, the bounds `cache_tokens` of the pool's field `expected`
  (section 4.7; issue E2-68).
- Sessions the cache forgot: how long a block sat idle before it was evicted, from the engine's
  cache metrics. Affinity does not help when the cache itself is full: the engine evicts the
  prefixes of idle sessions first (section 5.5).
- Concurrent streams and the queue: the engine's running and waiting requests, per instance; the
  gateway's requests in flight and held, per pool, by the names
  `services/gateway/rhi_gateway/experience.py` emits (issue E2-74).
- Students per instance: the students with a request in the last five minutes, counted as the
  aliases of the gateway's per-student request counter for the pool (issue E2-79), over the pool's
  instances in service from the scaler. It is an average only: the gateway cannot know which
  instance serves a student (section 5.5), so imbalance shows as one instance's queue beside
  another's.
- Cost per million output tokens: the scaler's accrued dollars for the pool (issue E4-01) over the
  gateway's output tokens, over the dashboard's time range and for the month. Section 4.7 estimates
  $5.20 to $7.80 on the coursework pool with eight agent streams; an idle instance costs the same
  per hour.
- Tokens per GPU-hour and per joule: output throughput over the instances in service, and over the
  rate of the DCGM exporter's energy counter (issue E3-05).

The engine's metric names come from the per-engine table at the pool's engine. As in issue E4-12, a
series whose quantity the row of the pool's engine leaves out is not drawn, and a panel left with no
series is not shown: the mock's row leaves out preemptions and how long a block sat idle before it
was evicted (issue E3-06), so the mock pool's row has its cache panel without preemptions and no
panel on the sessions the cache forgot. A pool whose engine is the mock has no GPU, so its row has
no panel on tokens per GPU-hour and per joule either.

## Deliverables

- [ ] `dashboards/rhi_dashboards/inference.py` adds to each pool's row the panels of the list above, with the sources it names, by the rule of issue E4-12 for a quantity the pool's engine leaves out.
- [ ] The cache panel shows the capacity the lifecycle agent reports beside the low and high bounds of the pool's expected cache capacity from the configuration, and a pool that gives no expected values shows the reported capacity alone.
- [ ] The students-per-instance panel counts each alias once for a pool, however many gateway tasks counted its requests, and says in its title that it is an average.
- [ ] The cost panel says estimate in its title, and the panel on tokens per GPU-hour and per joule is only in the rows of pools whose engine is not the mock.
- [ ] `tests/unit/dashboards/test_inference.py` checks the new panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_inference.py` passes: the coursework pool's row has the six panels, the mock pool's row has all but the one on the sessions the cache forgot and the one on tokens per GPU-hour and per joule, and its cache panel has no preemption series, the cost panel's title says estimate, and the cache panel carries the expected capacity's bounds from the example configuration.
- [ ] `tests/unit/dashboards/test_inference.py::test_a_student_counts_once` shows the students-per-instance query counting one alias whose requests two gateway tasks reported as one student.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the new panels.

## References

- `docs/design/system-design.md`, sections 4.7, 5.5, 7.2, 7.3
