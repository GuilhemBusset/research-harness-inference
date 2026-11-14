+++
id = "E3-12"
title = "Verify a release on the coursework pool, and that it is the release served"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E0-21", "E1-16", "E2-64", "E3-40"]
+++

## Context

When a change touches the engine image, the machine image or a pool, the pipeline runs its smoke
test on the coursework pool too, and checks the served image digest and model revision (section 8.2,
step 6). A deployment does not reach an existing instance by itself: a release starts an instance
refresh, and without this check the stopped instance that serves every normal wake could keep the
old engine while the pipeline reports success (section 4.3). The served digest and revision are the
ones the instance's lifecycle agent reports, in its heartbeat and as a metric (section 4.1; issue
E2-35). The mock pool's identity test exists since issue E2-63; this issue runs it, and a full wake,
hold and completion, on the coursework pool.

**The decision.** Every change of a GPU pool's engine image, model revision, engine flags or machine
image starts an instance refresh of that pool's group, and an unrelated change starts none (issue
E2-42). So the verification step asks Auto Scaling whether this execution's deployment started a
refresh: a refresh of a GPU pool's group (found by its rhi-pool and rhi-engine tags, issue E3-01)
that began after the execution started means yes, and the step waits for that refresh to end before
it tests. A refresh that failed fails the step. Describing groups and their refreshes is granted by
AWS only on every resource, which is acknowledged with its reason. CDK Pipelines creates a step's
project and role in the pipeline stack, so the step's role and its acknowledgements belong to
`tests/policy/acknowledged/pipeline_stack.toml` (issue E1-10), as for issue E2-64.

**Where the step sits.** Issue E2-64 built the verification step and issue E0-21 made it keep the
IDs of the failed smoke tests for the reporter. This issue extends the same step, so the new test
gets an ID in `tests/smoke/ids.py`, which the reporter's allow-list reads, and the step's synthesis
test, `tests/synth/test_pipeline_verify.py`, covers the new part.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `scripts/pipeline/gpu_verification.py` decides whether an execution must verify on the coursework pool: yes when an instance refresh of a GPU pool's group began after the execution started; it waits for such a refresh to end and fails when the refresh failed.
- [ ] `src/rhi/pipeline/verify.py` runs that decision in the verification step and, when it says yes, the smoke tests against the coursework pool after the refresh has ended; the step's role may also describe groups and their refreshes, and `src/rhi/pipeline/stack.py` builds the step as before.
- [ ] `tests/smoke/deployed/` gains a test that wakes the pool it is given, completes a stream through it, and compares the image digest and the model revision its lifecycle agents report with the deployed release, and `tests/smoke/ids.py` lists its ID.
- [ ] `tests/policy/test_pipeline.py` holds the step's role to its actions, and `tests/synth/test_pipeline_verify.py` checks the decision's place before the coursework tests.
- [ ] Every cdk-nag finding `src/rhi/pipeline/verify.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_verify.toml` or `tests/policy/acknowledged/pipeline_stack.toml`; CDK Pipelines creates the step's project and role in the pipeline stack, so what the step's role needs, describing groups and their refreshes included, is in the latter.
- [ ] `tests/unit/test_gpu_verification.py` covers the decision against a stubbed Auto Scaling client.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_gpu_verification.py` passes: a refresh of the coursework group begun after the execution's start asks for the GPU verification, a refresh begun before it, a refresh of the mock pool's group, or none, does not; a refresh in progress is waited for, and a failed one fails the decision.
- [ ] `tests/smoke/test_smoke.py::test_ids_are_unique_and_listed` passes with the new test.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_verify.py tests/policy/test_pipeline.py` passes with the new part of the step.
- [ ] In the account, a release that changes the engine image is verified on the coursework pool, whose warm instances then serve the new digest (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 8.2
