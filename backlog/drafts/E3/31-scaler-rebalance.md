+++
id = "E3-31"
title = "Scaler: raise a pool's rebalance number while one instance queues and another has room"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E2-06", "E2-08", "E2-10", "E3-28"]
+++

## Context

Stickiness alone would leave a new instance empty, and could leave one instance queuing while another
idles (section 5.5). So the scaler keeps a rebalance number in the pool's record and raises it while
a pool is lopsided; for each new number the gateway moves about one student in ten to the emptiest
instance (issue E3-32). The rule is a transition of section 5.1:

> rebalance: An instance reporting waiting requests for 60 seconds while another runs below half of
> `target_concurrency`, which is also the state of a pool that has just grown: the scaler raises the
> pool's rebalance number, which moves a few students to the emptier instance (section 5.5). At most
> once a minute, and only while the imbalance lasts. The size does not change.

The least time between two numbers is the pool's setting, 60 seconds by default (section 17; issue
E3-28). Moving a few students at a time matters: each move costs 10 to 25 seconds of prefill on the
instance that receives it, and moving half a class at once would stall that instance for minutes
(sections 4.7 and 5.5).

The waiting and running counts are the instance heartbeats the decision already reads (issue E2-05),
and the 60 seconds of waiting are kept in the decision's memory the way the scale-out rule keeps its
own (issue E2-06). The scale-out rule adds an instance when one has waited 60 seconds while no other
has room; this rule acts when another has room, and changes no size. The fail-safes of issue E2-08
come first in the decision, so the rules land in the order of section 5.1. The number reaches the gateway
through the scaler's note on the pool, which the scaler writes on every run (issues E2-03 and E2-10).

## Deliverables

- [ ] `services/scaler/rhi_scaler/decide.py` raises a pool's rebalance number by one when an instance in service has reported waiting requests for 60 seconds while another instance in service of the pool runs fewer requests than half of target_concurrency, with the reason rebalance, and leaves the pool's desired capacity as the other rules set it.
- [ ] It raises the number again only after the pool's least time between two numbers, 60 seconds by default, and only while the condition still holds; the time of the last raise is kept in the memory the function receives and returns, and the function still reads no clock.
- [ ] `services/scaler/rhi_scaler/model.py` carries the pool's rebalance number in the decision, and `services/scaler/rhi_scaler/aws_io.py` writes it in the pool's note of issue E2-03.
- [ ] `tests/unit/scaler/test_rebalance.py` covers the rule and the write against the stubbed clients of issue E2-10.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_rebalance.py` passes: with target_concurrency 8, an instance that has waited 60 seconds beside one running 3 requests raises the number once, and once more a minute later while that lasts; waiting for 59 seconds raises nothing; a neighbour running 4 raises nothing; and the desired capacity is the same as without the rule.
- [ ] `tests/unit/scaler/test_rebalance.py::test_a_pool_that_has_just_grown` raises the number for a pool whose new instance is in service with nothing running, beside one that has waited 60 seconds.
- [ ] `tests/unit/scaler/test_rebalance.py::test_the_number_reaches_the_note` finds the raised number in the note the stubbed table receives, and an unchanged number on the runs that raise nothing.
- [ ] `uv run --locked pytest tests/unit/scaler` passes: the other rules decide as before.

## References

- `docs/design/system-design.md`, sections 4.7, 5.1, 5.5, 17
