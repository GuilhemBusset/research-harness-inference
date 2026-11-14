+++
id = "E4-16"
title = "Measure the six objectives of section 7.3, during session windows only"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-74", "E2-81", "E2-84", "E3-06", "E4-06", "E4-31", "E4-33"]
+++

## Context

Section 7.3 sets six objectives, measured during session windows:

| Objective | Target |
| --- | --- |
| Gateway answers | 99.5% of requests are not 5xx from the gateway's own fault |
| Time to first token, warm pool | 95th percentile under 5 s, with the first turns of sessions counted apart |
| Output speed per stream, warm pool | On the coursework pool, the median request at 15 tokens per second or more and the slowest tenth at 10 or more |
| Cached share of input | Above 85% |
| Cold start from the warm pool | 90th percentile under 5 minutes |
| Pool ready at session start | Every scheduled pool in service 30 minutes before |

The speed targets and the cached share are provisional, held in configuration until spike S5 has
measured the pool (issue E4-31). The other three targets are fixed by the design. The dry-run class
of phase 3 is judged against these objectives (section 14; issue E4-26), so they are defined once,
in a module that both the dashboard and the dry run read.

Where each comes from:

- Gateway answers: the gateway's counts of requests and of errors by cause (issue E2-79). Every
  error the gateway means to give names a cause of section 6.2, and each 5xx cause names a reason
  outside the gateway: a pool starting, out of capacity, scheduled-only or disabled, the engine, or
  the 30-minute limit. A 5xx that names no cause is the gateway's own fault.
- Time to first token and speed per stream: the gateway's histograms of what students experience
  (issues E2-74 and E2-84), by the names `services/gateway/rhi_gateway/experience.py` emits; their bucket
  boundaries include the floors, so each floor is read exactly. A warm request is one that was not
  held; first turns and later turns are counted apart. The speed objective applies to the pools
  that run a GPU engine, the coursework pool in the example (issue E3-10).
- Cached share: the engine's cached input tokens over its input tokens, by the per-engine table
  (issue E3-06).
- Cold start from the warm pool: the lifecycle agent's seconds per stage of a warm-pool promotion
  (issue E2-37), already on the fleet dashboard (issue E2-81).
- Pool ready at session start: the series the class-day notice sends to the workspace for each
  window, 1 for go and 0 for no-go, at 30 minutes before its start (issue E4-06).

**How a query is limited to the session windows.** PromQL has no notion of a window, and the
dashboards are built before any window is known to them. So the scaler, which reads the schedule
(issue E4-05), sends a gauge to the workspace on every run: 1 while the time is inside a session
window of the configuration, not widened by any lead, and 0 outside. Every objective's query but
one keeps only the samples taken while that gauge is 1 (in PromQL, the series combined with the
gauge equal to 1, over a subquery of one-minute steps), so the same dashboard is right for any
schedule.

The exception is "pool ready at session start". Its series holds one verdict per window, sent 30
minutes before the window starts (issue E4-06), when the gauge reads 0, so the filter would drop
every sample. That objective needs no filter: each of its samples already belongs to one window. Its
query reads the verdicts sent in the dashboard's time range as they are, and its share is the
windows whose verdict was 1 over the windows with a verdict.

## Deliverables

- [ ] `services/scaler/rhi_scaler/metrics.py` sends to the Prometheus workspace, on every run, a gauge that is 1 while the time is inside a session window of the configuration and 0 outside, with no student label; `tests/unit/scaler/test_session_gauge.py` covers it on a clock the test controls.
- [ ] `dashboards/rhi_dashboards/objectives.py` defines the six objectives of the table above, each with the target it is compared with: five as queries that keep only the samples taken while that gauge is 1, and pool ready at session start as a query over the verdicts of issue E4-06 with no such filter.
- [ ] The time-to-first-token, speed and cached-share targets are read from the configuration of issue E4-31; the targets 99.5%, 5 minutes and 30 minutes are the design's; the gateway objective counts as the gateway's own fault a 5xx answer that names no cause of section 6.2; the speed objective is measured for each pool whose engine is not the mock.
- [ ] `dashboards/rhi_dashboards/fleet.py` gains a row that shows each objective against its target, over the session windows in the dashboard's time range, and `tests/unit/dashboards/test_fleet.py` checks that row.
- [ ] `tests/unit/dashboards/test_objectives.py` covers the six queries.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_objectives.py tests/unit/dashboards/test_fleet.py` passes: six objectives, with targets of 99.5%, 5 seconds at the 95th percentile, 15 and 10 tokens per second, 85%, 5 minutes at the 90th percentile and 30 minutes under the example configuration, and every query but the pool-ready one limited by the in-session gauge.
- [ ] `tests/unit/dashboards/test_objectives.py::test_the_ready_objective_is_not_filtered` finds the pool-ready query reading the verdict series that `services/scaler/rhi_scaler/metrics.py` sends for each window, by its name, with no reference to the in-session gauge.
- [ ] `tests/unit/dashboards/test_objectives.py::test_first_turns_apart` shows the time-to-first-token objective as two queries, one over first turns and one over later turns, both over warm requests only.
- [ ] `tests/unit/dashboards/test_objectives.py::test_the_targets_follow_the_configuration` sets other provisional targets in the configuration and finds them in the objectives.
- [ ] `uv run --locked pytest tests/unit/scaler/test_session_gauge.py` passes: the gauge reads 1 at a window's start and one minute before its end, and 0 one minute before its start, 60 minutes before it, and after its end.

## References

- `docs/design/system-design.md`, sections 6.2, 7.3, 14, 17
