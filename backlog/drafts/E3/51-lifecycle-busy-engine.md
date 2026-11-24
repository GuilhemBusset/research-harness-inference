+++
id = "E3-51"
title = "Lifecycle agent: restart the engine only when it has counted no token for a minute, never a busy engine that answers its check late"
labels = ["size:M", "kind:app"]
epic = 5
+++

## Context

In spike S5's first full run on the coursework pool (run `8bit-baseline-1`), the lifecycle agent
restarted the engine's unit 19 times. The engine was busy, not hung: between restarts its own
counts showed thousands of input tokens computed a second and several requests running and
waiting, and nothing failed inside the engine before a restart; the restart is what stopped it.
About four requests in five of that run failed because of it.

The cause is the check of liveness by inference (section 4.3). Every minute `Serving.check`, in
`services/engine/lifecycle/rhi_lifecycle/serving.py`, sends the engine a completion of one token
(line 217) and, when no answer comes within `PROBE_SECONDS`, 30 seconds (line 72), restarts the
unit (line 220). Under S5's load, long agentic contexts bound by prefill with requests queued, the
completion waits its turn behind the queue and does not answer in time. Each restart kills every
request in flight, which the gateway answers 502 `engine_error`, or ends with that cause's error
event in a stream already answered 200, and it empties the prefix cache, so the next prefills are
longer and the next completion slower still: a cascade.

Neither the health route nor the running and waiting counts can tell a busy engine from a hung
one. The check exists because an engine can hang with its health route still answering (evidence
E23), and a hung engine goes on reporting the requests it holds as running or waiting, the one
that stalled it among them. What a hung or dead engine cannot do is compute or generate a token.
So the criterion is progress: the engine's input and output token counters, the contract's
`input_tokens` and `output_tokens` quantities (section 4.1), read under the names of the engine's
row in `rhi_common.engine_metrics`, from the answer of the engine's metrics route that the
heartbeat already fetches every 5 seconds (`Serving.beat`). The heartbeat notes when that sum last
grew. A failed completion then restarts the engine only when the sum has not grown in the
`PROBE_INTERVAL_SECONDS` (60 seconds) before the completion failed; a metrics route that does not
answer shows no growth. An engine that answers its completion is never restarted, as now; the wait
for the health route after a restart, and the skipped minute when the pool's secret cannot be
read, are unchanged.

The window is the probe interval, not `PROBE_SECONDS`: an engine may count a request's input
tokens only once its prefill ends, so one long prefill can hold a counter still for tens of
seconds while the engine works. A hung engine is then restarted at most about a minute later than
today, which costs less than killing every request of a busy one.

The stand-in engines live in `tests/unit/lifecycle/test_serving.py`, beside its world and its
clock; the mock engine (`services/engine/mock/`) is not changed. The comment above `PROBE_SECONDS`,
which says it is long enough for one token from a busy engine, is corrected with the rest of the
file.

Suggested branch: `fix/lifecycle-busy-engine`

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/serving.py` restarts the engine's unit after a failed completion only when no heartbeat in the PROBE_INTERVAL_SECONDS before the failure read a sum of the engine's input and output tokens greater than the heartbeat before it.
- [ ] The description of that module and `docs/design/system-design.md`, section 4.3, state that criterion and its reason: a busy engine can take longer than the completion's limit to answer it.
- [ ] `tests/unit/lifecycle/test_serving.py` holds a stand-in engine that is slow but alive, whose token counts grow while it answers no completion in time, and one that is dead, beside the hung mock it already uses.

## Acceptance criteria

- [ ] `tests/unit/lifecycle/test_serving.py` holds that an engine that answers no completion within PROBE_SECONDS while its token counts grow at every heartbeat is not restarted over five minutes of checks, and its restarts metric stays at 0.
- [ ] `tests/unit/lifecycle/test_serving.py` holds that an engine whose completion hangs while its health route answers 200 and its token counts never grow, and one where nothing listens on the engine's port, are each restarted at their first check.
- [ ] `tests/unit/lifecycle/test_serving.py` holds that an engine whose token counts grew and then stopped is not restarted by a failed completion within 60 seconds of their last growth, and is restarted by the first failed completion after that.
- [ ] `tests/unit/lifecycle/test_serving.py` holds that an engine that answers its completion is never restarted, whatever its token counts do.
- [ ] `uv run --locked pytest tests/unit/lifecycle` passes.
- [ ] The next run of spike S5 on the coursework pool shows no engine restart: the increase of rhi_lifecycle_engine_restarts_total over the run's window, read from the stage's metrics workspace, is 0 on every instance (needs aws:deployed-environment).

## References

- `services/engine/lifecycle/rhi_lifecycle/serving.py`, the methods check and beat of Serving
- `services/common/rhi_common/engine_metrics.py`
- `docs/design/system-design.md`, sections 4.1 and 4.3
- `docs/design/evidence.md`, E23
- `docs/measurements/s5.md`
