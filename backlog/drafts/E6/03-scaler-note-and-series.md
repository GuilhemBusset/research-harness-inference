+++
id = "E6-03"
title = "Scaler: what each pool was asked, what waits and why, in its note and its series"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-02"]
+++

## Context

While a pool has fewer instances than its session asks, the instructor, the gateway and the
dashboard need the same five numbers and one cause (system design, section 5.4): the instances
requested, asked of AWS, waiting for capacity and since when, starting, and in service; and why the
group's last launch failed. The scaler is the one component that reads both the session and the
group, so it writes them: in its note, which the admin CLI, the gateway and the spikes read, and as
series, which the fleet dashboard reads (section 7.2).

The words are fixed. "Asked of AWS" is the note's existing `capacity_asked`. "Waiting" is what was
asked and has no instance yet. The cause is one of four words, never text from AWS:
`insufficient-capacity` (a launch that found no capacity, which `rhi_scaler.aws_io` already tells
from the group's activities), `quota` (a launch the account's vCPU quota refused),
`launches-suspended` (the scaler's own mark, after three abandoned launches) and `launch-failed`
for any other failed launch.

A reader that has not been updated must still read a note with more attributes, and this issue's
reader a note without them. The pool's memory item already holds an attribute `waiting_since`, a
map of instances that the rules on load use until E6-15: the time since which a pool waits for
capacity is kept under another name.

After this issue the deployed system sizes its pools as before; the note and the workspace hold
more.

## Deliverables

- [ ] `services/common/rhi_common/records.py`: the scaler's note also holds `requested`, `starting`, `in_service`, `draining`, `waiting`, `waiting_since` and `launch_failure`, and a note stored without them reads as four zeros, no time and no cause.
- [ ] `services/scaler/rhi_scaler/aws_io.py` writes them on every run: `requested` from the decision, the three counts from the supply it read, and `waiting` as the capacity asked less the instances starting and in service, never below zero.
- [ ] `waiting_since` is the time of the first of the runs in a row that saw `waiting` above zero, kept from one run to the next in the pool's memory item under the attribute `short_since`, and absent while nothing waits.
- [ ] `launch_failure` is absent while nothing waits; otherwise it is `launches-suspended` when the pool's launches are suspended, or else the cause of the group's last launch that failed since the wait began: `insufficient-capacity`, `quota` or `launch-failed`.
- [ ] `services/scaler/rhi_scaler/metrics.py` sends, for each pool on every run, the gauges `rhi_scaler_session_active`, `rhi_scaler_instances_requested`, `rhi_scaler_instances_waiting`, `rhi_scaler_waiting_seconds`, `rhi_scaler_instances_starting` and `rhi_scaler_instances_draining`; while the pool's session has not ended, `rhi_scaler_session_starts_at_seconds` and `rhi_scaler_session_ends_at_seconds`; and `rhi_scaler_launch_failure` with the labels pool and cause, one series for each of the four causes, 1 for the cause in force and 0 for the others.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_supply_note.py` passes: with 3 asked, 1 in service and 1 starting, the note reads `waiting` 1 since the first run that saw it, keeps that time on the next run, and drops it once nothing waits.
- [ ] `tests/unit/scaler/test_supply_note.py::test_each_cause` shows each of the four causes from the activity or the mark that gives it, and no cause while nothing waits.
- [ ] `uv run --locked pytest tests/unit/scaler/test_metrics.py tests/unit/common/test_records.py` passes: every series above is sent with the pool's label, the four series of the cause hold one 1 at most, and a note without the new attributes still reads.

## References

- `docs/design/system-design.md`, sections 5.4 and 7.2
- `services/scaler/rhi_scaler/aws_io.py`
- `services/scaler/rhi_scaler/metrics.py`
