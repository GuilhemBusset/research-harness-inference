+++
id = "E3-13"
title = "A weekly canary on the coursework pool, whatever was deployed"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E0-27", "E3-12"]
+++

## Context

A weekly canary runs the GPU test regardless of what changed (section 8.2, step 6). It costs about
$1.50 a run, since each wake also pays the idle tail (section 11.2). Phase 2 ends when it has been
green twice (section 14).

**What it starts.** A schedule starts one CodeBuild job of the pipeline stack, which runs the
coursework pool's smoke tests (the ones issue E3-12 runs after a refresh) against the deployed
endpoint, with the canary key of issue E2-56. It starts no pipeline execution and deploys nothing:
it tests what is deployed. Its role is the verification step's (read the canary secret, query the
workspace, read the pools' release parameters) and the grant of issue E0-27 to send a measurement,
and nothing more.

**When.** Once a week, on Sundays at 04:00 UTC. Section 8.5 holds deployments outside session
windows; the canary is not a deployment, so that hold does not apply to it, and a wake during a
session finds the pool already serving. The configuration holds no time for it, and the design gives
none: this issue fixes the time in the job's construct.

**Its result** is a measurement of issue E0-14, with the id gpu-canary, posted through the event of
issue E0-27 on the commit the job ran from: the verdict, and as numbers the seconds the wake took
and the smoke tests passed and failed. Measurement names are lower-case letters, digits and
underscores (issue E0-27). Its run label (issues E0-14 and E0-27) is the UTC date of the run,
YYYY-MM-DD: the phase's exit asks for the canary green twice, and a record keeps one run per label
(issue E1-11), so each week's run keeps a label of its own. The design gives no label; the date is
this issue's choice.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pipeline/canary.py` adds a job that runs the coursework pool's smoke tests against the deployed endpoint, and a schedule that starts it on Sundays at 04:00 UTC and starts nothing else; the job starts no pipeline execution.
- [ ] `scripts/pipeline/canary_result.py` turns the job's smoke-test results into the measurement gpu-canary (its verdict, the seconds the wake took, the numbers of smoke tests passed and failed) and sends it through the event of issue E0-27, on the commit the job ran from, with the UTC date of the run as its run label.
- [ ] The job's role may read the canary secret, query the workspace, read the pools' release parameters and send that event, and nothing else, and `src/rhi/pipeline/stack.py` builds the job and its schedule.
- [ ] `tests/policy/test_pipeline.py` fails when the canary's schedule starts anything but the canary job, or when the job's role may start a pipeline execution.
- [ ] Every cdk-nag finding `src/rhi/pipeline/canary.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_canary.toml`.
- [ ] `tests/synth/test_canary.py` checks the job, its schedule and its role, and `tests/unit/test_canary_result.py` the measurement it builds from recorded test results.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_canary.py tests/policy/test_pipeline.py` passes: one schedule, weekly on Sundays at 04:00 UTC, whose only target is the canary job, and a role that sends measurement events and starts no pipeline execution.
- [ ] `uv run --locked pytest tests/unit/test_canary_result.py` passes: recorded results with every smoke test passed give the verdict pass with the wake's seconds, one failed test gives fail, every name is lower-case letters, digits and underscores, and the run label is the date the test's clock gives, as YYYY-MM-DD.
- [ ] In the account, the weekly canary has been green twice, which is an exit criterion of phase 2 (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 8.2, 8.5, 11.2, 14
