+++
id = "E2-36"
title = "Lifecycle agent: complete the termination hook once the engine is idle"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-34", "E2-82"]
+++

## Context

When an instance leaves service, for the warm pool or for good, a request may still be running on
it. The agent waits for the engine to drain, then completes the hook (section 4.3, the third
transition). The hook's own timeout, `drain_seconds` plus a margin of 60 seconds (section 17), is
the bound if the agent is gone.

An instance that returns to the warm pool meets the same wait state as a new instance entering it
(section 4.3; evidence E10), so the agent chooses by transition: an instance that has served since
its start is leaving service, and drains; one that has not is entering, and is issue E2-34's.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/termination.py` waits until the engine reports nothing running and nothing waiting, then completes the termination hook so that the instance may leave, whether it is terminated or returns to the warm pool.
- [ ] `services/engine/lifecycle/rhi_lifecycle/launch.py` hands an instance that has served since its start, and arrives in the wait state a new instance enters the warm pool by, to this handling, never to an entry.
- [ ] It stops the engine's unit through the host module of issue E2-33 only after that, so that requests in flight finish.
- [ ] `tests/unit/lifecycle/test_termination.py` covers the wait against the mock engine.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_termination.py` passes: with a stream in flight the hook is not completed, and it is completed within one poll of the stream's end.
- [ ] `tests/unit/lifecycle/test_termination.py::test_a_return_to_the_warm_pool_drains` shows an instance that has served, returning to the warm pool with a stream in flight, completing its hook only after the stream ends, and never pulling the image or checking the release as an entry would.

## References

- `docs/design/system-design.md`, sections 4.3, 5.1, 17
- `docs/design/evidence.md`, E10
