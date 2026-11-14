+++
id = "E1-12"
title = "Spike S11: ty on aws-cdk-lib, and stages with no environment"
labels = ["size:S", "kind:test"]
epic = "E1"
blocked_by = ["E1-09", "E1-11"]
+++

## Context

Spike S11 of section 12 asks two things. Does ty, which is beta, give a clean gate on code written
against the current aws-cdk-lib? And do CDK Pipelines accept stages that name no account and no
region? Its pass condition is "a clean gate on a hello-world stack; the phase 0 deployment"; if the
first half fails, the design's answer is "adjust ty rules".

The first half could not wait for this issue: the empty app (issue E1-04) put CDK code in ty's
scope, and CI fails on a warning, so the gate has passed since then, with a ty ignore comment, naming
its rule and its reason, wherever ty misread aws-cdk-lib (AGENTS.md). What is left to measure is how
many such comments the CDK code needed, and for which rules: zero is a clean gate; a few are the
"adjust ty rules" the design foresaw. This issue records that number on the app and the pipeline
stack as they stand.

The second half is decided by the phase 0 deployment (issue E1-10); if it fails, the design's
fallback is to give stages an environment from pipeline variables.

The record follows the format of `docs/measurements/README.md`: it has one run, labelled ty, on
the commit of the record; its rows count the ty ignore comments, by rule and by what was checked
(the app, the pipeline stack), labelled with the ty and aws-cdk-lib versions; and the run, so the
record, stays pending, with the reason that the phase 0 deployment has not answered the second
half yet.

## Deliverables

- [ ] `tests/synth/test_environment_agnostic.py` fails when a stack of the pipeline or of the stage has an explicit account or region.
- [ ] `docs/measurements/s11.md` records the number of ty ignore comments that the app's entry point and its constructs carry for aws-cdk-lib, from one run labelled ty on the commit of the record, one row per rule and per part checked (the app, the pipeline stack), each labelled with the ty and aws-cdk-lib versions, the run pending with the reason that the deployment has not answered, and so the record.

## Acceptance criteria

- [ ] `scripts/check.sh types` exits 0 with `app.py` and `src/rhi/` in ty's scope.
- [ ] `uv run --locked pytest tests/synth/test_environment_agnostic.py tests/unit/test_measurements.py` passes.
- [ ] The phase 0 deployment succeeds with stages that name no environment, or the stages take one from pipeline variables, and `docs/measurements/s11.md` records which with its verdict (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 12, 14
- `docs/design/evidence.md`, E20
- `docs/measurements/README.md`, delivered by the measurement-records issue
- `AGENTS.md`, section "Validating a change"
