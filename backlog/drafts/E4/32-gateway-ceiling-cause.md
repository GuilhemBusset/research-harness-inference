+++
id = "E4-32"
title = "Gateway: a pool at its GPU-hour ceiling answers pool_ceiling_reached at once"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E2-27", "E2-78", "E4-02"]
+++

## Context

`pool_ceiling_reached` is a 402: the pool is at its GPU-hour ceiling, or the monthly budget has
latched it (section 6.2, step 8). The SDKs do not retry a 402, so the student learns at once
instead of being held for minutes for a pool the scaler will not start. The latched case is already
handled by the hold (issue E2-25), from the pool's mode. The ceiling is not a mode: the scaler
computes it on every run and says it in its note for the pool (issue E4-02).

`GET /status` lists such a pool as `unavailable` with that cause (issue E2-27), and a wake for it
asks for nothing (section 5.3; issue E2-78).

## Deliverables

- [ ] `services/gateway/rhi_gateway/hold.py` ends a request for a pool whose scaler note says it is at a GPU-hour ceiling at once, with the cause pool_ceiling_reached, without a hold and without a wake.
- [ ] `services/gateway/rhi_gateway/local.py` lists such a pool in `GET /status` as unavailable with the cause pool_ceiling_reached, and answers a `POST /wake` for it at once with that same record, never a 402, asking the scaler for nothing.
- [ ] `tests/unit/gateway/test_hold.py` and `tests/unit/gateway/test_local.py` cover a pool at its ceiling.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_hold.py tests/unit/gateway/test_local.py` passes: a request for a pool whose note says it is at a ceiling is answered 402 with the cause pool_ceiling_reached, with no keep-alive sent and no wake asked; a wake for it is answered with its record, unavailable with that cause, and makes no call to the scaler.

## References

- `docs/design/system-design.md`, sections 5.3, 6.2, 11.3
