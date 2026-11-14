+++
id = "E2-82"
title = "The mock engine's metrics, its limit on running requests, its shutdown and its image"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-31"]
+++

## Context

The contract's metrics row names at least: running and waiting requests; time to first token; time
per output token; input tokens, the cached part of them, and output tokens; cache use (section 4.1).
Engines name their metrics differently, so dashboards and the lifecycle agent read them through a
small per-engine table, which names the same quantities for every engine. The mock's numbers follow
the work it really does, so that the scaler's rules (section 5.1), spike S7 and the dashboards of
section 7 can be exercised on the mock pool: a mock that runs a limited number of requests at once
and queues the rest shows waiting requests the way an engine does. The cached part and the cache use
read zero here; issue E2-75 makes them follow an imitation of a prefix cache.

The metrics route is on localhost only (section 4.1): the mock answers it to a client on the
loopback address and 404 to any other. The contract's shutdown row: in-flight requests finish on
SIGTERM.

The mock pool runs on arm64 (section 4.2), and the pipeline builds its image for that architecture
(issue E2-42). CI's runners are x86_64. So the Dockerfile pins a base image that is published for
both architectures, by the digest of its multi-architecture index, and builds for whichever platform
it is asked for; CI's container job builds and starts it on the runner's own (issue E2-86). Its
build context is the one `src/rhi/packaging.py` gives a Python service (issue E1-16): the shared
package, the mock's package, its Dockerfile, and the mock-engine group's requirements with their
hashes.

## Deliverables

- [ ] `services/common/rhi_common/engine_metrics.py` is the per-engine table of metric names for the quantities of the contract's metrics row, with the mock engine's row, and `tests/unit/common/test_engine_metrics.py` covers it.
- [ ] `services/engine/mock/rhi_mock_engine/app.py` serves `GET /metrics` in Prometheus format to a client on the loopback address only, with every quantity of the contract's metrics row under the mock's names, each following the requests it serves, the cached part and the cache use at zero.
- [ ] A setting caps the requests the mock runs at once; requests beyond it wait, and are counted as waiting until they run.
- [ ] It finishes requests in flight when it receives SIGTERM.
- [ ] `services/engine/mock/Dockerfile` builds the mock's image from a base image pinned by the digest of its multi-architecture index, for linux/arm64 or for the builder's own platform, installing the build context's requirements with their hashes checked, and `tests/unit/engine/test_mock_metrics.py` covers the metrics, the limit and the shutdown.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_mock_metrics.py tests/unit/common/test_engine_metrics.py` passes: the metrics route serves every name of the mock's row to a loopback client and answers 404 to any other.
- [ ] `tests/unit/engine/test_mock_metrics.py::test_running_and_waiting` sets at most 2 running requests, sends 3 at once, and reads 2 running and 1 waiting from the metrics until the first ends; the input and output token counters then equal the usage the answers reported.
- [ ] `tests/unit/engine/test_mock_metrics.py::test_sigterm_finishes_requests_in_flight` shows a stream in flight completing after the signal.
- [ ] `tests/unit/engine/test_mock_metrics.py::test_the_base_image_is_pinned` fails when the Dockerfile names a base image without a digest.

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 5.1, 7.2
