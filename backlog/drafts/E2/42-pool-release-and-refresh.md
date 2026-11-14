+++
id = "E2-42"
title = "A release reaches every instance: one parameter, and a refresh when it changes"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-10", "E1-16", "E2-43", "E2-70", "E2-82", "E2-87"]
+++

## Context

A deployment does not reach an existing instance by itself (section 4.3). A change to the engine
image, the machine image or a pool's release therefore starts an instance refresh, which replaces
instances in service first and then those in the warm pool (evidence E10), and the pipeline's
verification asserts that the served release equals the deployed one. Each such deployment costs one
start per warm instance.

**Protected instances are replaced.** Every new instance is protected from scale-in (issue E2-71),
and an instance refresh's preference for protected instances, ScaleInProtectedInstances, waits on
them by default, then fails after an hour: any deployment made while an instance is in service would
fail with it. So the refresh's preferences set ScaleInProtectedInstances to Refresh, which replaces
them; the termination hook still lets each one drain (issue E2-36).

**The parameter.** Each pool's release is one SSM parameter, named `/rhi/pools/<pool>/release`, the
name the launch template already puts in the instance tag `rhi-release-parameter` (issue E2-71). Its
value is the JSON object whose keys issue E2-33 fixes:

- what an instance must hold, compared by the agent: the engine image by repository and digest, the
  model revision, the engine flags, and the location and SHA-256 digest of the release's files;
- outside the comparison: the measured start when the configuration gives one (issue E2-04), and the
  instance's settings, which the boot program (issue E2-38), the collector and the agent read: the
  pool's name, its group's name, the hook names `rhi-launch` and `rhi-termination` (fixed here, and
  given to the hooks by issue E2-40), its log group, the workspace's remote-write address, the
  heartbeat table's name and the pool secret's ID.

A change to the measured start or to a setting changes a deadline or an address, not what an instance
serves, so it starts no refresh: the next start reads it anyway.

**The files** are one tar archive, in the layout issue E2-38 fixes: the unit files of issue E2-70,
the node's collector configuration of issue E2-58, and the lifecycle agent's bundle. CDK zips a
folder asset at publication, which would leave the digest unknown at synthesis, so the archive is
built at synthesis as one file and published as a file asset, which CDK uploads unchanged; the
digest the parameter names is the SHA-256 of the archive's bytes. The archive is reproducible
(entries in sorted order, one fixed modification time, owner and group 0), so that the same files
give the same bytes and a synthesis that changes nothing in them starts no refresh. The bundle is the packaging module's layout for a function (issue E1-16), for the lifecycle group:
`rhi_common`, `rhi_lifecycle` and the group's libraries at their locked versions, built without
Docker. Those libraries (the AWS SDK and prometheus-client) are pure Python, so one bundle serves
both processor architectures, and the agent's Python is the version the bundle is built for (issue
E2-33). A test keeps it so: a library of the lifecycle group without a pure-Python wheel in `uv.lock`
fails it. CDK file assets go to the bucket the bootstrap creates (issue E1-08); "the pipeline's
artifact bucket" of section 4.3 is that one.

**The engine image** is built by the pipeline into ECR (section 4.3): for the mock pool, the mock
engine's image of issue E2-82. It is built for the processor architecture of the pool's instance
types: arm64 for a type whose family name has a g among the letters after its generation number
(`t4g`), amd64 for every other (the GPU pools' `g7e`); a pool whose types mix the two is refused at
synthesis. The pipeline's asset step builds both (issue E2-87). The image asset is tagged by the hash
of its sources; the digest the release names is the registry's, read at deployment.

**Grants.** This issue creates the parameter, the files asset and the image asset, so it grants the
pool's instance role (issue E2-41) the right to read that parameter and no other, read that asset's
object and no other object of the bucket, and pull that image. ECR's authorization token is granted
by AWS on every resource only, and is listed with its reason in the module's acknowledgement file.

## Deliverables

- [ ] `src/rhi/pools/release.py` builds each pool's engine image from its engine's Dockerfile, in the build context the packaging module of issue E1-16 gives, as an image asset for the architecture of the pool's instance types by the rule of Context, refuses a pool whose types mix the two, names the image in the release by its registry digest, and `src/rhi/pools/stack.py` adds the releases to the pools stack.
- [ ] It builds one tar archive in the layout of issue E2-38, holding the unit files, the node's collector configuration and the lifecycle agent with its libraries, the same bytes for the same files, publishes it as a file asset uploaded unchanged, and writes each pool's release to the SSM parameter /rhi/pools/<pool>/release when the stack is deployed, with the keys of issue E2-33: the compared release, whose files digest is the SHA-256 of the archive's bytes, the measured start when the configuration gives one, and the settings of Context.
- [ ] The pool's instance role may read that parameter and no other parameter, read that asset's object and no other object of the bucket, and pull the pool's engine image.
- [ ] When the compared release or the machine image ID the launch template resolves at deployment changes, the deployment starts an instance refresh of the pool's group, whose preferences replace instances protected from scale-in, and does not finish before the refresh does; a deployment that changes neither, or only the measured start or a setting, starts no refresh.
- [ ] Every cdk-nag finding `src/rhi/pools/release.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_release.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/synth/test_pool_release.py` checks the parameter, the archive, the grants, what triggers the refresh, and that every library of the lifecycle group has a pure-Python wheel in `uv.lock`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_release.py` passes: two syntheses that differ only in the engine's Dockerfile, or only in one unit file, differ in the release and in the refresh trigger; two that differ only in an unrelated resource, the measured start or a setting have the same trigger; the trigger references the pool's image parameter, resolved at deployment; the refresh's preferences set ScaleInProtectedInstances to Refresh; and the stack's deployment waits on the refresh.
- [ ] `tests/synth/test_pool_release.py::test_the_mock_image_is_arm64` shows the mock pool's engine image asset built for arm64, a pool on g7e.2xlarge built for amd64, and a pool listing t4g.small and g7e.2xlarge refused.
- [ ] `tests/synth/test_pool_release.py::test_the_agent_reads_the_parameter` reads the mock pool's synthesized parameter value, with each reference replaced by a sample value, with the release reader of issue E2-33, and finds the hook names rhi-launch and rhi-termination and every setting of Context, and the parameter's name outside the configuration's prefix /rhi/config/.
- [ ] `tests/synth/test_pool_release.py::test_the_boot_program_accepts_the_archive` runs `services/engine/boot/rhi-boot.sh` in a temporary root on the mock pool's synthesized archive and parameter value, with stand-ins for the instance metadata, the AWS CLI and systemctl, and finds the digest accepted, the unit files installed, `/opt/rhi/release/collector/config.yaml` and the agent's package in place, and the units rhi-collector and rhi-lifecycle started.
- [ ] The pipeline publishes the mock engine's image for arm64 and writes the mock pool's release parameter, and the deployment waits for the mock pool's refresh (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 8.2
- `docs/design/evidence.md`, E10
