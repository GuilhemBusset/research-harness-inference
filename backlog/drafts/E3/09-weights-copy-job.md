+++
id = "E3-09"
title = "The job that copies a pool's weights into the model bucket, and the coursework model's manifest"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E1-10", "E1-16", "E3-07", "E3-08"]
+++

## Context

The copy job is the only writer of the model bucket (section 4.4). It reads each GPU pool's model and
pinned revision, applies the allow-list and the manifest of issue E3-08, and writes under the pool's
prefix at that revision, `<pool>/<revision>/` (issue E3-07). Moving a pool to another build, as
spikes S3 and S5 do with the 4-bit build (issue E3-34), is then a change of configuration and a
committed manifest.

**When it runs.** It is a step of the pipeline, before the stage's deployment, so that a release
whose revision is new finds its weights when the instance refresh starts the engine. A new revision
is written beside the one in service, so an instance that wakes before the refresh still loads the
revision its release names. Older revisions stay in the bucket; removing them is not drafted, as a
revision of the coursework model costs under $1 a month (section 4.4). A configuration with no GPU
pool copies nothing and passes, so the step also passes before any GPU pool is configured.

**What it reads.** The configuration, from SSM, with the loader of `src/rhi/config.py`, so its role
may read the configuration's path; the manifests, from the commit the execution deploys; the files,
from Hugging Face anonymously at the pinned revision; and the bucket's name from the function of
`src/rhi/pools/model_store.py` (issue E3-07), which the pipeline stack gives the step in its
environment. It never reads an object of the bucket, whose reads only the VPC's endpoint may make:
it learns what a revision's prefix holds by listing it, and a file already there with the manifest's
size was verified when it was written, since only this job writes.

**The coursework model** is Qwen/Qwen3.8-27B-FP8, the publisher's 8-bit build: 30.9 GB, Apache-2.0,
with no model code (section 4.2; evidence E28). The design pins a commit of the model repository and
does not give one; this issue pins the commit the repository's main branch points at when the
manifest is built, and the example configuration's coursework pool names the same (issue E3-10).

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `scripts/pipeline/copy_weights.py` copies, for each GPU pool, the files of the manifest of the pool's model from Hugging Face at the pool's revision to the prefix of that pool and revision in the model bucket, verifying each file against the manifest before writing it and skipping a file the prefix already holds at the manifest's size; it removes from that prefix what the manifest does not list, and leaves the prefixes of other revisions alone.
- [ ] A pool whose model has no committed manifest, or whose revision is not its manifest's, fails the job before anything is copied for any pool, and a configuration with no GPU pool copies nothing and passes.
- [ ] `models/manifests/qwen--qwen3.8-27b-fp8.json` is the manifest of Qwen/Qwen3.8-27B-FP8 at a pinned revision, a full commit hash, written by the program of issue E3-08.
- [ ] `src/rhi/pipeline/weights.py` runs the job as a step before the stage's deployment, with the bucket's name from the model store's function of issue E3-07, and a role that may list, write and delete objects in the model bucket and read the configuration's path, and nothing else; `src/rhi/pipeline/stack.py` builds the step.
- [ ] `tests/policy/test_pipeline.py` records the step in its reviewed list, and fails when a role other than the step's may write to the model bucket.
- [ ] Every cdk-nag finding `src/rhi/pipeline/weights.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_weights.toml` or `tests/policy/acknowledged/pipeline_stack.toml`; CDK Pipelines creates the step's project and role in the pipeline stack, so what the step's role needs is in the latter.
- [ ] `tests/unit/test_copy_weights.py` covers the job against stubs of Hugging Face and of S3, and `tests/synth/test_weights_job.py` the step, its place and its role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_copy_weights.py` passes: only admitted files are written, under the pool's prefix at its revision; a file the prefix already holds at its size is not downloaded again; a file removed from the manifest is removed from that prefix while another revision's files stay; a pool whose revision differs from its manifest's stops the job with nothing written; and no call the job makes reads an object of the bucket.
- [ ] `tests/unit/test_weights.py::test_committed_manifests_are_valid` accepts `models/manifests/qwen--qwen3.8-27b-fp8.json`.
- [ ] `uv run --locked pytest tests/synth/test_weights_job.py tests/policy/test_pipeline.py` passes: the step comes before the stage's deployment, and its role alone may write to the bucket.
- [ ] In the account, the job copies the coursework model and the pool's prefix at its revision holds exactly the manifest's files (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.2, 4.4, 8.2
- `docs/design/evidence.md`, E28
