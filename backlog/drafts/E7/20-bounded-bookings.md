+++
id = "E7-20"
title = "Book, wait for and conditionally release evaluation capacity"
labels = ["size:M", "kind:app"]
epic = "E7"
+++

## Context

Use the existing session controller on an already-deployed pool. Only the operator-side driver has cloud access. The deployed scaler owns host provisioning and release.

## Deliverables

- [ ] `tools/evaluate/booking.py` handles managed and designated existing-session modes with deployed limits, an exact booking record, a unique writer marker, a scaler pass after owned writes and bounded readiness polling.
- [ ] It requires the selected host count in service, gateway readiness, release match and enough remaining time for the batch and cleanup, and exposes a fixed reason for each refusal.
- [ ] It reconciles ambiguous writes and releases only an unchanged booking created for that invocation, including on cancellation, late capacity and restart.
- [ ] `tools/evaluate/tests/test_booking.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_booking.py` uses a fake clock and table to prove no takeover after a lost booking race or operator edit, and no stop or extension of a designated existing session.
- [ ] `tools/evaluate/tests/test_booking.py` checks one-of-two ready hosts, stale supply or wrong release, unexpected capacity delay, write-success/response-loss and owned-stop retry against the saved writer marker and deadline.

## References

- `docs/design/agent-session-evaluation.md`, Booking and running
- `scripts/pipeline/booking.py`
- `admin/rhi_admin/session.py`
