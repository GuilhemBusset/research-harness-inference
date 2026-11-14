+++
id = "E2-30"
title = "Run the gateway on Fargate: its tasks, their environment, their role and their collector"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-05", "E2-12", "E2-43", "E2-44", "E2-47", "E2-49", "E2-58", "E2-76"]
+++

## Context

The gateway is an always-on service on Fargate: two tasks of 0.5 vCPU and 1 GB (sections 2 and
11.2). One task instead of two is a lever on the idle cost that keeps rolling deployments and loses
zone redundancy (section 11.2), so the task count is the configuration key `gateway_tasks`, two by
default (issue E2-76). This issue runs the service; how the public load balancer reaches it, the
target group whose deregistration delay keeps a deployment from cutting a request, and the health
check, are issue E2-90. Until then the service has no load balancer, and nothing reaches it from the
internet.

**What the task is given.** The image, its entry point and the environment variables it reads are
issue E2-76's; this issue fills that environment from the stage: the pools as the stage loaded
them, the default pool when the configuration has one, the internal load balancer of issue E2-44 as
the upstream, each pool's target group and the reference of each pool's secret of issue E2-43, the
names of the four tables of issue E2-49, the scaler function of issue E2-12, and the gateway's
numeric settings from the configuration keys of issue E2-76. The gateway reads each secret's value at
start; no template, parameter or environment holds it. In the synthesized template some of these
values are references that CloudFormation resolves at deployment; the test replaces each by a
placeholder before it parses the environment. The gateway listens on port 8080 and serves its
metrics on localhost port 9091, where its collector scrapes them (issue E2-58).

**The deployed configuration may hold no pool.** The owner's configuration in SSM has an empty pools
table until the owner adds the mock pool (issue E2-71), and then no default pool. The gateway starts
with no pool and no default pool (issue E2-76), and this issue's environment leaves RHI_DEFAULT_POOL
out when there is none, so the service reaches a steady state on that configuration too. A test
synthesizes the stage from such a configuration.

The gateway reads the health of the pools' target groups through the AWS API to know when a pool is
ready (section 5.3), and each pool's state record and the scaler's note (issue E2-73), so its role
may read them. Its collector writes to the Prometheus workspace with the task's role.

**The log group has a fixed name**, `/rhi/gateway`, so that Grafana's role names it without a
reference to this stack (issue E2-60). It keeps 90 days, which issue E4-18 then holds every log group
to.

The image asset's build context is the one `src/rhi/packaging.py` gives a Python service (issue
E1-16): the shared package, the gateway's package and Dockerfile, and the gateway group's
requirements with their hashes.

This issue changes `tests/policy/acknowledged/`, a protected path (ADR 0001, decision 10); the
builder may write the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/gateway/service.py` runs the image of issue E2-76 as a Fargate service of tasks of 0.5 vCPU and 1 GB, as many as gateway_tasks, in private subnets, listening on port 8080, with no load balancer; `src/rhi/gateway/stack.py` holds it and `src/rhi/stage.py` adds the gateway stack to the stage.
- [ ] The task's environment holds every variable the settings reader of issue E2-76 requires, RHI_DEFAULT_POOL only when the configuration has a default pool, and the numeric ones from configuration: the upstream is the internal load balancer of issue E2-44, and each pool's secret is given by reference, never by value.
- [ ] The task role may read the keys table, read and write the usage and heartbeat tables, read the pool-state table and the pools' secrets, read the health of the pools' target groups, invoke the scaler function of issue E2-12 and write to the Prometheus workspace; nothing else.
- [ ] A metrics collector runs beside the gateway in the same task, from an image pinned by digest, with the configuration of issue E2-58 and, in its environment, the workspace's remote-write address and the region; both containers log to the log group named /rhi/gateway, which keeps 90 days.
- [ ] Every cdk-nag finding `src/rhi/gateway/service.py`, `src/rhi/gateway/stack.py` or `src/rhi/stage.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/gateway_service.toml`, `tests/policy/acknowledged/gateway_stack.toml` or `tests/policy/acknowledged/stage.toml`.
- [ ] `tests/synth/test_gateway_service.py` checks the service, the task's environment, the log group and the role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_gateway_service.py` passes: two tasks under the example configuration and one when the test's configuration sets gateway_tasks to 1, both containers logging to the log group /rhi/gateway with a retention of 90 days, and a role limited as stated.
- [ ] `tests/synth/test_gateway_service.py::test_the_environment_is_what_the_gateway_reads` parses the task's environment with the settings reader of `services/gateway/rhi_gateway/settings.py`, which accepts it, and finds the mock pool's upstream and target group matching a forwarding rule of the internal load balancer to that pool's target group.
- [ ] `tests/synth/test_gateway_service.py::test_a_configuration_with_no_pool` synthesizes the stage from a configuration that leaves out every pool and https_listener, and finds the task's environment accepted by the settings reader, with an empty pools table and no RHI_DEFAULT_POOL.
- [ ] In the account, the gateway's service keeps its desired count of tasks running on the configuration the owner deployed, with or without a pool (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 2, 5.3, 6.2, 11.2, 17
