+++
id = "E4-37"
title = "Both load balancers' access logs, in a bucket that keeps them 90 days"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E2-44", "E2-47", "E2-88", "E4-18"]
+++

## Context

Source addresses in load balancer logs are personal data, kept no longer than course end plus 90
days (section 6.6). The logs of both load balancers go to one bucket whose objects expire 90 days
after they are written, which makes "course end plus 90 days" hold, as for the log groups (issue
E4-18).

Elastic Load Balancing's older way to be let into such a bucket names an AWS-owned account per
region; the current one names its log-delivery service and no account, and AWS recommends it in
every region. This issue uses the service, so that no template carries an AWS-owned account ID. No
synthesized template is committed either: the tests synthesize at run time. The hygiene scanner's
allow-list therefore needs no entry and is not changed.

The stacks are environment-agnostic, and CDK's access-log helper on a load balancer may refuse a
stack with no fixed region. So the load balancers' access-log attributes and the bucket's policy are
set directly, not through that helper. Elastic Load Balancing writes only to a bucket encrypted with
S3-managed keys. Each load balancer writes under a prefix of its own.

**The policy comes first.** Elastic Load Balancing checks that it may write to the bucket when the
access-log attributes are set, and refuses them otherwise. Setting the attributes directly drops the
ordering the helper would add, so each load balancer depends on the bucket's policy explicitly: the
public one, in the same stack as the bucket, lists the policy among its dependencies, and the
internal one, in the pools stack, is in a stack that deploys after the network stack, which holds
the policy.

The bucket holds personal data and is kept when its stack is deleted, like every stateful resource:
this issue adds it to `tests/policy/stateful_logical_ids.json`. `tests/policy/` is a protected path
(ADR 0001, decision 10): the builder may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/network/access_logs.py` creates, in the stack `src/rhi/network/stack.py` builds, one private bucket encrypted with S3-managed keys, whose objects expire 90 days after they are written, kept when its stack is deleted, and whose policy lets Elastic Load Balancing's log-delivery service write under the load balancers' prefixes and names no account other than the stack's own.
- [ ] `src/rhi/network/edge.py` and `src/rhi/pools/routing.py` set each load balancer's access-log attributes directly, to that bucket under a prefix of its own; `tests/synth/test_access_logs.py` checks the bucket and both load balancers.
- [ ] Each load balancer depends on the bucket's policy: the public load balancer's resource names the policy in its dependencies, and the pools stack, which holds the internal one, depends on the network stack, which holds the policy.
- [ ] `tests/policy/stateful_logical_ids.json` records the bucket, and `tests/policy/test_retention.py` also fails when a bucket a load balancer logs to has no expiry or a longer one.
- [ ] Every cdk-nag finding `src/rhi/network/access_logs.py`, `src/rhi/network/edge.py` or `src/rhi/pools/routing.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/network_access_logs.toml`, `tests/policy/acknowledged/network_edge.toml` or `tests/policy/acknowledged/pools_routing.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_access_logs.py` passes: both load balancers log to the bucket under different prefixes, the bucket's objects expire after 90 days, and its policy's only principal is the log-delivery service.
- [ ] `tests/synth/test_access_logs.py::test_the_policy_comes_first` finds the bucket policy's logical ID in the DependsOn of the public load balancer, and the network stack among the pools stack's dependencies.
- [ ] `uv run --locked pytest tests/policy/test_retention.py tests/policy/test_stateful_resources.py` passes with the bucket recorded and kept on deletion.
- [ ] `uv run --locked pytest tests/synth/test_app.py` still passes with the access logs on: no template holds a 12-digit account ID.

## References

- `docs/design/system-design.md`, sections 6.6, 10
- The bucket policy for access logs: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/enable-access-logging.html
- `tools/hygiene/README.md`
