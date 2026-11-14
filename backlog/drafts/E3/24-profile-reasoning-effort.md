+++
id = "E3-24"
title = "Request profile entry: map reasoning effort to a value the model's template accepts"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E3-23", "E3-43"]
+++

## Context

Claude Code asks for reasoning effort `high` by default. The coursework model's chat template accepts
`xhigh`, `medium` and `low` and raises an error on anything else, so every default Claude Code request
is refused (section 6.1; evidence E28, from https://github.com/vllm-project/vllm/issues/58647). The
profile's first entry maps the effort to a value the template accepts (section 6.2, step 4): `low`
and `medium` pass, and `high`, `xhigh` and `max` become `xhigh`, the model's default and the setting
its published results were measured at. The mapping is the pool's configuration (issue E3-23),
because it is also a lever on speed (section 4.7); section 17 gives that mapping as the coursework
pool's default, which issue E3-27 sets.

vLLM 0.33 has a flag for this mapping; the gateway does it instead, so that a unit test proves it
without a GPU. The template is the same whatever the route, so the entry applies to the three wire
formats, each in the field its allow-list keeps for the effort (issues E2-17 to E2-19). The design
says nothing of a value outside the mapping; the entry leaves it as sent.

## Deliverables

- [ ] `services/gateway/rhi_gateway/profile/reasoning_effort.py` defines the entry reasoning-effort: in a Messages, Chat Completions or Responses request, it replaces the reasoning effort the request sets by the value the pool's mapping gives for it, in that format's own field for the effort.
- [ ] A request that sets no effort, or a value the mapping does not name, is left as sent.
- [ ] The entry names its defect, https://github.com/vllm-project/vllm/issues/58647, and applies to the three generation routes.
- [ ] `tests/captures/profile-reasoning-effort/` holds a capture, built under the capture rules, of a Messages request shaped like Claude Code's with effort high, stating as its profile this entry with the mapping of section 17, holding no student key, whose engine-side body carries xhigh.
- [ ] `tests/unit/gateway/test_profile_effort.py` covers the mapping in each format.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_profile_effort.py` passes: with the mapping of section 17, low and medium reach the engine unchanged and high, xhigh and max reach it as xhigh, in each of the three formats, and a request without an effort reaches it without one.
- [ ] `uv run --locked pytest tests/replay/test_replay.py` passes with the capture of `tests/captures/profile-reasoning-effort/`.

## References

- `docs/design/system-design.md`, sections 4.7, 6.1, 6.2, 17
- `docs/design/evidence.md`, E28
