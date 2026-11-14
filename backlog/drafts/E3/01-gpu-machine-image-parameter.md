+++
id = "E3-01"
title = "GPU pools take their machine image from a parameter in the owner's account"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-71"]
+++

## Context

The launch template names an AWS-published GPU machine image by ID, read from an SSM parameter in
the owner's account (section 4.3). A promotion job changes that parameter only after a canary start
on the new image passes (issue E3-02), so an unrelated merge can never change the driver under a
pinned engine. A CloudFormation parameter that reads AWS's own public path would take the latest
image on every stack update (evidence E20). The image is AWS's base GPU image with the open-source
NVIDIA driver on Ubuntu 22.04 (section 4.3; evidence E10); its ID comes from the account, never from
the repository.

Each GPU pool has its own parameter, named from a prefix that is a constant of
`src/rhi/pools/image.py` and the pool's name, beside the pool's release parameter of issue E2-42.
The name lies outside the configuration's path in SSM: the configuration loader reads every
parameter under its own path, /rhi/config/, as a key and refuses one it does not know (issue E1-17), and the image
is not configuration the owner edits but state the promotion job writes. The mock pool keeps the
public arm64 image issue E2-71 gives it. The owner writes each pool's first value by hand before the pool enters the
configuration in SSM (issues E3-40 and E5-03); every later value comes from the promotion job.

A GPU pool is a pool whose engine is not mock. The promotion job (issue E3-02) and the pipeline's
GPU verification (issue E3-12) live in the pipeline stack, which cannot refer to a resource of the
stage; they find a pool's group by asking Auto Scaling for its tags. Issue E2-71 already tags each
instance with rhi-pool; this issue puts rhi-pool, the pool's name, and rhi-engine, its engine, on
the group and on the launch template themselves.

## Deliverables

- [ ] `src/rhi/pools/image.py` gives each GPU pool's launch template a machine image read, when the stack is deployed, from the parameter whose name the module builds from its constant prefix and the pool's name, a prefix outside the configuration's path, /rhi/config/.
- [ ] No GPU pool reads an AWS-published parameter, and no image ID is in the repository.
- [ ] `src/rhi/pools/group.py` tags each pool's group and launch template with the pool's name under the key rhi-pool and its engine under the key rhi-engine.
- [ ] Every cdk-nag finding `src/rhi/pools/image.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_image.toml`.
- [ ] Every cdk-nag finding `src/rhi/pools/group.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_group.toml`.
- [ ] `tests/synth/test_pool_image.py` checks where each GPU pool's image comes from and the tags of each group.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_image.py` passes: a GPU pool of a configuration built in the test reads its image from the parameter image.py names for it, outside the configuration's path, the template holds no image ID, and the group and launch template of each pool carry the tags rhi-pool and rhi-engine, and the mock pool's image is still the public one.
- [ ] `scripts/check.sh synth` exits 0, which shows the image needs no context lookup.

## References

- `docs/design/system-design.md`, section 4.3
- `docs/design/evidence.md`, E10, E20
