+++
id = "E3-28"
title = "Student affinity: a pool's three settings, and its rebalance number in the scaler's note"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E2-03", "E2-04"]
+++

## Context

An agent re-sends its whole transcript on every turn, and a turn that reaches the instance that
served the previous one pays only for its new tokens; on any other instance it pays for the whole
transcript again (section 5.5). So a student's turns stay on one instance: each pool's target group
has duration-based stickiness, the gateway keeps each student's cookie, and the scaler raises a
rebalance number in the pool's record while the pool is lopsided, which the gateway reads and acts on
by moving about one student in ten (section 5.5).

Three of these numbers are configuration, with the defaults of section 17: the stickiness on a
pool's target group, 3600 seconds, renewed by every request; the students moved by one rebalance
number, about one in ten; and the least time between two numbers, 60 seconds. Every pool has them,
so a pool's entry may leave them out, and the deployed configuration needs no new value. The
stickiness reaches the target group when the app synthesizes (issue E3-29); the share reaches the
gateway with the rest of each pool's configuration, through its entry point's settings (issue
E2-76); the least time reaches the scaler, whose function receives every pool's configuration as the
stage loaded it (issue E2-12). The example configuration's pools leave them out, and it documents
them in a comment.

The rebalance number lives in the pool's record that the gateway already reads (section 5.5). The
scaler writes it, so it belongs in the scaler's note on the pool (issue E2-03), beside what the
scaler asked for, and not in the pool's mode, which only the admin changes. The gateway reads that
note for every pool every 5 seconds (issue E2-73). This issue is the shape; the stickiness is issue
E3-29, the gateway's side issues E3-30 and E3-32, and the scaler's issue E3-31.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives every pool, with the defaults of section 17: the stickiness duration of its target group, 3600 seconds; the share of students one rebalance number moves, one in ten; and the least time between two rebalance numbers, 60 seconds.
- [ ] It refuses a stickiness duration outside the 1 second to 7 days the load balancer accepts, a share outside 0 to 1, and a least time below 1 second, naming the pool and the field.
- [ ] `config.example.toml` documents the three fields and their defaults in a comment.
- [ ] `services/common/rhi_common/records.py` adds to the scaler's note on a pool its rebalance number, a whole number that is 0 until the scaler first raises it, and a note stored without one reads as 0.
- [ ] `tests/unit/common/test_pool_config.py` and `tests/unit/common/test_records.py` cover the settings and the number.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a pool that leaves the three settings out reads 3600 seconds, one in ten and 60 seconds, and a share of 1.5 is refused.
- [ ] `uv run --locked pytest tests/unit/common/test_records.py` passes: a note with a rebalance number survives a round trip through its item form, and an item without one reads as 0.

## References

- `docs/design/system-design.md`, sections 5.1, 5.5, 17
- `docs/design/evidence.md`, E13
