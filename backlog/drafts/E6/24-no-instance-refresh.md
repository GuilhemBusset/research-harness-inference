+++
id = "E6-24"
title = "Pools: no instance refresh"
labels = ["size:M", "kind:infra"]
epic = "E6"
blocked_by = ["E6-15", "E6-22"]
+++

## Context

A deployment that changes a pool's release starts an instance refresh of the pool's group today,
because a stopped warm instance would otherwise keep the old engine. Now a deployment replaces no
instance and needs none replaced (system design, section 4.3). A pool has no instance outside a
session (E6-15), has no warm pool (E6-22), and a deployment waits while a session is active
(E6-05): a release finds nothing to reach, and the next session's instances launch from the new
launch template and read the new release at their start. A refresh could only replace the
instances of a session, and give back GPUs that may take long to get again.

An instance that is in service when a release changes, because a session was booked while a
deployment was under way, keeps what it started with until it leaves. Its heartbeat says which
release it serves.

What goes: the custom resource that starts the refresh and waits for it, its two handlers, their
role, and the acknowledged findings about them. What stays of a release: the engine image, the
archive of files, the release parameter and the grants to read them.

**The verification's decision changes with it.** The step decides whether an execution verifies on
the coursework pool by asking Auto Scaling whether a refresh of a GPU pool's group began after the
execution started (`scripts/pipeline/gpu_verification.py`), and waits for that refresh. With no
refresh, it asks the release parameters instead, which the step may already read: an execution
changed a GPU pool's release when the release parameter of a pool whose engine is not the mock was
last written after the execution started. A deployment that changes only a setting the parameter
carries also writes it, and the GPU tests then run once more than needed, which costs one launch.
The step's role loses the two descriptions of Auto Scaling it held for the old decision.

`src/rhi/pipeline/` and `tests/policy/` are protected paths (ADR 0001, decision 10). This issue
names the files it changes there: the verification's role is narrowed, and the acknowledgements of
the refresh's functions are removed.

After this issue a deployment that changes a release starts no refresh, and the verification books
its instance (E6-08) on a group that the deployment left alone.

## Deliverables

- [ ] `src/rhi/pools/release.py` creates no resource that starts or waits for an instance refresh, `src/rhi/pools/refresh_handler.py` is deleted, and `src/rhi/pools/stack.py` builds none; the release parameter, the archive and the instance role's grants are as before.
- [ ] `scripts/pipeline/gpu_verification.py` names the coursework pool when the release parameter of a pool whose engine is not the mock was last written after the execution started, names nothing otherwise, and neither reads nor waits for a refresh.
- [ ] `src/rhi/pipeline/verify.py` gives the step's role no action of Auto Scaling, and `tests/policy/test_pipeline.py` no longer admits one on it.
- [ ] `tests/policy/acknowledged/pools_release.toml` acknowledges nothing about an instance refresh or its functions.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_release.py` passes: the pools stack holds no custom resource for a refresh, and still holds each pool's release parameter with its grants.
- [ ] `uv run --locked pytest tests/unit/test_gpu_verification.py` passes: a GPU pool's parameter written after the execution started names the coursework pool, one written before it names nothing, and the mock pool's parameter alone names nothing.
- [ ] `uv run --locked pytest tests/policy tests/synth/test_pipeline_verify.py` passes, and `tests/policy/test_pipeline.py::test_refuses_a_verification_that_may_do_more` refuses a verification that may describe instance refreshes.
- [ ] In the account, after a deployment that changes the coursework pool's release, `tests/smoke/deployed/test_gpu_release.py` passes in the verification on an instance the step booked (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.3 and 8.2
- `src/rhi/pools/release.py`
- `src/rhi/pools/refresh_handler.py`
- `scripts/pipeline/gpu_verification.py`
- `tests/policy/test_pipeline.py`
