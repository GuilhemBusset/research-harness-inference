+++
id = "E7-21"
title = "Persist experiment issues, terminal scorecards and comparison comments"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-19", "E7-22"]
+++

## Context

Experiment issues are evidence records in the repository. Creation, delivery and recovery use the driver’s GitHub identity. Only an explicit driver request launches inference.

## Deliverables

- [ ] `tools/evaluate/publisher.py` creates or selects one experiment issue, confirms its identity before booking, and queues one terminal comment per run and a final comment per paired comparison in the journal.
- [ ] The publisher renders the design’s public issue and comment fields, fixed reasons for partial runs, completed counts, cleanup status, bundle digest and optional reviewed permalink, and scans the exact rendered bytes before each write.
- [ ] It reconciles stable run or comparison markers against the writer’s own records after ambiguous responses and implements publish-only resume from the outbox without loading cloud or inference clients.
- [ ] `tools/evaluate/tests/test_publisher.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_publisher.py` uses a fake GitHub endpoint to simulate issue or comment accepted with the response lost and recovers exactly one record of each kind even when a different author posts the same marker.
- [ ] `tools/evaluate/tests/test_publisher.py` proves a rejected public field or failed hygiene scan prevents delivery and a post-run outage retains a retryable comment including measured misses and cleanup outcome.
- [ ] `tools/evaluate/tests/test_publisher.py` verifies a candidate and a reference comment plus their comparison attach to the same selected experiment and publish-only recovery sends no benchmark traffic.

## References

- `docs/design/agent-session-evaluation.md`, Where results persist
- `tools/hygiene/scan.py`
