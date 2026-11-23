+++
id = "E0-35"
title = "Reporter: an instance in service is named at its stage serving, not counted as dropped"
labels = ["size:S", "kind:infra"]
epic = 2
+++

## Context

When a verification fails, the reporter names the last lifecycle stage each instance reached,
read from the heartbeat rows, and only by a name of its allow-list of stages (#144). The
allow-list is built at synthesis from the members of the lifecycle agent's enumeration of the
stages of a start, `services/engine/lifecycle/rhi_lifecycle/stages.py` (`lifecycle_stages()` in
`src/rhi/pipeline/reporter.py`). An instance in service writes its heartbeat with the stage
serving instead (`services/engine/lifecycle/rhi_lifecycle/serving.py`, its constant STAGE),
which is not a member of that enumeration.

So the reporter drops every row of an instance in service and counts it: the failure report
cannot say that an instance was serving, which is the most telling fact about a failed
verification (the start went through), and the public status `verify/<stage>/dropped` carries
the count, which reads as dropped tests. Two failed verifications on 2026-11-22 carried
"dropped 1" and "dropped 3", each for heartbeat rows of instances in service.

## Deliverables

- [ ] `src/rhi/pipeline/reporter.py` gives the reporter's function an allow-list of lifecycle stages that holds every stage of a start and the stage serving, which an instance in service names in its heartbeat, taken from `services/engine/lifecycle/rhi_lifecycle/serving.py` and not written out again.
- [ ] A failure report built from a heartbeat row whose stage is serving names that stage for its instance and counts nothing as dropped for it, in `services/reporter/rhi_reporter/report.py` or the handler as they stand.

## Acceptance criteria

- [ ] `tests/synth/test_reporter.py` holds the function's allow-list of lifecycle stages to the stages of a start and the stage serving.
- [ ] `tests/unit/reporter/test_handler.py` holds that a heartbeat row whose stage is serving is named in the report and not counted as dropped.
- [ ] `uv run --locked pytest tests/synth/test_reporter.py tests/unit/reporter` passes.

## References

- `src/rhi/pipeline/reporter.py`
- `services/reporter/rhi_reporter/report.py`
- `services/engine/lifecycle/rhi_lifecycle/serving.py`
- `docs/design/system-design.md`, section 8.4
