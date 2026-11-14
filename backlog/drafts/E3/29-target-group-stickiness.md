+++
id = "E3-29"
title = "Each pool's target group keeps a student on one target, for an hour after the last request"
labels = ["size:S", "kind:infra"]
epic = "E3"
blocked_by = ["E2-44", "E3-28"]
+++

## Context

Each pool's target group has duration-based stickiness (section 5.5). The internal load balancer
picks a target for a first request by least-outstanding requests, and names it in an encrypted
cookie, `AWSALB`, on the response; a request that carries the cookie goes to the same target. The
duration is 1 second to 7 days and its expiry is reset after every request; stickiness is not
available with the weighted random algorithm, and least outstanding requests is not excluded
(evidence E13). When the target a cookie names is unhealthy, deregistered or draining, the load
balancer picks another and issues a new cookie, so scale-in and a failed instance need no code.

The duration is the pool's stickiness setting, 3600 seconds by default (section 17; issue E3-28).
The target groups are built with the internal load balancer (issue E2-44), which keeps
least-outstanding routing and the four forwarding rules.

The gateway is the load balancer's client and keeps the cookie per student and pool (issue E3-30).
Until it does, every request arrives without a cookie and routing stays least outstanding, so this
issue changes nothing a student sees on its own. AWS lists a healthy target in every zone as a
requirement for stickiness, which a pool of one or two instances does not have; spike S8 checks on
the mock pool that it holds anyway (issue E3-33).

## Deliverables

- [ ] `src/rhi/pools/routing.py` turns on duration-based stickiness, with the load balancer's own cookie, on each pool's target group, for the pool's stickiness duration, and keeps least outstanding requests as its routing algorithm.
- [ ] Every cdk-nag finding `src/rhi/pools/routing.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_routing.toml`.
- [ ] `tests/synth/test_pool_stickiness.py` checks every pool's target group.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_stickiness.py` passes: the mock pool's target group has stickiness enabled, of the load balancer cookie type, for 3600 seconds, with least outstanding requests as its algorithm, and a pool configured with 600 seconds gets 600.
- [ ] `uv run --locked pytest tests/synth/test_internal_routing.py` still passes: four forwarding rules per pool and a default 404.

## References

- `docs/design/system-design.md`, sections 2, 5.5, 17
- `docs/design/evidence.md`, E13
