+++
id = "E3-02"
title = "Promote a machine image only after a canary start on it passes"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E1-10", "E1-16", "E3-01", "E3-39"]
+++

## Context

The image parameter of a GPU pool changes through one job (section 4.3): start a canary on the
candidate image, and write the parameter only if the engine serves on it. The image must list the
pool's instance family, and the promotion job refuses a pool whose family it does not list. AWS's
base GPU image with the open-source NVIDIA driver on Ubuntu 22.04, release 20261110, lists G7e with
G6e, P5, P5e, P5en and P6, so the coursework pool's family is listed. The "single CUDA" variant of
that image is a different image whose notes list G6e, G7, P5 and P6 and not G7e (evidence E10): a
candidate taken from it must be refused for a G7e pool, which is what the test below checks.

**The first image of a pool is the owner's.** The launch template reads the parameter when the stack
is deployed (issue E3-01), so the parameter must hold a value before the pool enters the
configuration in SSM; the owner writes that first value by hand (issues E3-40 and E5-03). This job
serves every later image.

**The canary is not one of the group's instances.** The job never sets a group's capacity: the
scaler stays the only writer of a pool's size (section 5.1). So the job starts the canary itself,
with EC2's own call, from the launch template of the pool's group (found by the group's rhi-pool
tag, issue E3-01) with the candidate image in place of the template's, in one of the group's
subnets. Like the group's instances, it carries the tags rhi-pool and rhi-release-parameter that
the launch template gives (issue E2-71), which the boot unit reads; the job adds a tag of its own.
Auto Scaling does not know the instance, so the scaler never counts it; the lifecycle agent
on it recognises an instance that belongs to no group, writes no heartbeat, completes no hook, and
writes its outcome on the instance's serial console (issue E3-39), which the job reads. The instance
carries a tag of the job, and the job terminates it whatever the outcome.

**The job's role** may start an instance only from a launch template tagged as a GPU pool's (a
rhi-engine tag other than mock), pass only the roles under the GPU pools' IAM path (issue E3-39),
read the console of and terminate only the instances that carry its tag, write the image parameters,
and start this project's pipeline. AWS grants describing groups, launch templates, images and
instances only on every resource; those actions are acknowledged with their reasons.

After writing the parameter the job starts an execution of the pipeline, so the next deployment
takes the new image and refreshes the pool (issue E2-42).

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `scripts/pipeline/promote_image.py` takes a pool, a candidate image ID and the instance families the candidate lists, and refuses a pool whose family the candidate does not list before it starts anything.
- [ ] It finds the pool's group by its rhi-pool tag and starts one instance, outside the group, from the group's launch template with the candidate image, in one of the group's subnets, with the template's tags rhi-pool and rhi-release-parameter and a tag of the job's own; it never changes a group's capacity.
- [ ] It terminates the canary whatever its outcome and writes the pool's image parameter only when the canary's console line says the engine served; on a failed or timed-out canary it writes nothing and exits non-zero with a cause from an enumeration of the module.
- [ ] After writing the parameter, and only then, it starts an execution of this project's pipeline.
- [ ] `src/rhi/pipeline/image_promotion.py` adds a job that the owner starts by hand and nothing else starts, with the role the Context describes, and `src/rhi/pipeline/stack.py` builds it.
- [ ] `tests/policy/test_pipeline.py` fails when a role other than the promotion job's may write an image parameter or start an instance, and when the promotion job has a trigger.
- [ ] Every cdk-nag finding `src/rhi/pipeline/image_promotion.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_image_promotion.toml`.
- [ ] `tests/unit/test_promote_image.py` covers the script against stubbed clients, and `tests/synth/test_image_promotion.py` the job and its role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_promote_image.py` passes: the parameter is written after a serving canary and at no other time, the pipeline is started once after that write and never after a failed canary, the canary is terminated after a serving, a failed and a timed-out run, no call changes a group's capacity, and a pool of the G7e family is refused, before anything starts, for a candidate that lists G6e, G7, P5 and P6 only.
- [ ] `uv run --locked pytest tests/synth/test_image_promotion.py tests/policy/test_pipeline.py` passes: the job has no trigger, its role alone may write the image parameters and start an instance, and it may pass no role outside the GPU pools' IAM path.
- [ ] A newer release of AWS's base GPU image with the open-source NVIDIA driver on Ubuntu 22.04 is promoted for the coursework pool through the job after a canary start on g7e.2xlarge, and the pool's warm instances then run it (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.3, 5.1, 9
- `docs/design/evidence.md`, E10
