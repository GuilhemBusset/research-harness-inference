+++
id = "E3-37"
title = "A pool may give a whole class one cache salt; the default stays one per student"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E2-04", "E2-16"]
+++

## Context

The gateway sets a cache salt per student (section 6.2, step 4; issue E2-16), so that a prefix the
engine cached for one student is reused only by that student, and one student cannot probe whether
another has sent a given text (section 6.6). Section 4.7 lists a lever for scheduled sessions: with
one salt for the class, students share whatever part of Claude Code's 20,000-token opening is
identical, and a class starting together does not queue behind its own openings. The price is the
probe of section 6.6. The design keeps the default per student; spike S5 measures the lever
(issue E3-19).

So a pool may choose the salt's scope: per student, the default, or one for the class. The class
salt is a fixed value derived from the pool's name, so that it is the same on every gateway task and
reveals nothing about any student.

The field, cache_salt_scope, is optional with the default student, so every pool deployed before this
issue keeps its salt and the deployed configuration needs no new value. It reaches the gateway with
the rest of each pool's configuration, through its entry point's settings (issue E2-76). The example
configuration's pools leave it out, and it documents the field in a comment.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives every pool the optional field cache_salt_scope, student or class, student by default, and refuses any other value, naming the pool and the field.
- [ ] `config.example.toml` documents the field and its default in a comment.
- [ ] `services/gateway/rhi_gateway/body.py` sets the salt per student for a pool whose scope is student, and to one value derived from the pool's name alone for a pool whose scope is class.
- [ ] `tests/unit/gateway/test_body.py` and `tests/unit/common/test_pool_config.py` cover both scopes.

## Acceptance criteria

- [ ] `tests/unit/gateway/test_body.py::test_the_salt_scope` shows two students given different salts on a pool that leaves the scope out, the same salt on a pool whose scope is class, and that salt holding neither student's identity.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a pool without the field reads student, and a scope of team is refused.

## References

- `docs/design/system-design.md`, sections 4.7, 6.2, 6.6
