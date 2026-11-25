+++
id = "E6-15"
title = "Scaler: the decision is the session, and nothing reads demand"
labels = ["size:L", "kind:app"]
epic = "E6"
blocked_by = ["E6-09", "E6-11", "E6-13"]
+++

## Context

The scaler does not scale by load. It reconciles: each run sets every pool to the size its session
asks for, and to zero when it has none (system design, section 5.1). The reason is supply
(`docs/design/evidence.md`, E32): an instance asked for when a student needs it may come half an
hour later, and one given back because the pool was quiet may not come back.

Since E6-02 a session is a floor under the rules of today. Everything that relied on those rules
has moved to sessions: the verification (E6-07, E6-08), the canary (E6-09), the spikes (E6-11 to
E6-13). This issue replaces the rule table of `rhi_scaler.decide` by the one of section 5.1:

| Case | Rule |
| --- | --- |
| A session is active | The size is `requested`: the session's number, capped at `max_instances`. All of it is asked for at once |
| No session, or its end has passed | The size is zero |
| The size goes down | Capacity that has not arrived is no longer asked for. Then the instances in service beyond `requested` are released: those that report nothing running and nothing waiting first, then those with the fewest requests |
| rebalance | Unchanged |
| launch loop | Unchanged: three abandoned launches in a row suspend the group's launches, and a suspended pool is never raised |

Gone: the wake on a held request, growth and shrinking by load, the return to zero by idleness, the
stop of a disabled pool, and the return to zero when the gateway is silent. A gateway that does not
answer is an alarm, not a reason to give back GPUs that took long to get.

An instance still starting is never named for release: scale-in protection starts when an instance
enters service (E32), so it has none to lift. When the capacity is lowered, Auto Scaling may take
the starting instance by itself, which spike S7 measures; if it does not, the instance enters
service and the next run releases whichever is idle. Either way the pool reaches `requested`. A
busy instance that is released drains under the load balancer and the termination hook, which this
issue does not touch. What the deployed scaler then does to a lowered and to a stopped session is
what spike S7 records on the mock pool (E6-13), which is the owner's to run.

This issue changes the decision and its model, which are pure, and their tests. The edge keeps
reading what it reads today and the decision ignores it; E6-16 trims the edge and the memory. The
decision leaves the memory's attributes of load, idleness and the gateway at their empty values.

Why the size: the decision and four test modules are rewritten together, and cannot land apart.

After this issue the deployed system starts nothing without a session and holds each pool at its
session's number. A lowered session releases instances, and an ended one empties its pool within
the drain. The gateway is still the one that holds and wakes: a wake now asks for nothing, and a
held request for a pool whose session is active and that has no instance yet ends at the hold's
deadline. E6-18 removes the hold.

## Deliverables

- [ ] `services/scaler/rhi_scaler/decide.py` sets each pool's desired capacity to `requested` on every run, and reads neither the gateways' heartbeats, nor the pool's mode, nor the instances' load to size a pool.
- [ ] When `requested` is above the desired capacity the run read, the decision asks for all of `requested` at once with the reason `provision`; a pool whose launches are suspended is not raised, and its reason is `launch-suspended`.
- [ ] When the instances in service exceed `requested`, the decision names that many of them for release with the reason `release`: first those whose fresh heartbeat reports nothing running and nothing waiting, then those with the fewest requests running and waiting; it never names an instance that is still starting.
- [ ] The rebalance rule and the launch-loop rule decide as they do today, and their tests in `tests/unit/scaler/test_rebalance.py` and `tests/unit/scaler/test_fail_safes.py` still hold.
- [ ] `services/scaler/rhi_scaler/model.py` has the reasons provision, release, rebalance, launch-suspended and unchanged, and no other.
- [ ] `tests/unit/scaler/test_scale_out.py`, `tests/unit/scaler/test_scale_in.py` and `tests/unit/scaler/test_session_floor.py` are deleted, with the tests of a silent gateway in `tests/unit/scaler/test_fail_safes.py`; `tests/unit/scaler/test_decide.py`, `tests/unit/scaler/test_scenarios.py` and `tests/unit/scaler/sim.py` are rewritten for the table above.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_scenarios.py` passes with the six scenarios of section 5.1: a session that starts, one raised while instances still wait for capacity, one lowered under load, one that ends with requests running, a launch that never becomes healthy, and a record that asks for more than the pool allows.
- [ ] `tests/unit/scaler/test_decide.py::test_release_order` shows two idle instances released before a busy one, the busy ones by fewest requests, and a starting instance never named.
- [ ] `tests/unit/scaler/test_decide.py::test_no_rule_reads_demand` shows the same decision for a pool whatever the gateways' heartbeats and the pool's mode say: held requests wake nothing, a full pool does not grow, an idle pool stays at its session's number, and a silent gateway releases nothing.
- [ ] `uv run --locked pytest tests/unit/scaler` passes.

## References

- `docs/design/system-design.md`, sections 5.1 and 5.4
- `docs/design/evidence.md`, E32
- `services/scaler/rhi_scaler/decide.py`
- `services/scaler/rhi_scaler/model.py`
