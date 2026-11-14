+++
id = "E4-33"
title = "The session schedule in configuration: windows, the instances each needs, and the checks of section 4.7"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E1-13", "E1-17", "E2-04", "E3-10"]
+++

## Context

The course is five sessions, the first of four hours and the others of two (assumption A2). A
session's number of instances is a setting, not something the scaler discovers once the class has
started: each window of the schedule carries it, and the scaler pre-warms that many (sections 4.7
and 5.1; issue E4-05). A window that names a pool without a number pre-warms 1 instance of it
(section 17).

Section 4.7 states what a window's number must fit:

- `max_instances` must be at least that number;
- `warm_instances` should equal it: a number above it means cold launches on the day;
- the GPU quota must cover one instance more (section 5.4), which only the account can show;
- the daily GPU-hour ceiling must cover that many instances for the session's length plus 80 minutes
  of pre-warm and idle tail. The 80 minutes are the pool's pre-warm lead, 60 minutes by default for
  the coursework pool (sections 5.4 and 17), and its `idle_minutes`, 20. The defaults, two instances
  and 16 GPU-hours a day, cover the four-hour first session, which uses 10.7; four instances would
  need 22 (section 11.3). A pool with no daily ceiling, such as the mock pool, has nothing to check
  there.

Each pool has its own pre-warm lead: the pool field pre_warm_lead_minutes, 60 by default (sections
5.4 and 17), optional so that the pools already in SSM load unchanged; it reaches the scaler with the
rest of the pool's configuration (issue E2-12). The design does not say which pool a window that
names none would pre-warm, so a window names its pools.

**The deployed schedule.** The schedule and the ceilings are configuration in SSM, never in the
repository; the example holds an invented schedule (section 10). The loader of issue E1-03 reads
the schedule's time zone and the course end date, and refuses any session window until this issue
gives a window its shape, so that the owner's configuration in SSM keeps the list empty and holds
nothing this parser could refuse. This issue replaces that refusal with its parsing, and changes
the test that holds it. An empty list stays valid and means no session. Once this issue is merged,
the owner writes the windows in this shape to SSM with the configuration step of the owner-setup
script (issues E1-13 and E1-17).

## Deliverables

- [ ] `services/common/rhi_common/schedule.py` parses the session windows of the configuration, an empty list included, into windows, each with a start and an end read in the schedule's time zone and the pools it pre-warms, each with a number of instances, 1 when the window gives none; it says whether a time is inside a window widened before its start by a given lead.
- [ ] `services/common/rhi_common/pool_config.py` gains pre_warm_lead_minutes, 60 by default, and `tests/unit/common/test_pool_config.py` holds the default.
- [ ] `services/common/rhi_common/schedule.py` refuses, naming the window and the pool, a window that names no pool or a pool that is not configured, a number of instances above the pool's max_instances, and a number of instances that, each running for the window's length plus the pool's pre-warm lead and idle_minutes, would exceed the pool's gpu_hours_per_day; it returns a warning when the number differs from the pool's warm_instances.
- [ ] `src/rhi/config.py` parses the session windows with this module instead of refusing them, applies those checks when it loads a configuration, fails on a refusal, and prints each warning; `tests/unit/test_config.py::test_only_the_owners_keys_are_required` no longer asks for a session window to be refused, and still holds the set of required keys.
- [ ] `config.example.toml` gives an invented schedule of five windows whose first is four hours long and pre-warms 2 coursework instances, and gives the coursework pool's pre-warm lead of 60 minutes.
- [ ] `tests/unit/common/test_schedule.py` covers the parsing, the windows and the checks.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_schedule.py` passes: an empty list of windows parses to no window, a window that names the coursework pool without a number pre-warms 1 instance, and with the default lead a time 60 minutes before a window's start is inside its widened window while 61 minutes before is not.
- [ ] `tests/unit/common/test_schedule.py::test_the_checks_of_section_4_7` shows, for a pool with max_instances 2, warm_instances 2, idle_minutes 20, the default lead and gpu_hours_per_day 16, a four-hour window of 2 instances accepted with no warning, one of 3 instances refused for max_instances, one of 1 instance accepted with a warning, and, with max_instances raised to 4, a four-hour window of 4 instances refused for the daily ceiling.
- [ ] `uv run --locked pytest tests/unit/test_config.py tests/unit/common/test_pool_config.py` passes with the schedule of `config.example.toml`, and a pool without pre_warm_lead_minutes loads 60.
- [ ] The owner gives the dates of the sessions that use the endpoint, decision 1 of the design (needs human:owner-decision).
- [ ] After this issue is merged, the owner writes the session windows, in the shape it gives, to the configuration in SSM with the configuration step of `setup/owner-setup.sh` (needs aws:account).
- [ ] The owner says what students use if the endpoint is not ready for the first session, the second half of decision 1 of the design (needs human:owner-decision).
- [ ] The owner gives the class size and the kind of each session, decision 5 of the design, from which each window's number of instances is written to the configuration (needs human:owner-decision).
- [ ] The account's vCPU quota for the G instance family covers one coursework instance more than the largest number a window names, section 5.4 (needs aws:account).

## References

- `docs/design/system-design.md`, sections 4.7, 5.1, 5.4, 10, 11.3, 15, 17
