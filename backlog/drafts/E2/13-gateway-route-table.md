+++
id = "E2-13"
title = "Gateway: a closed route table"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-02"]
+++

## Context

The engine's own request surface is too large to hand to forty students unfiltered, and vLLM answers
routes such as `/invocations` and `/pause` without its key (section 4.1; evidence E23). The gateway
routes by a closed table: four routes are forwarded, four are answered locally, everything else is
404 with the cause `not_found`. The upstream URL comes from the table, never from the client's path
(section 6.2, steps 1 and 8).

The table is the design's, unchanged. The public load balancer checks the gateway's health with
`GET /v1/models` without a key and counts the 401 as healthy (issue E2-90), so that no route is added
for it.

Starlette is pinned by issue E1-16, which this issue waits for.

## Deliverables

- [ ] `services/gateway/rhi_gateway/routes.py` holds the table: forwarded are `POST /v1/messages`, `POST /v1/messages/count_tokens`, `POST /v1/chat/completions` and `POST /v1/responses`; answered locally are `GET /v1/models`, `GET /status`, `POST /wake` and `HEAD /api/hello`.
- [ ] Any other method or path is a 404 with the cause not_found of issue E2-02, whatever its query string, trailing slash, letter case or encoding.
- [ ] The upstream path of a forwarded route is the table's own constant; nothing of the client's path or query is copied into the upstream URL.
- [ ] `services/gateway/rhi_gateway/app.py` builds the Starlette application from the table, with a handler slot per route that later issues fill.
- [ ] `tests/unit/gateway/test_routes.py` covers the table and its refusals.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_routes.py` passes: the eight entries answer, and `/invocations`, `/pause`, `/tokenize`, `/metrics`, `/health`, `/v1/completions` and `/v1/messages/` with an encoded slash are all 404 with the cause not_found.
- [ ] `tests/unit/gateway/test_routes.py::test_the_upstream_url_is_the_tables` sends paths with dot segments, encoded characters and query strings and finds only the table's constants in the upstream URLs.
- [ ] The owner confirms a custom gateway over LiteLLM, decision 7 of the design; the gateway issues of this epic are written for the design's choice (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.1, 6.2, 6.3, 15
- `docs/design/evidence.md`, E23
