+++
id = "E2-81"
title = "Dashboard: fleet and scaling, with the cold start of every launch"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-37", "E2-61"]
+++

## Context

The first of the six dashboards of section 7.3. It shows what the scaler decided and why, and what
each start cost: the fleet's desired, in-service and warm counts and the scale decisions with their
reasons come from the scaler (section 7.2; issue E2-11); the engine's running and waiting requests,
per instance, are the scaler's view of engine load (section 5.1); the seconds of each stage of each
launch come from the lifecycle agent (issue E2-37). Cold-start time is the central engineering
number of the design (sections 0 and 5.2), and its objective, the 90th percentile under 5 minutes
from the warm pool, is measured by issue E4-16.

It follows the rules and the build of issue E2-61: rates per instance then summed, "asleep" for a
pool at zero, a percentile with its request count.

## Deliverables

- [ ] `dashboards/rhi_dashboards/fleet.py` defines the fleet and scaling dashboard with one row per pool of the configuration: the desired, in-service and warm counts; the scale decisions, counted by reason; and the engine's running and waiting requests for each instance, by the names of the per-engine table.
- [ ] Each row also shows the seconds each stage of a start took, per launch, with warm-pool promotions and cold launches apart.
- [ ] `tests/unit/dashboards/test_fleet.py` builds the dashboard from the example configuration and checks its panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_fleet.py` passes: one row for each pool of the example configuration, with the panels named above, and every query on a metric that the scaler, the lifecycle agent or the per-engine table names.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the fleet dashboard in the package.

## References

- `docs/design/system-design.md`, sections 5.1, 5.2, 7.2, 7.3
