+++
id = "E5-05"
title = "Spike S6: does the upgrade model load and serve on its instance type, and what does it serve"
labels = ["size:M", "kind:test"]
epic = "E5"
blocked_by = ["E3-36", "E3-42", "E3-45", "E5-01", "E5-02", "E5-03"]
+++

## Context

Nobody has run Qwen3.8-Flash-Next on `g7e.24xlarge`. Its vLLM recipe was run on four of this GPU,
and section 4.7's figures for it are judgment from other models, the weakest of that section
(section 12; evidence E28, E29). Spike S6 asks: does the model load and serve under vLLM on that
instance type, on all three routes with tool calls; how long is the load from S3; and what does the
instance serve, by the measures of section 4.7? Pass: it loads within the launch hook's timeout,
the routes answer, and a class's sessions stay within the floors of spike S5. If it fails, the
upgrade pool waits and scheduled sessions run on more coursework instances (section 12). Its
requests go through the gateway, so the request profile is in place: section 12 asks S6 for the
same check on the upgrade model that S3 makes on the coursework model.

Section 4.7 estimates for this pool 60 to 100 tokens per second for one stream alone, 40 to 60 at
8 streams and 25 to 40 at 32, 800 to 1,300 tokens per second in total at 32 streams, a cached
turn's first token under 1 second, and a class of twenty on one instance. The spike measures at 1,
8 and 32 streams, the counts of that estimate, and at the class's size, which is the owner's
decision 5. Its load uses the load generator of issue E3-36, and its runs and verdict are those of
spike S5 (issues E3-19 and E3-42), with the floors of section 17 that issue E4-31 holds in
configuration (target_ttft_p95_seconds, target_stream_median_tokens_per_second and
target_stream_p10_tokens_per_second).

**Who starts the pool.** An admin's window (issue E5-01) is the only way to start a scheduled-only
pool outside the schedule. Opening one writes the pool's state record and asks the scaler to run,
which a spike job's role may not do: it reads the pool-state records and nothing of the scaler
(issue E3-45). So the owner opens the window with the admin CLI before starting the job, long enough
for the run, and closes it afterwards; the spike reads the window from the pool's state record,
refuses to start without an open one, and measures the start that the window causes.

It costs about $17 an hour while it runs (section 12) and is the owner's call; its budget is part
of decision 6. Agents build and test the job and the record. Running it is an obligation here,
never a pass.

## Deliverables

- [ ] `spikes/s6_upgrade_pool.py` refuses to start unless the pool it is given, the upgrade pool by default, has an admin's window open in its state record, and reads from the lifecycle agent's metrics the seconds of each stage of the start and of the whole, and whether the launch hook completed or abandoned the instance.
- [ ] Once the pool is ready, it sends through the gateway one streamed request with a tool call on each of the three wire formats, and records which of them answered with a valid tool call.
- [ ] It then runs the load generator of issue E3-36 at 1, 8 and 32 streams and at the class's size, given to the job, and records for each the output speed per stream at the median and the slowest tenth, the output throughput, the time to first token at the median and the 95th percentile with the first turns of sessions counted apart, and the preemptions.
- [ ] It records the cache capacity in tokens that the engine reports at start, and the cost per million output tokens at the pool's hourly price.
- [ ] It judges the run with the verdict of spike S5 and its floors from configuration, declares the names of the numbers it posts, and posts them and the verdict with the code of issue E3-14; `docs/measurements/s6.md` is the record, with a pending verdict until the spike has run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s6_upgrade_pool, which runs `spikes/s6_upgrade_pool.py`.
- [ ] `tests/unit/spikes/test_s6.py` covers the verdict, the derived numbers on recorded measurements and the refusal without a window.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s6.py tests/unit/test_measurements.py` passes: a completed hook, three routes answering and a class's sessions within the floors pass; an abandoned hook, a route that fails, and a class's sessions below a floor or with a preemption each fail with the cause named; and the pending record is valid.
- [ ] `tests/unit/spikes/test_s6.py::test_no_window_no_run` finds the spike refusing to start, with nothing sent, for a pool whose state record holds no open window.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The owner gives a budget for the paid spike, part of decision 6 of the design (needs human:owner-decision).
- [ ] The owner opens an admin's window on the upgrade pool, runs the spike, closes the window, and `docs/measurements/s6.md` records its load time, the routes that serve and what the instance serves (needs gpu:capacity).
- [ ] The upgrade pool's configuration takes the start measured by the spike and a pre-warm lead that covers it (needs aws:account).

## References

- `docs/design/system-design.md`, sections 4.7, 5.2, 5.4, 12, 15
- `docs/design/evidence.md`, E28, E29
