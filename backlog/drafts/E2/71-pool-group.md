+++
id = "E2-71"
title = "A pool's Auto Scaling group and launch template, over three zones"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-38", "E2-39", "E2-44", "E2-69"]
+++

## Context

A pool becomes one Auto Scaling group and one target group (section 4.2). The fleet-cap invariant of
issue E2-39 holds the group to a minimum of zero, no desired capacity in the template, no scaling
policy and its pool's maximum; this issue builds the group to it. The warm pool and the nightly
action are issue E2-72.

The group spans three zones (section 5.4): the private subnets of issue E2-45, whose three zones are
chosen from the deploying region when the stack is deployed. A pool may list fallback instance types
after its first; a warm pool allows several types in one group if they are On-Demand and carry no
weights (section 5.4; evidence E10). New instances are protected from scale-in, and the scaler lifts
that protection when it chooses which instance leaves.

Zone rebalancing is suspended, because it replaces instances in service (section 4.3). CloudFormation
has no property that suspends a group's process for good, so the group's own construct does it: a
custom resource calls Auto Scaling's `SuspendProcesses` for the process `AZRebalance` when the group
is created and whenever it is replaced. The scaler resumes only the `Launch` process (issue E2-10),
so rebalancing stays suspended. The custom resource's function and role come with cdk-nag findings,
acknowledged with their reasons in the module's file.

Instances have no public address and no SSH, and IMDSv2 is required (section 4.3). The launch
template also lets an instance read its own tags through the instance metadata, and tags it with
`rhi-pool` (the pool's name) and `rhi-release-parameter` (the name of the pool's release parameter,
`/rhi/pools/<pool>/release`, which issue E2-42 writes): that is how the boot program of issue E2-38
finds its release without any setting in the user data.

The GPU pools' machine image comes from a parameter in the owner's account, written by a promotion
job (issue E3-01). The mock pool runs on `t4g.small`, an arm64 type, and needs an image with a
container runtime, as the GPU image has, and the AWS CLI, curl and python3 the boot program uses
(issue E2-38). The design names none, so this issue takes it from a parameter AWS publishes for an
arm64 Amazon Linux 2023 image that has Docker (the ECS-optimized image, for example), read when the
stack is deployed. Whether that image also carries the AWS CLI, curl and python3 is seen only on the
deployed pool: issue E2-40's check that an instance of the mock pool serves shows it. The group joins
the pools stack.

**The first pool in the account.** The owner's configuration in SSM has an empty pools table until
the owner adds a pool; with none, the stage builds no group and deploys as before. Once this issue
is merged, the owner adds the example's mock pool to SSM, so that every later pool issue deploys
against a real group. The mock pool's entry needs none of the top-level keys of issue E2-69: with one
pool, the default pool is that pool and the allowed types are its own.

## Deliverables

- [ ] `src/rhi/pools/group.py` creates, for each configured pool, a launch template (no public address, IMDSv2 required, instance tags readable through the instance metadata, no key pair, an encrypted EBS volume, the instances' security group of issue E2-46, the user data of issue E2-38, and the instance tags rhi-pool and rhi-release-parameter) and an Auto Scaling group tagged rhi-pool over the private subnets of the three zones, and `src/rhi/pools/stack.py` adds them to the pools stack.
- [ ] The group has a minimum of 0, a maximum equal to the pool's max_instances, no desired capacity in the template, no scaling policy and new instances protected from scale-in, and a custom resource suspends its AZRebalance process when the group is created or replaced.
- [ ] The group launches the pool's instance types in the order the configuration lists them, On-Demand only and without weights, and registers its instances with the pool's target group of issue E2-44.
- [ ] The mock pool's machine image is read, when the stack is deployed, from a public parameter AWS publishes for an arm64 Amazon Linux 2023 image with Docker, and no image is looked up at synthesis.
- [ ] Every cdk-nag finding `src/rhi/pools/group.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_group.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/synth/test_pool_group.py` checks the launch template and the group of the mock pool.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_group.py tests/policy/test_fleet_cap.py` passes on the app with the example configuration's mock pool: a maximum of 2, no desired capacity, IMDSv2 required, the two instance tags, and AZRebalance suspended by the custom resource.
- [ ] `tests/synth/test_pool_group.py::test_types_in_order` shows a pool configured with two instance types listing both in order, On-Demand only and without weights.
- [ ] `scripts/check.sh synth` exits 0, which shows the image and the zones need no context lookup.
- [ ] The owner adds the mock pool of `config.example.toml` to the pools table of the configuration in SSM, as one parameter under the pools path of the configuration, with the configuration writer of issue E1-17 (needs aws:account).

## References

- `docs/design/system-design.md`, sections 4.2, 4.3, 5.1, 5.4, 8.4
- `docs/design/evidence.md`, E10
