+++
id = "E4-05"
title = "Scaler: pre-warm the instances each session window names"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-08", "E4-02", "E4-33"]
+++

## Context

Inside a session window minus the pool's lead time, the scaler keeps at least the number of
instances that window names in the schedule, 1 if it names none (section 5.1, the pre-warm rule;
section 4.7). The lead is the pool's own, 60 minutes by default for the coursework pool (section
5.4). The schedule, with its windows and their numbers, is parsed and checked by issue E4-33, which
also adds the pool's lead to the pool's configuration.

The schedule lives in the scaler and nowhere else: the group's minimum is always zero and it has no
scheduled minimum, so that a schedule and a stop cannot contradict one another (section 5.1). A
stop wins: a pool that is disabled, latched by the budget, at a GPU-hour ceiling (issue E4-02) or
whose launches are suspended (issue E2-08) is not pre-warmed, and a silent gateway still takes the
pool to zero, since nobody can reach the fleet without it (section 5.1; issue E2-08). A pre-warm
needs no request, and it starts a `scheduled-only` pool, which a student's request never does
(section 4.2).

Between the window's widened start and its end, the rules that take instances away (n to n-1, and
to zero after `idle_minutes`) never take the pool below the window's number. After the end they apply
as usual.

The deployed function receives in its settings every pool's configuration, the leads included, and
the schedule's time zone (issues E2-11, E2-12 and E4-01). It does not yet receive the session
windows: this issue adds them to the settings, so that pre-warm fires in the account and not only in
the tests. Settings without them read as no window, and while the configuration in SSM holds no
window, the scaler pre-warms nothing.

## Deliverables

- [ ] `services/scaler/rhi_scaler/model.py` adds the session windows of the configuration, parsed by the schedule module of issue E4-33, to the inputs of a run.
- [ ] `services/scaler/rhi_scaler/decide.py` keeps, for each pool a window names, at least the number of instances the window names, from the window's start minus the pool's pre-warm lead until its end, for a pool in mode enabled or scheduled-only, with no request at all, and gives the reason pre-warm.
- [ ] While that holds, no scale-in rule takes the pool below that number, except the rule for a silent gateway; and a pool that is disabled, budget-latched, at a GPU-hour ceiling or launch-suspended is never pre-warmed; `tests/unit/scaler/test_pre_warm.py` covers the rule.
- [ ] `src/rhi/pools/scaler.py` adds the session windows of the configuration to the function's settings, and `services/scaler/rhi_scaler/settings.py` reads them, none when the settings give none; `tests/synth/test_scaler.py` and `tests/unit/scaler/test_settings.py` check both.
- [ ] Every cdk-nag finding `src/rhi/pools/scaler.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_scaler.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_pre_warm.py` passes: for a window that names 2 coursework instances, with the default lead and no request at all, 2 instances are asked for 60 minutes before the start and none 61 minutes before, the pool keeps 2 through the window with nothing in flight, and it goes to zero once the window has ended and idle_minutes have passed.
- [ ] `tests/unit/scaler/test_pre_warm.py::test_one_when_the_window_names_none` shows 1 instance kept for a window that names the pool without a number, and a scheduled-only pool started by its window.
- [ ] `tests/unit/scaler/test_pre_warm.py::test_a_stop_wins_over_a_schedule` shows a disabled pool, a budget-latched pool, a pool at a GPU-hour ceiling and a launch-suspended pool not pre-warmed, and a pre-warmed pool taken to zero when no gateway has reported for 10 minutes.
- [ ] `uv run --locked pytest tests/synth/test_scaler.py tests/unit/scaler/test_settings.py` passes: the scaler function's settings carry the example configuration's session windows and each pool's pre-warm lead, and settings without windows read as none.

## References

- `docs/design/system-design.md`, sections 4.2, 4.7, 5.1, 5.4, 17
