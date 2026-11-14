+++
id = "E2-08"
title = "Scaler: a silent gateway, and a launch that fails every time"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-07"]
+++

## Context

Two situations would otherwise cost GPU time with nobody served (sections 5.1 and 5.4). Nobody can
reach the fleet without the gateway, so when no gateway reports, the pool goes to zero. And a launch
that fails every time (a bad image, a wrong prefix, a rotated secret) would loop: launch, wait,
abandon, relaunch. The scaler counts abandoned launches and suspends them after three, then waits
for the admin (section 5.1, launch loop). The alarm that tells the admin is a CloudWatch alarm of
phase 3, and clearing the mark is an admin command of phase 3 (issues E4-09 and E4-03): the mark is
the attribute `launch_suspended` of the pool's memory item (issue E2-67), which the admin command sets
back to false.

The number of launches abandoned in a row is an input of the decision (issue E2-05), counted by the
edge from the group's activity history (issue E2-10); a launch that reaches service ends a row. A
launch that found no capacity is not an abandoned launch. The history still holds the three
abandons after the admin clears the mark, so a run that finds the mark cleared keeps its own time in
the memory as the start of the count, and the edge counts no abandon before it. Going to zero names every instance of the
pool, so that their scale-in protection is lifted (issue E2-05).

## Deliverables

- [ ] `services/scaler/rhi_scaler/decide.py` sets a pool to zero, naming every instance of the pool, when no fresh gateway heartbeat has been seen for 10 minutes, the time of the last one being kept in the memory that `services/scaler/rhi_scaler/model.py` defines.
- [ ] When the input counts three launches abandoned in a row, it asks for the group's launches to be suspended, sets launch_suspended in the pool's memory, and wakes nothing until an admin clears the mark.
- [ ] A run that finds the mark cleared keeps its time in the memory as the start of the count, asks for launches to resume, and wakes the pool again on a held request; only three new abandoned launches suspend it again.
- [ ] `tests/unit/scaler/test_fail_safes.py` covers both rules.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_fail_safes.py` passes: with the gateway silent for 10 minutes the desired capacity is zero even with an instance busy; two abandoned launches change nothing and the third suspends.
- [ ] `tests/unit/scaler/test_fail_safes.py::test_a_suspended_pool_stays_suspended` shows that held requests do not wake a launch-suspended pool until the mark is cleared, and that once it is cleared a held request asks for one instance and the memory holds the new start of the count.

## References

- `docs/design/system-design.md`, sections 5.1, 5.4
