+++
id = "E2-90"
title = "Reach the gateway through the public load balancer, so that a deployment never cuts a request"
labels = ["size:S", "kind:infra"]
epic = "E2"
blocked_by = ["E2-30", "E2-47"]
+++

## Context

The gateway's service runs with no load balancer (issue E2-30). This issue attaches it to a target
group of the public load balancer, behind one rule of its HTTPS listener.

**A deployment never cuts a request.** A gateway deployment must not cut a held request or a long
stream. ECS waits for the old task's target to finish deregistering before it stops the task, so the
gateway's target group is set to 1800 seconds: requests up to 30 minutes long finish, and a gateway
deployment takes that long to complete (section 8.5; the 1800 seconds are a default of section 17).
Spike S7 checks it in the account.

**Only when the listener exists.** The listener exists only while the configuration's key
https_listener is true (issue E2-47), and it is false in the deployed configuration until the
owner's domain resolves. ECS refuses a service whose target group belongs to no load balancer, so
the target group, the rule and the service's attachment to them are all created only when the
listener exists; with the key false the service runs as issue E2-30 left it. The example
configuration sets the key to true, so the other synthesis tests see the rule; a test synthesizes
with the key false.

**The rule.** The public load balancer forwards to the gateway only the paths of its table (the
forwarded and local routes all sit under /v1/, or are /status, /wake and /api/hello), and answers
every other path with its fixed 404 (issue E2-47); the gateway's own table is still the control. The
listener's rules need distinct priorities: the gateway's is 10, the issuer's 20 (issue E2-52) and
Grafana's 30 (issue E2-60). The design gives none; their paths do not overlap, so the order only
keeps them distinct and leaves room between them.

**The health check.** The route table is closed (section 6.2, step 1), and the only route answered
without a key, `HEAD /api/hello`, cannot be a load balancer's health check, which sends GET. The
check therefore sends `GET /v1/models` without a key and counts the 401 as healthy: the gateway
answers it from the headers alone, with no lookup, and the design's route list stays as it is.

## Deliverables

- [ ] `src/rhi/gateway/service.py` attaches the gateway's service, on port 8080, to a target group whose deregistration delay is 1800 seconds and whose health check sends GET /v1/models without a key and counts the status 401 as healthy, reached through one rule of the public load balancer's listener, at priority 10, for the paths /v1/*, /status, /wake and /api/hello.
- [ ] The target group, the rule and the attachment exist only when the public load balancer has a listener, which it has only while https_listener is true (issue E2-47); with the key false the service has no load balancer and nothing reaches it from the internet.
- [ ] Every cdk-nag finding `src/rhi/gateway/service.py` or `src/rhi/gateway/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/gateway_service.toml` or `tests/policy/acknowledged/gateway_stack.toml`.
- [ ] `tests/synth/test_gateway_routing.py` checks the target group, its health check and the rule with the listener, and their absence without it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_gateway_routing.py` passes under the example configuration: one rule at priority 10 for the paths /v1/*, /status, /wake and /api/hello, a deregistration delay of 1800 seconds, and a health check on GET /v1/models that expects 401.
- [ ] `tests/synth/test_gateway_routing.py::test_no_listener_no_target_group` synthesizes the stage with https_listener false and finds the gateway's service with no load balancer, and no target group or listener rule of the gateway in any template.
- [ ] In the account, once the listener exists, the gateway answers `HEAD /api/hello` through the public load balancer, its targets are healthy, and a stream in flight survives a gateway deployment (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.3, 6.2, 8.5, 17
