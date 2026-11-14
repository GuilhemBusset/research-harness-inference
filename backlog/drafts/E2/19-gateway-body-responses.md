+++
id = "E2-19"
title = "Gateway: the body allow-list for OpenAI Responses"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-16"]
+++

## Context

Codex CLI accepts only the Responses format; Chat Completions has been a configuration error there
since February 2026 (section 6.1; evidence E15). Codex sends structured input items, tools and
reasoning options, and returns the model's earlier reasoning among its input items.

The request profile of phase 2 runs after this filter and maps reasoning effort to a value the
model's template accepts (section 6.2, step 4). So the list keeps the format's reasoning-effort
field, the effort of the `reasoning` object in OpenAI's reference, whatever its value, and it keeps
system and developer messages wherever they sit among the input items.

## Deliverables

- [ ] `services/gateway/rhi_gateway/formats/responses.py` lists the standard fields of a Responses request, at the top level and inside input items and tools, each with the place in OpenAI's reference it comes from.
- [ ] The list keeps the reasoning-effort field as sent, whatever its value, the reasoning items of earlier turns, system and developer messages wherever they sit among the input items, and images with inline data wherever the reference allows one.
- [ ] `tests/unit/gateway/test_body_responses.py` covers a request shaped like Codex's.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_body_responses.py` passes: a request with input items, function tools and a reasoning option passes unchanged in those fields, and unknown top-level and nested fields are gone.
- [ ] `tests/unit/gateway/test_body_responses.py::test_what_the_profile_rewrites_is_kept` finds reasoning effort high, a reasoning item of an earlier turn, and a developer message between two user messages forwarded unchanged and in place.

## References

- `docs/design/system-design.md`, sections 6.1, 6.2
- `docs/design/evidence.md`, E15
