+++
id = "E2-63"
title = "Smoke tests that run against a local stack and against the deployed endpoint"
labels = ["size:M", "kind:test"]
epic = "E2"
blocked_by = ["E2-26", "E2-27", "E2-42", "E2-79", "E2-82"]
+++

## Context

Step 6 of the pipeline verifies each deployment from CodeBuild, against the deployed endpoint, with
a canary roster entry: health, rejection without a key, the forbidden routes, a full wake, hold and
completion on the mock pool, metrics present. When a change touches the engine image, the machine
image or a pool, the same test runs on the coursework pool and checks the served image digest and
model revision; a weekly canary does the GPU test regardless (section 8.2). Those GPU runs are phase
2's, but they reuse these tests, so the pool under test is a parameter.

**What differs on a deployed stack.** Two tests cannot assert the same thing on both stacks. The
public load balancer forwards to the gateway only the paths of its table (issue E2-90) and answers
every other path with its own fixed 404 (issue E2-47), which carries no cause: so the forbidden
routes /invocations, /pause, /tokenize, /metrics and /health are 404 with the cause not_found when the
gateway answers them, as on the local stack, and on a deployed stack the test requires the 404 only.
And a deployed pool is cold only after idle_minutes without a request: a run soon after another, or
any stream after the first, finds it ready. So the test reads `GET /status` before the first stream,
and asserts the wake and the hold of that stream only when the pool read cold; the local stack's pool
always starts cold, so there the hold is always asserted.

The same tests run locally against the gateway and the mock engine in process, so that agents can
prove them without an account. Each test has a stable ID, which the reporter's failure report names
(section 8.4).

A deployment does not reach an existing instance by itself, so the pipeline's verification asserts
that the release the instances serve is the one deployed (section 4.3). The served image digest and
model revision are what the instance's lifecycle agent reports, in its heartbeat and as a metric
(section 4.1; issue E2-35).

**What the deployed-only tests rest on.** They query the workspace for the gateway's metrics (issue
E2-79) and the pool's, by the per-engine table (issue E2-82) and the labels the collectors add (issue
E2-58), and compare the lifecycle agent's identity metric (issue E2-35) with the pool's release
parameter (issue E2-42). This issue waits for those, so that the names it queries exist. It runs
none of them: they are collected only when an endpoint is given, which only the pipeline's
verification step does (issue E2-64).

The environment gives a stack to test as RHI_SMOKE_ENDPOINT (the endpoint's address), RHI_SMOKE_KEY
(the canary's key), RHI_SMOKE_POOL (the pool under test, the mock pool when unset), and, for the
deployed-only tests, the workspace's query address and each pool's release parameter as issue E2-64
passes them.

## Deliverables

- [ ] `tests/smoke/conftest.py` gives the tests an endpoint and a pool: the endpoint whose address and canary key RHI_SMOKE_ENDPOINT and RHI_SMOKE_KEY give, or a local stack built in process from the gateway, the mock engine, and stand-ins for the scaler and for the target group's health; the pool is RHI_SMOKE_POOL, or the mock pool when it is unset.
- [ ] `tests/smoke/test_smoke.py` holds the tests both stacks can answer, each with a fixed ID: the hello route answers; a request without a key is refused with a 401 and the cause invalid_key; the routes /invocations, /pause, /tokenize, /metrics and /health are 404, with the cause not_found when the gateway answers them, and on a deployed stack, where the public load balancer answers them with its fixed 404, with that status only; a stream in each wire format completes with its usage; and when GET /status read the pool cold before the first stream, that stream wakes it and is held with keep-alives until it is ready.
- [ ] `tests/smoke/deployed/` holds the tests only a deployed stack can answer: the forbidden routes through the internal load balancer, the health route among them; the gateway's and the pool's metrics present in the workspace; and the image digest and model revision the pool's lifecycle agents report equal to the deployed release. They are collected only when an endpoint is given, so that locally none is skipped.
- [ ] `tests/smoke/ids.py` lists every ID, and is what the reporter's allow-list reads.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/smoke/test_smoke.py` passes against the local stack, with no test skipped, the forbidden routes answered with the cause not_found, and the first stream held, since the local stack's pool starts cold.
- [ ] `tests/smoke/test_smoke.py::test_ids_are_unique_and_listed` fails when a smoke test, the deployed-only ones included, has no ID or an ID missing from `tests/smoke/ids.py`.
- [ ] `tests/smoke/test_smoke.py::test_deployed_tests_need_an_endpoint` collects `tests/smoke/deployed/` in a subprocess and finds no test collected with RHI_SMOKE_ENDPOINT unset and every deployed-only test collected with it set.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 6.2, 8.2, 8.4
