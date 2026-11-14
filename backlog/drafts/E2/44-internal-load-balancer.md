+++
id = "E2-44"
title = "The internal load balancer: the four inference routes and nothing else"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-46", "E2-49", "E2-69"]
+++

## Context

The engine cannot be trusted to protect itself: vLLM's key covers only some path prefixes (section
4.1; evidence E23). The internal load balancer is the control: it forwards the four inference routes
and answers 404 to everything else, and only the gateway reaches it. It also gives health checks,
draining and least-outstanding routing (section 2).

Nothing probes an engine through it. Each target group checks its engines' health route directly,
and the gateway reads the result, the target group's health, through the AWS API (section 5.3). So
the health route has no forwarding rule, and a request for it through the load balancer is a 404
like any other route outside the four.

One listener serves every pool: HTTP on port 80, inside the VPC, which the security group of issue
E2-46 opens to the gateway's tasks only. A rule's conditions choose the pool: a header named
X-Rhi-Pool, whose value is the pool's name, which the gateway sets on every request it forwards
(issue E2-24) and which no client can reach the load balancer to set. Each target group sends to the
engine's port 8000, checks its engines every 5 seconds, which the cold-start estimate assumes
(section 5.2), and drains for the pool's `drain_seconds` (section 5.1).

The load balancer joins the pools stack, which issue E2-49 creates with the pool tables. With the
owner's pools table still empty, it holds the listener and its default 404 and no target group.

Section 2 also gives this load balancer stickiness per student. That is phase 2's, with the rest of
keeping a student on one instance (section 5.5); this issue adds none.

## Deliverables

- [ ] `src/rhi/pools/routing.py` creates an internal load balancer in the private subnets, with the internal load balancer's security group of issue E2-46, an idle timeout of 1200 seconds and one HTTP listener on port 80, and `src/rhi/pools/stack.py` adds it to the pools stack.
- [ ] Its listener forwards, to the target group of the pool named by the request's X-Rhi-Pool header, the four inference routes of section 4.1, each for the method POST only, and nothing else; its default action is a fixed 404.
- [ ] Each pool's target group sends to port 8000, checks health on the engine's health route every 5 seconds, routes by least outstanding requests, and has a deregistration delay equal to the pool's drain_seconds.
- [ ] Every cdk-nag finding `src/rhi/pools/routing.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_routing.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/synth/test_internal_routing.py` checks the listener, the rules, the default action and the target group of the mock pool.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_internal_routing.py` passes: one listener on port 80, exactly four forwarding rules per pool, a default 404, a 5-second health check on port 8000, and a deregistration delay of 300 seconds for the mock pool.
- [ ] `tests/synth/test_internal_routing.py::test_no_rule_forwards_the_health_route` fails a listener, built in the test, with a rule that forwards the health route.
- [ ] Through the internal load balancer, `/invocations`, `/pause`, `/tokenize`, `/metrics` and `/health` answer 404 on the deployed mock pool (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 2, 4.1, 5.1, 5.2, 5.3, 5.5
- `docs/design/evidence.md`, E13, E23
