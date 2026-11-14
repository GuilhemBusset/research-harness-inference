+++
id = "E1-03"
title = "Invariant: nothing in the template accepts traffic from the internet"
labels = ["size:S", "kind:test"]
epic = "E1"
blocked_by = ["E1-02", "E0-01"]
+++

## Context

The fleet must never be reachable from the public internet. The rule is a test over the
synthesized template, so that a later issue cannot break it quietly.

## Deliverables

- [ ] `tests/policy/test_no_public_ingress.py` reads the synthesized template and fails on any security group rule open to every address.

## Acceptance criteria

- [ ] `uv run pytest tests/policy/test_no_public_ingress.py` passes on the empty stack.
- [ ] The same test fails when a rule open to every IPv4 or IPv6 address is added to `infra/stack.py`: `tests/policy/test_no_public_ingress.py::test_detects_open_rule` proves it on a template built in the test.

## References

- `docs/design/system-design.md`, section 6
- `infra/stack.py`
