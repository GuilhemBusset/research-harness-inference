+++
id = "E2-75"
title = "The mock engine imitates a prefix cache, so that the cached share and affinity show on the mock pool"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-82"]
+++

## Context

An agent re-sends its whole transcript on every turn, and the engine keeps what it has computed for
that transcript in its prefix cache: a turn that reaches the instance that served the previous one
pays only for its new tokens (section 5.5). The cache salt is per student, so a prefix is reused
only by the student who sent it (sections 5.5 and 6.6). When the cache is full the engine forgets
the prefixes of idle sessions first (section 5.5).

The mock pool is where the control loop is proven for cents (section 4.1), and phase 2 checks there
that a student stays on one instance and moves when it leaves (spike S8, section 5.5). With a mock
that reports no cached input, neither that nor the cached share of section 7.2 can be seen on the
mock pool. This issue makes the mock's cached part, cache use and usage follow a small imitation of
a prefix cache: it computes nothing, it only counts.

The gateway sets the per-student cache salt in the request field vLLM's API reference names for it
(issue E2-16); the mock reads the same field.

## Deliverables

- [ ] `services/engine/mock/rhi_mock_engine/cache.py` keeps, for each cache salt, the input of the last request with that salt that this engine served, and counts as cached the tokens a new request with the same salt shares with it from its start.
- [ ] It holds at most a capacity in tokens from the mock's settings, and when the inputs it keeps exceed it, forgets first the salt whose last request is oldest.
- [ ] `services/engine/mock/rhi_mock_engine/app.py` reports the cached tokens in each answer's usage, in the field the format's reference gives for cached input, and in the metrics of issue E2-82, whose cache use becomes the tokens kept over the capacity.
- [ ] `tests/unit/engine/test_mock_cache.py` covers the imitation and what the mock reports.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_mock_cache.py` passes: a second request of one salt that repeats the first one's 1,000 input tokens and adds 100 reports 1,000 cached, and the same request under another salt reports none.
- [ ] `tests/unit/engine/test_mock_cache.py::test_the_oldest_session_is_forgotten_first` fills the capacity with three salts and finds the oldest one's next request reporting none cached and the other two's reporting their prefixes.
- [ ] `tests/unit/engine/test_mock_cache.py::test_metrics_follow_the_cache` finds the metrics' cached input equal to the sum of the cached tokens the answers reported, and the cache use equal to the tokens kept over the capacity.

## References

- `docs/design/system-design.md`, sections 4.1, 5.5, 6.6, 7.2
