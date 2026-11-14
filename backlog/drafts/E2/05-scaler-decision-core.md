+++
id = "E2-05"
title = "The scaler's decision function: wake, stop and scale to zero"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-03", "E2-04"]
+++

## Context

One scaler is the only writer of a pool's size (section 5.1). Its decision is a pure function of its
inputs, so every rule is tested without AWS. This issue defines the inputs and the decision, and the
three rules that take a pool from zero and back. The rules for more than one instance and the
fail-safes are the next issues, in the same module.

Demand is what the gateway's heartbeats report. Status calls and token counting are not demand. An
explicit wake is: it reaches the scaler as a held request until the pool is ready, and as a request
received at that moment, both through the heartbeat of issue E2-03, so the decision needs no rule of
its own for it. A pool the admin stopped is in mode `disabled`; `parked` is not a mode (section
11.3).

A gateway task that is replaced, by a deployment or a failure, starts a new row with no request in
it, and the old row stops being fresh after 20 seconds. The time of the last request received is
therefore kept in the scaler's memory as well, or a gateway deployment would look like an idle pool.
The memory is stored between runs in the pool-state table (issues E2-10 and E2-67).

New instances are protected from scale-in, and the scaler lifts that protection on the instances it
chooses (section 4.3). AWS lowers the desired capacity of a group whose instances are all protected
but terminates none of them, so a decision names every instance whose protection it lifts: one for a
scale-in, all of them when a pool goes to zero.

A stopped pool scales in "as instances fall idle" (section 5.1, stop): an instance of a pool in mode
`disabled` or `budget-latched` that reports nothing running and nothing waiting is released at the
run that sees it so, without waiting for `idle_minutes`, which is the rule for a pool nobody asked to
stop.

The decision also needs the number of launches abandoned in a row, which the edge counts from the
group's activity history (issue E2-10) and the launch-loop rule reads (issue E2-08). This issue
defines that input, so that the two issues meet in this module.

The other rules of section 5.1 come later and extend the same inputs and decision: the ceilings and
the spend (issues E4-01 and E4-02), the schedule and its pre-warm (issue E4-05), and the rebalance
transition of phase 2, which changes no size (section 5.5).

## Deliverables

- [ ] `services/scaler/rhi_scaler/model.py` defines the inputs of a run (per pool: its configuration and mode, the fresh gateway heartbeats, the instance heartbeats, the supply as desired, in service, starting, draining, warm and every running instance, the number of launches abandoned in a row, the memory of earlier runs, and the time) and the decision (per pool: the desired capacity, the instances whose scale-in protection to lift, whether to suspend launches, the new memory, a reason).
- [ ] `services/scaler/rhi_scaler/decide.py` is a function of those inputs alone: it calls nothing and reads no clock.
- [ ] From 0 to 1: any held request for a pool in mode enabled asks for one instance, however many requests are held.
- [ ] To 0: a pool with no request received and none in flight for idle_minutes goes to zero and the decision names every instance of the pool, judged on the inputs of the run that acts: the last request received is the latest such time among the fresh gateway heartbeats and the memory, and the new memory keeps it.
- [ ] Stop: a pool in mode disabled or budget-latched is never woken, and each run lowers its desired capacity by, and names, every instance that reports nothing running and nothing waiting, without waiting for idle_minutes; held requests do not wake a pool in mode scheduled-only.
- [ ] Every decision carries a reason from an enumeration defined in the module.
- [ ] `tests/unit/scaler/test_decide.py` covers the three rules.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_decide.py` passes: twenty held requests ask for exactly one instance; a pool whose last request was received 19 minutes ago keeps its instance and at 20 minutes goes to zero naming both of its two instances; a request in flight keeps it.
- [ ] `tests/unit/scaler/test_decide.py::test_a_replaced_gateway_task_is_not_idleness` shows that a pool whose last request, received 5 minutes ago, is held only in the memory, every fresh heartbeat reporting none, keeps its instance.
- [ ] `tests/unit/scaler/test_decide.py::test_stopped_pools_are_not_woken` shows that held requests wake neither a disabled, nor a budget-latched, nor a scheduled-only pool, and that a disabled pool with one busy and one idle instance goes to 1 at once, naming the idle one.

## References

- `docs/design/system-design.md`, sections 5.1, 11.3
