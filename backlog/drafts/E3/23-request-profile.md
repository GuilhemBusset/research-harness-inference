+++
id = "E3-23"
title = "Gateway: a pool's request profile, a configured list of named rewrites applied after the body filter"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E2-16", "E2-17", "E2-18", "E2-19", "E2-76"]
+++

## Context

No open model works in Claude Code through vLLM unchanged: the vLLM project replayed real Claude Code
traffic and found a defect for every model family (sections 0 and 6.1; evidence E28). For the
coursework model, three of those defects decide whether a session works, and the gateway handles
them with the pool's **request profile** (section 6.2, step 4): a short per-model table of rewrites
that stay inside one wire format. Each entry names the engine defect it works around, is covered by
a replayed capture, and is deleted when the pinned engine no longer has the defect. The upgrade
model shares the coursework model's template and parsers, so it shares the profile; the optional
xl model would not (section 6.1).

The body filter already gives the profile its place: after the allow-list and the shared rewrites,
before serialization, a per-pool rewrite step that changes nothing for a pool without a profile
(issue E2-16), on a body whose allow-list keeps everything the profile rewrites (issues E2-17 to
E2-19). This issue is the mechanism and its configuration; replaying a capture through a profile is
issue E3-43, each entry is its own issue (E3-24 to E3-26), and the coursework pool's profile is
issue E3-27.

One entry, the reasoning-effort mapping, is configuration as well as code, because the mapping is
also a lever on speed (sections 4.7 and 6.2), so the pool's configuration carries it beside the list
of entries.

**Configuration.** Both fields are optional: a pool without them has an empty profile and no
mapping, so every pool deployed before this issue forwards as it did. The design gives no default
beyond that. They reach the gateway with the rest of each pool's configuration, through the settings
its entry point reads (issue E2-76); the gateway builds every configured pool's profile when it
starts, so a profile that names an entry the gateway does not have stops the start instead of
failing a student's request.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives a pool an optional request profile, the names of its entries in the order they apply, and an optional reasoning-effort mapping from each value a request may send to the value the engine receives; a pool without them has an empty profile and no mapping.
- [ ] `config.example.toml` documents the two fields and their defaults in a comment.
- [ ] `services/gateway/rhi_gateway/profile/__init__.py` applies to a filtered body the entries its pool's profile names, in that order, and finds each entry by its name among the modules of its own package.
- [ ] `services/gateway/rhi_gateway/body.py` runs the pool's profile in its per-pool rewrite step, after the shared rewrites and before serialization, for every forwarded route.
- [ ] Each entry declares its name, the wire formats and routes it applies to, and the public address of the engine defect it works around; an entry rewrites inside the request's wire format and never changes its pool or its format.
- [ ] The function the gateway's entry point calls at start builds each configured pool's profile and stops the start, naming the pool and the entry, when a profile names an entry the gateway does not have.
- [ ] `tests/unit/gateway/test_profile.py` and `tests/unit/common/test_pool_config.py` cover the order, the empty profile, the fields and the refusal.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_profile.py` passes: two entries defined in the test apply in the order the pool's profile names them, a pool with an empty profile forwards its filtered body unchanged, and a profile that names an unknown entry stops the gateway's start.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a pool's profile and its effort mapping parse as written, and a pool without them has an empty profile and no mapping.

## References

- `docs/design/system-design.md`, sections 0, 4.2, 4.7, 6.1, 6.2, 13
- `docs/design/evidence.md`, E28
