+++
id = "E7-25"
title = "Submit typed evaluation runs to an operator-owned local driver"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-24"]
+++

## Context

The coding agent can request a bounded run. The credentials belong to the long-lived operator-side process. The terminal and socket share the same controller.

## Deliverables

- [ ] `tools/evaluate/driver.py` and `tools/evaluate/cli.py` provide the operator-started driver, owner-only local socket and typed submission, inspection and cancellation client.
- [ ] The driver validates explicit pool, host count, time bounds and experiment destination at admission, returns run ID, issue link when confirmed and status, and continues after the submitting client exits.
- [ ] The socket protocol isolates credentials and private booking state inside the driver and permits recovery through the journal after driver restart.
- [ ] `tools/evaluate/tests/test_driver.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_driver.py` uses a local Unix socket and fake services to submit a run, disconnect the client, reconnect for status and see one completed run on the original issue.
- [ ] `tools/evaluate/tests/test_driver.py` verifies restricted socket access and rejects generic commands, unknown fields, an invalid booking bound and credential-bearing payloads before dispatch.

## References

- `docs/design/agent-session-evaluation.md`, How the operator runs it
