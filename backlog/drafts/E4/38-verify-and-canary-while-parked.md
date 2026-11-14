+++
id = "E4-38"
title = "While parked, the pipeline's verification passes as parked and the weekly canary does not run"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E0-21", "E3-13", "E4-17"]
+++

## Context

A parked stage has no endpoint (issue E4-17). The pipeline's verification runs smoke tests against
the deployed endpoint (issue E2-64), and the weekly canary wakes the coursework pool (issue E3-13).
Parked, both must pass without an endpoint, or every merge between cohorts would fail its
deployment.

A verification that ran nothing must not read as one that ran its tests: the engine discharges an
owner item about the deployed system only on a verification status whose description is the one
`services/reporter/rhi_reporter/status.py` holds for a verification whose tests ran and passed
(issues E0-08 and E0-09). So a parked verification passes with a description of its own.

The pipeline is synthesized from the same configuration as the stage, so the flag is known when the
pipeline's steps are built: the verification step learns it from its definition, not from a read of
SSM at run time.

**Where the mark goes.** A verification that ran no test says why in one place: in its post-build
phase it writes, in the pipeline's artifact bucket, the object
verification/<pipeline execution ID>/not-run, whose one line is the reason. Issue E2-64 writes the
reasons canary-missing and listener-off there, and issue E0-21 has the reporter read the object,
under the prefix verification/ that the step's role already writes and the reporter's role already
reads. This issue adds the reason parked, and the description the reporter posts for it; nothing
is written to the step's output artifact.

CDK Pipelines creates a step's project and role in the pipeline stack, so a finding on the step's
role is acknowledged in `tests/policy/acknowledged/pipeline_stack.toml` (issue E1-10), as for issue
E2-64.

This issue changes `src/rhi/pipeline/` and `tests/policy/acknowledged/`, protected paths (ADR 0001,
decision 10 and amendment 1), and names each file it changes in its Deliverables. Under the example
configuration, which is not parked, the pipeline's steps and jobs are as before, so
`tests/policy/test_pipeline.py` is not changed.

## Deliverables

- [ ] `src/rhi/pipeline/verify.py` builds, for a configuration that is parked, a verification step, still the post-deployment action named Verify that issue E0-09 maps to the context verify/<stage>, that runs no test against the endpoint, writes in its post-build phase the one line parked to the object verification/<execution ID>/not-run of the pipeline's artifact bucket, and passes; for any other configuration the step is as before.
- [ ] `src/rhi/pipeline/canary.py` creates no weekly schedule for a configuration that is parked, and `src/rhi/pipeline/stack.py` gives both the configuration's flag; `tests/synth/test_pipeline_verify.py` and `tests/synth/test_canary.py` cover the two.
- [ ] `services/reporter/rhi_reporter/status.py` gains a description for a verification that passed because the system is parked, distinct from the constant for a verification whose tests ran and passed and from the descriptions for canary-missing and listener-off, and `services/reporter/rhi_reporter/handler.py` posts it when the execution's not-run object holds the reason parked; `tests/unit/reporter/test_handler.py` and `tests/unit/reporter/test_status.py` cover it.
- [ ] Every cdk-nag finding `src/rhi/pipeline/verify.py`, `src/rhi/pipeline/canary.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_verify.toml`, `tests/policy/acknowledged/pipeline_canary.toml` or `tests/policy/acknowledged/pipeline_stack.toml`; CDK Pipelines creates the verification step's project and role in the pipeline stack, so what that role needs is in the last.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline_verify.py tests/synth/test_canary.py tests/policy/test_pipeline.py` passes: under a parked configuration the verification runs no smoke test and writes parked to the not-run object under the prefix verification/ in its post-build phase, and the canary has no schedule; under the example configuration both are as before.
- [ ] `uv run --locked pytest tests/unit/reporter/test_handler.py tests/unit/reporter/test_status.py` passes: a passed verification whose not-run object holds parked gets the parked description, one whose object holds canary-missing does not, and one with no such object gets the constant of a verification whose tests ran and passed.
- [ ] In the account, the system is parked and unparked once, a student's key works again afterwards, and the month's idle cost while parked is recorded (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.2, 8.2, 8.5, 11.2, 14
