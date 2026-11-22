+++
id = "E4-51"
title = "Session runbook: the status command shows the verification's description, and the idle wait counts in-flight requests"
labels = ["size:S", "kind:docs"]
epic = 6
+++

## Context

Two steps of the runbook for a session switched on and off tell the owner to look at something
the given command or query does not show (follow-up #544, findings 8 and 10).

- Check, step 3: the owner must tell a `verify/<stage>` status whose description reads "The
  verification's tests ran and passed." from one whose description begins "No test ran". The
  `gh api .../commits/main/status` command the step gives prints each status's context and state
  only, so both read `success` and look the same.
- Verify, step 4: before counting the pool's idle interval, the owner waits until the engines run
  nothing and then until the gateway holds nothing for the pool, with the query
  sum by (pool) (rhi_gateway_requests_held). A held request can be forwarded between the two
  checks, and the step never looks at the gateway's in-flight gauge, so the owner can start the
  idle wait early and then read a pool that has not fallen to zero as a fault. The gateway
  publishes both gauges per pool (`services/gateway/rhi_gateway/experience.py`):
  rhi_gateway_requests_held and rhi_gateway_requests_in_flight.

## Deliverables

- [ ] In `docs/runbooks/session-on-and-off.md`, the command of Check step 3 prints each status's context, state and description, and the step says which description marks a verification whose tests ran.
- [ ] In `docs/runbooks/session-on-and-off.md`, Verify step 4 has the owner wait until the single query sum by (pool) (rhi_gateway_requests_held + rhi_gateway_requests_in_flight) reads zero, or no data, for the pool, before counting the pool's idle interval.

## Acceptance criteria

- [ ] `grep -F '\(.description)' docs/runbooks/session-on-and-off.md` exits 0.
- [ ] `grep -F 'rhi_gateway_requests_held + rhi_gateway_requests_in_flight' docs/runbooks/session-on-and-off.md` exits 0.
- [ ] `uv run --locked pytest tests/unit/test_runbooks.py` passes.

## References

- `docs/runbooks/session-on-and-off.md`
- `services/gateway/rhi_gateway/experience.py`
- `docs/design/system-design.md`, section 5.3
