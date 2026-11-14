+++
id = "E2-60"
title = "Grafana behind the load balancer's sign-in with Cognito"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-12", "E2-30", "E2-41", "E2-52", "E2-59", "E2-85"]
+++

## Context

The load balancer authenticates against the Cognito user pool of issue E2-85, with one admin user
and mandatory MFA; unauthenticated requests never reach Grafana (section 7.4). Grafana trusts the
identity header the load balancer sets, so a rule that forwarded to it without authenticating first
would let anyone name themselves admin: the authentication is the rule's first action, and no other
rule forwards to Grafana. Grafana accepts connections from the load balancer only, through the
security group of issue E2-46, on its port 3000 (issue E2-59), and its role can query the one
Prometheus workspace and named log groups, not the build logs that hold real configuration. Grafana
is served under the path prefix /grafana/ of the endpoint's domain (issue E2-59). The log groups it
reads are those of the gateway, the scaler, the issuer and each pool's instances, whose issues give
them fixed names: `/rhi/gateway` (issue E2-30), `/rhi/scaler` (issue E2-12), `/rhi/issuer` (issue
E2-52) and `/rhi/pools/<pool>` for each configured pool (issue E2-41). The role names them so, and
never by a reference to another stack; the pipeline's events are added by issue E4-30.

**Only when the listener exists.** The public load balancer has a listener only while the
configuration's key https_listener is true (issue E2-47), and it is false in the deployed
configuration until the owner's domain resolves. ECS refuses a service whose target group belongs to
no load balancer, so Grafana's target group, its rule and the service's attachment to them are
created only when the listener exists; with the key false Grafana's service runs with no load
balancer. The listener's rules need distinct priorities: Grafana's is 30, beside the gateway's 10
(issue E2-90) and the issuer's 20 (issue E2-52).

Grafana also reads CloudWatch metrics: the load balancers', Auto Scaling's and the pipeline's
(section 7.1), and the account's month-to-date charge (issue E4-14). CloudWatch grants metric reads
and the retrieval of log-query results on every resource only, so those few actions are listed, each
with its reason, in the acknowledged file of the module, which the wildcard invariant reads (issue
E1-07).

Spike S10 passes when the CI assertions (issues E2-59 and E2-65) and a deployed check pass; the
deployed check is an obligation here. Self-hosted Grafana is decision 11 of section 15; if the owner
already has AWS Organizations or a SAML provider, Managed Grafana is the better choice and the swap
is contained in one stack.

This issue creates `tests/policy/test_admin_surfaces.py` and changes `tests/policy/acknowledged/`.
`tests/policy/` is a protected path (ADR 0001, decision 10): the builder may write the files named
below because this issue names them.

## Deliverables

- [ ] `src/rhi/observability/grafana.py` runs the Grafana image as a Fargate service in private subnets, reached on port 3000 through a target group and one rule of the public load balancer's listener, at priority 30, for the path prefix /grafana/, whose first action authenticates against the user pool of issue E2-85, and `src/rhi/observability/stack.py` holds it.
- [ ] Grafana's target group, its rule and the service's attachment to them exist only when the public load balancer has a listener, which it has only while https_listener is true (issue E2-47); with the key false the service has no load balancer and nothing reaches it from the internet.
- [ ] The task's environment gives Grafana the workspace's query address and the region; no template synthesized from the example holds a real address.
- [ ] Grafana's task role may query the one workspace, read CloudWatch metrics, and read the log groups /rhi/gateway, /rhi/scaler, /rhi/issuer and /rhi/pools/<pool> of each configured pool, named by those names and never by a reference to another stack, so that the observability stack depends on no stack that already depends on it; it may not read a build project's log group.
- [ ] Every cdk-nag finding `src/rhi/observability/grafana.py` or `src/rhi/observability/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource, the CloudWatch reads among them, is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/observability_grafana.toml` or `tests/policy/acknowledged/observability_stack.toml`.
- [ ] `tests/synth/test_grafana.py` checks the rule's actions, the target group and the role, and `tests/policy/test_admin_surfaces.py` fails when a rule of the public load balancer for the path prefix /grafana/ forwards before it authenticates, or when any other rule forwards to Grafana.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_grafana.py tests/policy/test_iam_wildcards.py tests/policy/test_admin_surfaces.py` passes: the rule, at priority 30, authenticates before it forwards, and the role names the workspace and the log groups /rhi/gateway, /rhi/scaler, /rhi/issuer and /rhi/pools/mock only, each the name of a log group the stage creates, apart from the acknowledged CloudWatch read actions.
- [ ] `tests/synth/test_grafana.py::test_build_logs_are_out_of_reach` fails a template, built in the test, in which Grafana's role may read a CodeBuild log group.
- [ ] `tests/synth/test_grafana.py::test_no_listener_no_target_group` synthesizes the stage with https_listener false and finds Grafana's service with no load balancer, and no target group or listener rule of Grafana in any template.
- [ ] `tests/policy/test_admin_surfaces.py::test_detects_an_open_grafana` fails a template, built in the test, whose /grafana/ rule forwards without authenticating.
- [ ] In the account, a request to Grafana without sign-in never reaches it, the default admin account cannot log in, and the admin signs in with MFA and sees the workspace: this is spike S10, recorded as a measurement record (needs aws:deployed-environment).
- [ ] The owner confirms self-hosted Grafana over Amazon Managed Grafana, decision 11 of the design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 7.1, 7.4, 12, 15
- `docs/design/evidence.md`, E24
