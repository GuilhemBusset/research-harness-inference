+++
id = "E7-33"
title = "Keep the confirmed cleanup status and survive an unreadable booking journal"
labels = ["size:S", "kind:app"]
epic = 744
+++

## Context

Two faults in `tools/evaluate/controller.py`. `settle_bookings` and `reconcile` load each booking journal outside any error handler, so an unreadable `booking-0.json` after a driver death raises out of the cleanup: the command exits 2 and the terminal comment is never posted although the run journal and GitHub are available. `release` writes the status field `cleanup` from the invocation's own value, which starts as `not-owned`, so resuming a run that finished with `released` takes no booking action and still shows `not-owned` to the operator.

## Deliverables

- [ ] `tools/evaluate/controller.py` reports cleanup as unconfirmed when a booking journal cannot be read, and still posts the failed-run comment on the confirmed issue.
- [ ] `tools/evaluate/controller.py` keeps the status of a resume that takes no booking action at the cleanup outcome the run recorded.
- [ ] `tools/evaluate/tests/test_controller.py` holds a test for each of the two cases.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_controller.py` shows an unreadable booking journal after a driver death gives cleanup `unconfirmed` and a failed-run comment, not an uncaught error.
- [ ] `tools/evaluate/tests/test_controller.py` shows resuming a finished run recorded as `released` leaves its status at `released`.
- [ ] `tools/evaluate/tests/test_controller.py` shows a booking the run holds is still released conditionally when its journal reads.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_controller.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
