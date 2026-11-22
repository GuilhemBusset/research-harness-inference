+++
id = "E1-22"
title = "The pipeline deploys with no manual approval: remove the approval on widened permissions"
labels = ["size:M", "kind:infra"]
epic = 3
+++

## Context

The stage is added to the pipeline behind CDK Pipelines' approval on widened permissions:
`src/rhi/pipeline/stack.py` builds `pipelines.ConfirmPermissionsBroadening("Check", stage=stage)`
and passes it first in the stage's pre-steps (`add_stage(stage, pre=[check, copy_weights], ...)`),
after which the copy of the weights waits on it (`copy_weights.add_step_dependency(check)`). That
step makes the CodeBuild action `Check.Check`, which runs `cdk diff --security-only` on the stage,
the manual approval action `Check.Confirm`, and a function that approves the check when it finds
no widened permission. A change that widens a permission therefore stops the deployment until a
person approves it in the console.

Deployments run with no person in the path (owner's decision of 2026-11-22). The controls on a
change that widens a permission are the ones before the merge: the examiner, which reviews every
change to IAM and to the network and was measured on seeded defects in such changes (issue #121),
and the invariants of `tests/policy/` on the synthesized templates; behind them the bounded
bootstrap's permissions boundary. The approval adds a wait without adding a control those lack,
and it never covered the pipeline's own stack, which updates earlier in the same execution. So it
goes, with its check, its function and everything that describes or tests it.

What stays: the reporter's vocabulary for an approval (`APPROVAL_DESCRIPTION` and the refusal's
description in the reporter), which still maps an approval event should one ever occur, and
`approval_description` of the engine's `loop.toml`. The deployment hold (issue #587) is not this
issue's: if it has landed first, its step stays among the stage's pre-steps, before the copy of
the weights.

This issue changes `src/rhi/pipeline/` and `tests/policy/`, protected paths (ADR 0001, decision 10
and amendment 1), and names each file it changes in its Deliverables.

## Deliverables

- [ ] `src/rhi/pipeline/stack.py` adds the stage with no `ConfirmPermissionsBroadening` and no other approval step: the copy of the weights is the first of the stage's pre-steps and depends on no check, and the stack's docstring describes the stage's deployment with no approval.
- [ ] `tests/policy/test_pipeline.py` has no Check.Check or Check.Confirm in its reviewed list, and its rule that a CloudFormation action comes after the approval on widened permissions becomes a rule that refuses any manual approval action in any stage of a pipeline; its module docstring, its built pipelines and its refused cases follow, a pipeline built with `ConfirmPermissionsBroadening` or a `ManualApprovalStep` among them.
- [ ] `tests/policy/acknowledged/pipeline_stack.toml` no longer acknowledges a finding, or lists a wildcard action, whose reason is the security check's project or the function that approves it, and keeps each that still has a resource of the stack behind it.
- [ ] `tests/synth/test_pipeline.py` asserts that the pipeline holds no action of the category Approval and that the stage's deployment actions follow the copy of the weights, in place of the test that they follow the approval; `tests/synth/test_weights_job.py` no longer requires the copy to follow an approval.
- [ ] `docs/design/system-design.md` section 8.4, control 6, says that a change that widens a permission deploys with no approval in the pipeline, and why: the examiner and `tests/policy/` hold such a change before the merge, the bounded bootstrap's boundary holds it after, and an approval in the pipeline would not cover the pipeline's own stack. Section 8.6 no longer lists approving the pipeline's pause among the owner's recurring tasks.
- [ ] No file under `src/rhi/pipeline/` or `tests/policy/` builds, acknowledges or expects an approval on widened permissions any more, and no manual approval action is built anywhere in the pipeline.
- [ ] `docs/runbooks/park-and-unpark.md` and `docs/runbooks/bad-deployment.md` no longer tell the owner to review, approve or reject an approval, nor name Check, Check.Check or Check.Confirm as an action of the pipeline.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_pipeline.py` passes: the app's pipeline is held by the rules, and a pipeline built with `ConfirmPermissionsBroadening` or with a `ManualApprovalStep` is refused as one with a manual approval.
- [ ] `uv run --locked pytest tests/synth/test_pipeline.py tests/synth/test_weights_job.py tests/synth/test_nag.py tests/policy/test_nag_acknowledgements.py tests/policy/test_iam_wildcards.py tests/unit/test_runbooks.py tests/unit/test_docs.py` passes.
- [ ] `scripts/check.sh synth` passes.
- [ ] `grep -rnE "ConfirmPermissionsBroadening|Check\.Confirm|security-only" src/rhi docs/runbooks docs/design/system-design.md` prints nothing.
- [ ] After this merge has deployed, a later merge that widens a permission reaches `deploy/Rhi` success with no pending status carrying the reporter's approval description (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2, 8.4 and 8.6
- `docs/adr/0001-software-factory.md`, decision 10 and amendment 1
- `src/rhi/pipeline/stack.py`
- `tests/policy/test_pipeline.py`
