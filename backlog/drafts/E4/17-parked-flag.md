+++
id = "E4-17"
title = "A parked configuration that leaves only data and the pipeline"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E2-41", "E2-42", "E2-60", "E2-72", "E2-88", "E2-90", "E4-04", "E4-06", "E4-09", "E4-10", "E4-18", "E4-37"]
+++

## Context

The idle floor is about $170 a month (section 11.2). `parked` is a configuration flag that removes
the load balancers, the NAT gateway, the Fargate services and the warm pool, leaving data and the
pipeline: a few dollars a month. It is the right state between cohorts, and outside homework periods
if the owner accepts unparking before use. It is delivered and tested in phase 3. A parked platform
has no gateway and answers nothing (section 6.2, step 8).

This issue is the flag and what the stage builds under it. The pipeline's verification and the
weekly canary, which must pass without an endpoint, are issue E4-38; until it lands the owner does
not park.

**What goes.** With no load balancer, what sits behind one goes too: the gateway service with its
target group and listener rule (issue E2-90), the issuer (reached only through a rule of the public
load balancer) and Grafana's service. With no pool
group, the scaler, its schedule and the alarms on a pool, on the scaler or on the gateway have
nothing to watch and go too. The web ACL protects only the public load balancer and goes with it.

**What stays.** Parking must never touch state: the tables, the model bucket, the workspace, the
Cognito user pool, the pools' secrets, under the same logical IDs (section 8.5). The alerts topic,
the budget with its latch and the anomaly monitor stay, because the budget still counts. The
class-day topic stays, so that its confirmed e-mail subscription survives. The access-log bucket and
every log group whose name is fixed stay: a log group is kept by its retention when its resource
leaves a template, and one of a fixed name would make the unparking deployment fail on a name
already taken. Those are the web ACL's, the gateway's /rhi/gateway (issue E2-30), the scaler's
/rhi/scaler (issue E2-12), the issuer's /rhi/issuer (issue E2-52) and each pool's /rhi/pools/<pool>
(issue E2-41): each stays in its stack while the service that writes to it goes. The VPC, its subnets, its security groups and its S3
endpoint cost nothing and stay; with no NAT gateway the private subnets keep their names, so their
logical IDs do not change.

**An export in use cannot be deleted.** CloudFormation refuses to delete an export that another
stack still imports, and the pipeline deploys a stack before the stacks that import from it. A
resource that parking removes and that another stack imports (the public load balancer's listener,
the internal load balancer, a pool's group, the scaler function, the gateway's log group) would make
the parking deployment fail. When the flag is set, every stack that imported such a resource must
deploy before the stack that exported it, or must not reach it through an export at all (for
example, an alarm that names a group by a name both stacks derive from the pool's name). The test
holds the rule, whatever the means. The guardrails stack is one such importer: the alarms of issues
E4-09 and E4-10 watch the pools' groups, the gateway's log group and the public load balancer, and
the guardrails stack deploys after the stacks that hold them. So this issue may change how
`src/rhi/guardrails/alarms_fleet.py` and `src/rhi/guardrails/alarms_service.py` reach what they
watch, and names both.

A test that lists what a parked synthesis may hold keeps every later construct honest: one that
forgets the flag fails it. The stateful resources are those `tests/policy/stateful_logical_ids.json`
records (issue E2-88); this issue reads that file and does not change it, and checks the parked
synthesis in its own test, since the policy tests' fixture synthesizes the example only.

`parked` is a deployment flag and nothing else. A pool the admin stops is in mode `disabled`, which
is a record, not a deployment (section 11.3; issue E4-03). The key is read by the CDK app when it
synthesizes the stage, and by the pipeline's steps through the same synthesis (issue E4-38); false by
default, so that a configuration in SSM written before this issue loads unchanged and builds the
stage as it was.

## Deliverables

- [ ] `src/rhi/config.py` gains parked, false by default; `config.example.toml` gives it false; `tests/unit/test_config.py` holds the default.
- [ ] `src/rhi/stage.py` gives the flag to every stack of the stage; when it is set, `src/rhi/network/stack.py` builds no public load balancer, listener, alias record or web ACL, and `src/rhi/network/vpc.py` no NAT gateway, keeping its subnets under the same names.
- [ ] When it is set, `src/rhi/pools/stack.py` builds no internal load balancer, pool group, warm pool, scaler or scaler schedule, `src/rhi/gateway/stack.py` no gateway service, target group or listener rule, `src/rhi/observability/stack.py` no Grafana service, `src/rhi/access/stack.py` no issuer, and `src/rhi/guardrails/stack.py` no alarm on a pool, on the scaler or on the gateway.
- [ ] The tables, the model bucket, the workspace, the user pool, the pools' secrets, the alerts topic, the budget and its latch function, the anomaly monitor when it is on, the class-day topic, the access-log bucket and every log group of a fixed name, /rhi/gateway, /rhi/scaler, /rhi/issuer and /rhi/pools/<pool> of each pool included, stay, under the same logical IDs.
- [ ] When the flag is set, every stack that imports, in the unparked stage, a resource the flag removes deploys before the stack that exports it, or does not import it through an export; the alarms of `src/rhi/guardrails/alarms_fleet.py` and `src/rhi/guardrails/alarms_service.py` keep that rule for the groups, the log group and the load balancer they watch.
- [ ] `tests/synth/test_parked.py` synthesizes the stage both ways and fails when the parked synthesis holds a resource of a type outside a list the test keeps of what may stay, when a resource the policy tests' record of stateful logical IDs lists is missing from it or has another logical ID or fewer protections than in the unparked one, and when the rule on exports above does not hold.
- [ ] Every cdk-nag finding `src/rhi/network/vpc.py`, `src/rhi/stage.py`, `src/rhi/guardrails/alarms_fleet.py` or `src/rhi/guardrails/alarms_service.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/network_vpc.toml`, `tests/policy/acknowledged/stage.toml`, `tests/policy/acknowledged/guardrails_alarms_fleet.toml` or `tests/policy/acknowledged/guardrails_alarms_service.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_parked.py tests/unit/test_config.py` passes: the parked synthesis has no load balancer, NAT gateway, Fargate service, Auto Scaling group or scaler function, it keeps the log groups /rhi/gateway, /rhi/scaler, /rhi/issuer and /rhi/pools/mock under the logical IDs they have unparked, every recorded stateful resource has the same logical ID and protections in both, and a configuration that leaves parked out builds the stage as before.
- [ ] `tests/synth/test_parked.py::test_no_export_in_use_is_deleted` fails a pair of stages, built in the test, in which a parked stack drops an export that another stack, deployed after it, imported.
- [ ] The owner says whether about $170 a month while the endpoint is up is acceptable and when the system is parked, part of decision 6 of the design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 6.2, 8.5, 11.2, 11.3, 14, 15
