+++
id = "E4-26"
title = "A dry-run class on the coursework pool: the expected sessions for an hour, within objectives and ceilings"
labels = ["size:M", "kind:test"]
epic = "E4"
blocked_by = ["E0-27", "E3-19", "E3-27", "E3-29", "E3-31", "E3-32", "E3-36", "E3-42", "E3-45", "E4-16"]
+++

## Context

Phase 3 ends with a dry-run class on the coursework model: the expected number of concurrent harness
sessions for an hour, within the objectives of section 7.3 and the ceilings (section 14). Each pool's
configuration carries its expected values for speed per stream, time to first token and cache
capacity (issue E2-68), which the inference performance dashboard shows beside the live series, and
the dry-run class is judged against them too (section 4.7).

The objectives of section 7.3, measured during session windows (issue E4-16):

| Objective | Target |
| --- | --- |
| Gateway answers | 99.5% of requests are not 5xx from the gateway's own fault |
| Time to first token, warm pool | 95th percentile under 5 s, with the first turns of sessions counted apart |
| Output speed per stream, warm pool | On the coursework pool, the median request at 15 tokens per second or more and the slowest tenth at 10 or more |
| Cached share of input | Above 85% |
| Cold start from the warm pool | 90th percentile under 5 minutes |
| Pool ready at session start | Every scheduled pool in service 30 minutes before |

The speed targets and the cached share are provisional, held in configuration (section 7.3; issue
E4-31). The objectives are defined once, in the module of issue E4-16, which the dry run reads. The
ceilings are the coursework pool's GPU-hour ceilings, 16 a day and 150 a month by default (section
17).

So the dry run is a rehearsal session: the owner adds a window to the schedule, the scaler
pre-warms the instances that window names (issue E4-05) and the class-day notice goes out (issue
E4-06), and the job replays the class's sessions inside that window with the load generator of
issue E3-36, which spike S5 also uses (issue E3-19). The cached share above 85% rests on student
affinity: the target groups' stickiness (issue E3-29), the gateway's cookies (issues E3-30 and E3-32)
and the scaler's rebalance number (issue E3-31), so the dry run comes after all of them. The number of sessions is the class size, decision 5; the shape (each
student working hands-on, or running several agents as in the second session) is the job's
argument, recorded with the result. The verdict is the exit criterion: the six objectives and the
ceilings. Against the expected values, the record says for each whether the measurement is below,
within or above it; a measurement below its expected range is a finding, not a failure, as long as
the objectives hold.

The dry run is a spike job like the others: one entry, the job dry_run_class, in the explicit list
of spike jobs of `src/rhi/pipeline/spikes.py` (issue E3-45), started by hand only, on the one role
and the one log group every spike job shares, and held by the policy test over every spike job,
which is a rule and needs no change here. A new entry brings no cdk-nag finding (issue E3-45), so
this issue names no acknowledgement file and does not change `src/rhi/pipeline/stack.py`. It posts its numbers with the shared code of issue E3-14, through the reporter's measurement
events (issue E0-27): the measurement's id is dry-run-class, one of the fixed set of issue E0-14,
every number's name is made of lower-case letters, digits and underscores, starting with a letter,
and the statuses go on the commit the job ran from.

Agents build and test the job and the record. Running it needs the account and a GPU, and is the
owner's; it reaches the pool through the endpoint, so it also needs the public HTTPS listener, which
the owner turns on (issue E2-47).

This issue changes `src/rhi/pipeline/`, a protected path (ADR 0001, decision 10 and amendment 1),
and names each file it changes in its Deliverables.

## Deliverables

- [ ] `spikes/dry_run_class.py` waits for the start of a session window of the configuration, replays the given number of sessions of the given shape against the coursework pool for one hour, then reads the six objectives over that hour with the definitions of issue E4-16, and the pool's GPU-hours accrued over the day and the month.
- [ ] It decides the verdict, pass only when every objective meets its target and no ceiling was reached, and compares the measured speed per stream, time to first token and cache capacity with the pool's expected values; `tests/unit/spikes/test_dry_run_class.py` covers the verdict and the comparison on recorded numbers.
- [ ] It declares the names of the numbers it posts, posts them and the verdict as the measurement dry-run-class with the code of issue E3-14, and `docs/measurements/dry-run-class.md` is the record, with a pending verdict until the run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job dry_run_class, which runs `spikes/dry_run_class.py` and is built like every spike job, started by hand only.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_dry_run_class.py tests/unit/test_measurements.py` passes: numbers within the six targets and under the ceilings pass; a 95th-percentile time to first token of 6 seconds fails, a cached share of 80% fails, a slowest tenth at 9 tokens per second fails, and a day's GPU-hours at the daily ceiling fails; a median speed below the expected range but at 15 tokens per second or more passes and is recorded as below.
- [ ] `uv run --locked pytest tests/synth/test_spike_jobs.py tests/policy/test_pipeline.py` passes with the dry run among the spike jobs, with no trigger.
- [ ] The dry-run class is run in a rehearsal window on the coursework pool at the class size the owner gave, and its record shows it within the objectives and the ceilings, which is the exit criterion of phase 3 (needs gpu:capacity).
- [ ] The owner gives the class size, decision 5 of the design, which is the number of sessions the dry run replays (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.7, 7.3, 14, 15, 17
