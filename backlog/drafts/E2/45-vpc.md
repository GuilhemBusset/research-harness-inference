+++
id = "E2-45"
title = "The network: private subnets, one NAT gateway, three zones"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-04"]
+++

## Context

GPU instances and Fargate tasks are in private subnets with one NAT gateway for outbound calls
(section 6.5). The design keeps the NAT gateway: without it instances need public addresses, and a
security group would be the only thing between the internet and an engine with unauthenticated
routes. Pools span three zones (section 5.4), and instances read weights through an S3 gateway
endpoint (section 4.4). Nothing here may need a context lookup.

The stacks are environment-agnostic, and the CDK gives such a stack only two zones unless the stack
supplies its own: three selected from the zones of the deploying region at deployment time, which
needs no name in the repository and no lookup.

The VPC is the first resource of the network stack, which this issue creates and adds to the stage.
The security groups and both load balancers' public side join it (issues E2-46, E2-47 and E2-48).

## Deliverables

- [ ] `src/rhi/network/vpc.py` creates a VPC over three availability zones with public subnets for the load balancer, private subnets for tasks and instances, one NAT gateway, and a gateway endpoint for S3.
- [ ] The three zones are taken from the deploying region when the stack is deployed, with no context lookup and no zone name in the repository.
- [ ] `src/rhi/network/stack.py` defines the network stack, which holds the VPC and exposes it to the other stacks, and `src/rhi/stage.py` adds it to the stage.
- [ ] Every cdk-nag finding `src/rhi/network/vpc.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/network_vpc.toml`.
- [ ] Every cdk-nag finding `src/rhi/network/stack.py` or `src/rhi/stage.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/network_stack.toml` or `tests/policy/acknowledged/stage.toml`.
- [ ] `tests/synth/test_vpc.py` checks the subnets, the single NAT gateway and the endpoint.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_vpc.py` passes: three zones, one NAT gateway, no subnet for tasks or instances that assigns public addresses.
- [ ] `scripts/check.sh synth` exits 0 with no `cdk.context.json` written.

## References

- `docs/design/system-design.md`, sections 4.4, 5.4, 6.5
