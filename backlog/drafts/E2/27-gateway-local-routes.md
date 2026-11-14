+++
id = "E2-27"
title = "Gateway: the models, status and hello routes, answered locally"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-14", "E2-25", "E2-73"]
+++

## Context

Four routes are answered by the gateway itself (section 6.2, step 1): `GET /v1/models`,
`GET /status`, `POST /wake` and `HEAD /api/hello`. This issue answers the first, second and fourth;
the wake is issue E2-78. Cheap calls never wake a GPU, and status calls are not demand (sections 5.1
and 5.3). Token counting, the other cheap call, is a forwarded route and is handled in the hold path
(issue E2-26).

`GET /status`, with a key, lists the pools, each with a state and, when unavailable, a cause from
the list in section 6.2 (section 5.3). The design gives every student every configured pool: there
is no per-key pool list, so the routes list every configured pool. Pools are named by their model
name. `rhi status` and `rhi wait` read this route (issue E2-55).

The four states are the design's (section 5.3). A pool is `ready` when it has a healthy target;
`warming` when it has none but the scaler has asked for capacity; `cold` when it has none and none
is asked for; `unavailable` when it cannot be started for this caller: `pool_disabled`,
`pool_ceiling_reached`, `pool_scheduled_only`, or `capacity_unavailable` when its last launch found
no capacity in the past 10 minutes. The healthy target, the pool's mode and the scaler's note come
from the values issue E2-73 keeps; the three immediate causes are those of the hold's rule (issue
E2-25), so that the status never says a pool can start when a request for it would be refused.

`GET /v1/models` without a key is also the public load balancer's health check: its 401 needs no
lookup and no route outside the table (issue E2-90).

## Deliverables

- [ ] `services/gateway/rhi_gateway/local.py` answers `GET /v1/models`, for a valid key only, with the model names of every configured pool, and says which is the default pool, the one that model names starting with claude- map to.
- [ ] It answers `GET /status`, for a valid key only, with one record per configured pool: the pool's model name, its state, and, when unavailable, its cause; a pool is ready when it has a healthy target, warming when it has none but the scaler's note says capacity was asked for, and cold when it has none and none is asked for.
- [ ] A pool is unavailable with the cause pool_disabled, pool_ceiling_reached or pool_scheduled_only exactly when the rule of issue E2-25 ends a request for it at once with that cause, and with the cause capacity_unavailable when it has no healthy target and the scaler's note says its last launch found no capacity in the past 10 minutes.
- [ ] It answers `HEAD /api/hello` with 200 and no body, without a key; the models, status and hello routes ask for no wake and reserve no budget.
- [ ] `services/gateway/rhi_gateway/app.py` serves the three routes from this module, and `tests/unit/gateway/test_local.py` covers them.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_local.py` passes: the three routes answer as stated, `GET /v1/models` and `GET /status` without a key are refused with a 401 and the cause invalid_key with no lookup, and no route makes a wake call or a reservation.
- [ ] `tests/unit/gateway/test_local.py::test_the_four_states` shows a pool with a healthy target as ready, one with none and capacity asked for as warming, one with none and none asked for as cold, and a disabled, a budget-latched and an unstarted scheduled-only pool as unavailable with the cause the hold's rule gives each.
- [ ] `tests/unit/gateway/test_local.py::test_no_capacity_reads_unavailable_for_10_minutes` shows a pool with no healthy target whose last launch found no capacity 9 minutes ago as unavailable with the cause capacity_unavailable, and the same pool at 11 minutes as cold or warming.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 6.2
