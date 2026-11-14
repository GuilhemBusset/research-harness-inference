+++
id = "E4-07"
title = "Hold deployments from one hour before a session to one hour after"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E1-10", "E4-33"]
+++

## Context

There is one environment, and deployments wait outside session windows (section 8.5): the pipeline's
deploy step is held from one hour before to one hour after a scheduled session (section 8.2, step
5). A deployment that touches a pool refreshes its instances (section 4.3), which in a session would
cost the class its warm instances and every student's cache. The session windows are those of the
configuration (issue E4-33). The longest held span is the first session, four hours, plus an hour
on each side.

The step reads the schedule from SSM at run time, through the configuration loader's ssm source
(issue E1-17) and the schedule module of issue E4-33, so that a window the owner adds after a merge
holds the next deployment. While the configuration in SSM holds no window, the step passes at once.

**How long it may wait.** Two sessions closer than two hours make one held span: the step waits
through both. So the step's timeout is not the longest single widened window but the longest wait
the schedule can cause: the longest run of widened windows that overlap or touch one another, from
one hour before the first to one hour after the last. `src/rhi/pipeline/hold.py` computes it from the
configuration the app synthesizes, with the windows of the schedule module, and sets the timeout 15
minutes above it, at most CodeBuild's limit of 36 hours; with no window the timeout is CodeBuild's
default of 60 minutes. The 15 minutes are this issue's margin for the step's own start, not a value
of the design.

**What its role holds.** The step's own permission is to read the configuration's parameters in SSM.
CDK Pipelines also gives every step's role what any step needs, the same for each: writing the
step's build logs and reports, and reading and writing the pipeline's artifact bucket with its key.
The role holds nothing beyond those two. CDK Pipelines creates a step's project and role in the
pipeline stack, so its findings are acknowledged in `tests/policy/acknowledged/pipeline_stack.toml`
(issue E1-10), as for issue E2-64.

Whether production deployments during course weeks should also wait for a person is the second half
of the owner's decision 8. This issue builds the design's default, a hold around sessions only.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1), and names
each file it changes in its Deliverables.

## Deliverables

- [ ] `scripts/pipeline/deploy_window.py` reads the session schedule from the configuration and exits 0 when the time is outside every session window widened by one hour on each side; inside one, it waits until that widened window has passed, then exits 0; `tests/unit/test_deploy_window.py` covers the decision on a clock the test controls.
- [ ] `src/rhi/pipeline/hold.py` defines a step that runs it with the configuration read from SSM, whose timeout is the longest wait the configuration's schedule can cause, as the Context computes it, plus 15 minutes and at most 36 hours, and whose role may read the configuration's parameters in SSM and holds nothing else beyond what CDK Pipelines gives every step's role.
- [ ] `src/rhi/pipeline/stack.py` runs that step before the stage's deployment, `tests/policy/test_pipeline.py` records the step in its reviewed list, and `tests/synth/test_pipeline.py` checks the step's place and its timeout.
- [ ] Every cdk-nag finding `src/rhi/pipeline/hold.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_hold.toml` or `tests/policy/acknowledged/pipeline_stack.toml`; CDK Pipelines creates the step's project and role in the pipeline stack, so what the step's role needs is in the latter.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_deploy_window.py` passes: 61 minutes before a session the step passes at once, 59 minutes before it waits until one hour after the session's end, between two sessions closer than two hours it waits through both, and with no window it passes at once.
- [ ] `uv run --locked pytest tests/synth/test_pipeline.py tests/policy/test_pipeline.py` passes with the hold before the deployment and its role limited to the configuration's parameters and what CDK Pipelines gives every step's role.
- [ ] `tests/synth/test_pipeline.py::test_the_hold_outlasts_close_sessions` synthesizes a configuration with two sessions 90 minutes apart and finds the hold's timeout longer than the span from one hour before the first to one hour after the second.
- [ ] The owner says whether deployments during course weeks also wait for a person, the second half of decision 8 of the design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.3, 8.2, 8.5, 15
