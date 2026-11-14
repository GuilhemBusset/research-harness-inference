+++
id = "E4-31"
title = "The provisional targets of section 7.3, held in configuration"
labels = ["size:S", "kind:infra"]
epic = "E4"
blocked_by = ["E1-03"]
+++

## Context

The speed targets and the cached share of the objectives are provisional and held in configuration
until spike S5 has measured the pool; the estimate of section 4.7 puts the coursework pool at the
speed floor, not clear of it (section 7.3). Section 17 gives their defaults: time to first token
under 5 seconds at the 95th percentile, with the first turns of sessions counted apart; the median
stream at 15 tokens per second or more; the slowest tenth at 10 or more; and a cached share of input
of 85%, below which the dashboard flags a pool.

The same floors are spike S5's pass marks (section 12; issue E3-42), the objectives of issue E4-16
and the lines of the inference performance dashboard (issues E4-12 and E4-29). Holding them in one
place lets the owner move them once S5 has measured, without a change to any of those. The
objectives that are not provisional (99.5% of requests, a cold start under 5 minutes, every pool in
service 30 minutes before a session) are fixed by section 7.3.

The four keys, so that every reader names them the same way: target_ttft_p95_seconds,
target_stream_median_tokens_per_second, target_stream_p10_tokens_per_second and
cached_share_flag_percent. Each has the default of section 17, so that a configuration in SSM
written before this issue loads unchanged. They reach their readers through the loaded
configuration: the dashboards are built from it at synthesis (issue E2-80), and spike S5's verdict
loads it (issue E3-42); no running service reads them.

## Deliverables

- [ ] `src/rhi/config.py` holds the four keys with the defaults of section 17: target_ttft_p95_seconds 5, target_stream_median_tokens_per_second 15, target_stream_p10_tokens_per_second 10 and cached_share_flag_percent 85.
- [ ] `config.example.toml` gives the four keys at their defaults.
- [ ] `tests/unit/test_config.py` covers the four keys.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_config.py` passes: a configuration that leaves the four out loads 5 seconds, 15 and 10 tokens per second, and 85%, and one that sets them loads its own values.

## References

- `docs/design/system-design.md`, sections 4.7, 7.3, 12, 17
