+++
id = "E2-10"
title = "Scaler: read supply and demand from AWS, and apply a decision"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-08", "E2-67"]
+++

## Context

The decision function is pure. This issue is its edge: what a run reads from Auto Scaling, from
target health and from DynamoDB, and how a decision becomes calls (section 5.1, the inputs table).
`SetDesiredCapacity` is absolute, draws from the warm pool and is safe to call twice (evidence E10),
which is what makes one writer enough.

An abandoned launch is an instance that started and that its launch hook gave up on (a bad image, a
wrong prefix, a rotated secret); three in a row suspend the group's launches (section 5.4). A launch
that found no capacity is something else: no instance started, the gateway answers
`capacity_unavailable`, and a wake still reaches the scaler, which tries again (section 5.3). It
must not count towards the three. A launch that reaches service ends a row, and so does the admin's
clearing of the mark: the memory keeps the time a run found it cleared (issue E2-08), and no abandon
before that time counts. Suspending launches is suspending the group's `Launch` process; zone
rebalancing, `AZRebalance`, stays suspended for good (issue E2-71), so a resume names `Launch` alone.

New instances are protected from scale-in (section 4.3). Whether an instance that returns from the
warm pool to service keeps the protection it had when it left is not something the design settles,
and an unprotected instance in service could be chosen by AWS in place of the one the scaler picked.
So a run protects every instance in service that it does not release.

Where things are kept, by the schema of issue E2-67: the heartbeats in the heartbeat table, by pool;
the admin's mode, the scaler's note and the scaler's memory in the pool-state table, as the records
mode, note and memory of each pool. A pool without a mode record is in its configured mode (issue
E4-03). The table names and the pools come from the scaler's settings (issue E2-11).

## Deliverables

- [ ] `services/scaler/rhi_scaler/aws_io.py` reads, for each pool: the group's desired capacity and its instances by lifecycle state with their scale-in protection, warm pool included; target health; the fresh heartbeats of the heartbeat table; the mode record, or the configured mode when there is none; and the memory record of the pool-state table.
- [ ] It counts the launches abandoned in a row from the group's own activity history, since the later of the last launch that reached service and the start of the count the memory gives, into the input issue E2-05 defines; a launch that failed for lack of capacity does not count.
- [ ] It applies a decision in this order: protect from scale-in every instance in service that is unprotected and not named; lift the protection of each instance the decision names; set the desired capacity; suspend the Launch process when the decision asks for it and resume the Launch process alone when it no longer does.
- [ ] An Auto Scaling call is made only for what differs: a decision equal to the current state makes no Auto Scaling call.
- [ ] On every run it writes, for each pool, the scaler's note of issue E2-03 (the capacity it asked for, and the time the last launch failed for lack of capacity) and the memory record the decision returns.
- [ ] `tests/unit/scaler/test_aws_io.py` covers reads and writes against stubbed clients.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_aws_io.py` passes: a recorded description of a group with a warm pool becomes the expected supply, a decision becomes the expected calls in the stated order, and the note carries the capacity asked for and the time of a recorded launch that found no capacity.
- [ ] `tests/unit/scaler/test_aws_io.py::test_no_change_no_call` shows that an unchanged decision makes no Auto Scaling call, while the note and the memory are still written.
- [ ] `tests/unit/scaler/test_aws_io.py::test_what_counts_as_abandoned` counts three abandoned launches in a recorded activity history, none for three launches that failed for lack of capacity, and none before the start of the count the memory gives.
- [ ] `tests/unit/scaler/test_aws_io.py::test_protection_and_resume` shows an unprotected instance in service protected again, and a resume that names the Launch process and never AZRebalance.

## References

- `docs/design/system-design.md`, sections 4.3, 5.1, 5.3, 5.4
- `docs/design/evidence.md`, E10
