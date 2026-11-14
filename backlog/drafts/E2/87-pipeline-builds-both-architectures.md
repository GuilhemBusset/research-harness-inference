+++
id = "E2-87"
title = "The pipeline's asset step builds container images for arm64 as well as amd64"
labels = ["size:S", "kind:infra"]
epic = "E2"
blocked_by = ["E1-10", "E1-19"]
+++

## Context

The engine image is built by the pipeline into ECR (section 4.3, section 8.2, step 4), for the
processor architecture of its pool's instance types: the mock pool's `t4g.small` is arm64 and the GPU
pools' types are x86_64 (section 4.2). The pipeline's asset step builds every image asset of the
stage, so it must build both in the same run. CodeBuild's standard build hosts are x86_64, so the
step needs an emulator for arm64 and a builder that can target either platform; whatever it installs
for that is pinned by version or digest, like everything else the pipeline runs.

This issue changes the pipeline before any arm64 image exists, so that issue E2-42, which adds the
mock engine's arm64 image, deploys on a pipeline that can build it. Nothing in the stage changes here.

`src/rhi/pipeline/` is a protected path (ADR 0001, decision 10 and amendment 1); the builder may
change the files named below because this issue names them. `tests/policy/test_pipeline.py` holds the
pipeline's stages and its non-asset actions in a reviewed list and the asset actions by rules (issue
E1-19); this change to the asset step's build settings is expected to leave both as they are, and the
issue names the test so that the builder may update it if a rule it holds is affected.

## Deliverables

- [ ] `src/rhi/pipeline/stack.py` gives the pipeline's asset-publishing step what it needs to build an image asset for linux/arm64 as well as one for linux/amd64 in the same run, with any tool it installs for that pinned by version or digest.
- [ ] `tests/policy/test_pipeline.py` passes with this change, and is changed only where a rule it holds about the asset step is affected.
- [ ] Every cdk-nag finding `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_stack.toml`.
- [ ] `tests/synth/test_pipeline_assets.py` checks the asset step's build settings.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline_assets.py tests/policy/test_pipeline.py` passes: the asset step installs an arm64 emulator and a multi-platform builder, each pinned by version or digest, and builds with them.
- [ ] `tests/synth/test_pipeline_assets.py::test_tools_are_pinned` fails when a tool the asset step installs is named without a version or a digest.
- [ ] The next pipeline run that builds an arm64 image asset publishes it for arm64 (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.2, 4.3, 8.2
- `docs/adr/0001-software-factory.md`, decision 10
