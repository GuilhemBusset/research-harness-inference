+++
id = "E2-16"
title = "Gateway: parse a body strictly and rewrite what every format shares"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-02", "E2-04"]
+++

## Context

vLLM states that it gives no isolation between tenants sharing a server, and many of its 2026
advisories are a single request field that stalls the engine for everyone (section 6.2, step 4;
evidence E23). The gateway therefore never forwards a client's bytes: it parses the body, keeps what
an allow-list names, rewrites a few fields, and sends its own serialization. This issue is the part
the three wire formats share; each format's allow-list is its own issue. A body the filter rejects
is a 400 with the cause `request_refused` (section 6.2, step 8), and the largest body it reads is 8
MB by default (section 17). The limits it clamps to are the pool's own: its context length and its
largest output (section 17; issue E2-04).

Section 6.2, step 4, gives the order: parse, keep the allow-list, clamp, refuse media that is not
inline data, set the per-student cache salt, replace the request ID, map `claude-*` model names to
the default pool, apply the pool's **request profile**, then serialize. The request profile is a
per-model table of rewrites inside one wire format (reasoning effort, system messages placed
mid-conversation, document blocks); it is built in phase 2, by an issue of epic E3, and it runs on
what this filter keeps. So the filter must keep what the profile rewrites, never refuse it, and
leave the profile a place to run before the body is serialized. The cache salt is per student so
that one student's cached prefixes cannot be probed by another (section 6.6); vLLM's API names the
request field that carries it.

The deployed configuration may have no default pool (issue E2-69), and the filter is then given none:
a claude- name maps to no pool and is refused like any name of no pool.

Students choose a pool by model name (section 4.2). The engine serves the pool's model under that
name, so the name the gateway forwards is the pool's, whatever name the client sent.

The maximum body size is a parameter of the filter, 8 MB by default (section 17), read as 8,388,608
bytes. The configuration key `request_body_limit_bytes` and the environment variable that carry it
to the running gateway are issue E2-76's; this issue reads neither.

## Deliverables

- [ ] `services/gateway/rhi_gateway/body.py` parses a JSON body and refuses one with a duplicate key at any depth, one that is not UTF-8, and one above the maximum size it is given, 8,388,608 bytes by default, without reading past that size; every refusal of the filter ends the request with the cause request_refused of issue E2-02.
- [ ] It keeps the fields an allow-list names and drops every other field, at each level the allow-list describes.
- [ ] It maps the model name to a pool: a configured pool's model name is kept, a name that starts with claude- becomes the default pool's when one is given and is refused when none is, any other name is refused, and the forwarded body names the pool's model name.
- [ ] It clamps numeric fields to the pool's own limits, its context length and its largest output, replaces the request ID by the gateway's own, and sets a cache salt derived from the student in the request field vLLM's API reference names for it.
- [ ] It refuses a media reference, an image or a document, that is not inline data, and keeps one that is.
- [ ] After the shared rewrites and before serialization, the filtered body passes through a per-pool rewrite step that changes nothing for a pool without a request profile, which every pool is until phase 2.
- [ ] What is forwarded is always the gateway's own serialization of the result; `tests/unit/gateway/test_body.py` covers parsing and the shared rewrites.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_body.py` passes: a duplicate key at depth three is refused with the cause request_refused, a body above 8 MB is refused after the maximum has been read and no more, and an unlisted field is absent from the forwarded body.
- [ ] `tests/unit/gateway/test_body.py::test_shared_rewrites` shows a claude- model name forwarded as the default pool's model name, and refused with the cause request_refused when no default pool is given, an output limit clamped to the pool's largest output, two students given different cache salts, and an image and a document given by URL refused while the same image and document inline pass.
- [ ] `tests/unit/gateway/test_body.py::test_the_profile_step_sees_the_filtered_body` registers a rewrite for one pool in the test and finds it applied to that pool's filtered body, after the shared rewrites, and no change for another pool.

## References

- `docs/design/system-design.md`, sections 4.2, 6.1, 6.2, 6.6, 17
- `docs/design/evidence.md`, E23
