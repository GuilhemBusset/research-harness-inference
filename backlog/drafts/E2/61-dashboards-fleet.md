+++
id = "E2-61"
title = "Dashboards as Python: built from the configuration, with the rules every panel follows"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-11", "E2-59", "E2-68", "E2-69", "E2-82"]
+++

## Context

Six dashboards (fleet and scaling, GPU health, inference performance, students, cost, deployments)
are written in Python with the Grafana Foundation SDK and checked in CI (section 7.3). The SDK is a
preview at version 0.0.x (evidence E19); plain JSON with a schema check is the fallback. This issue
lays the ground they share; each dashboard is an issue of its own (E2-81, E4-11 to E4-15), and each
adds one module to the package without touching the build.

A dashboard is built from the configuration, because a pool is one configuration entry and one
dashboard row (section 4.2): the rows follow the pools, each row's engine queries follow the pool's
engine, and the inference dashboard shows each pool's expected values (section 4.7). Engines name
their metrics differently, so engine queries take their names from the per-engine table, and adding
an engine is one adapter and one table row (section 4.1). In tests the dashboards are built from the
example configuration; at synthesis, from the deployed one (issue E2-80). A build that differed from
one run to the next would rebuild Grafana's image on every deployment.

Section 7.2 gives six rules, because each has a way of showing a wrong number. Three of them belong
to every panel and are built here: rates are taken per instance and then summed, since an instance's
counters restart from zero every time it starts; a pool at zero has no engine series at all, so its
panels take the pool's state from the scaler and show "asleep", never a gap or a stale value; a
percentile is shown with the number of requests behind it. The rule on input tokens belongs to the
inference dashboard (issue E4-12), the finer buckets to the engine's flags, and the labels for pool,
instance and instance type to the collector (issue E2-58). The data sources have fixed identifiers
(issue E2-59).

## Deliverables

- [ ] `dashboards/rhi_dashboards/build.py` builds every dashboard of the package from a configuration and writes each as one JSON file into a given folder; a module that defines a dashboard is found without being listed in the build.
- [ ] `pyproject.toml` and `uv.lock` pin the Grafana Foundation SDK for Python to an exact version, and the dashboards are written with it, or, for a panel the SDK cannot express, as plain JSON with a schema check.
- [ ] `dashboards/rhi_dashboards/rules.py` gives the helpers every panel uses: a rate or an increase taken per series and then summed by pool; an engine panel that also queries the pool's in-service count from the scaler and shows asleep when it is zero; a percentile panel that also shows the count of requests over the same window.
- [ ] An engine query takes its metric names from the per-engine table, at the row of the engine that the pool's configuration names, and every query names its data source by the fixed identifier of issue E2-59.
- [ ] Each dashboard module declares, for every metric name it queries, where the name comes from: the per-engine table, the file of this repository that emits it, or an outside exporter named with its pinned version.
- [ ] `tests/unit/dashboards/test_rules.py` covers the helpers, and `tests/unit/dashboards/test_build.py` builds every dashboard of the package from the example configuration and holds each to the rules.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_rules.py` passes: the rate helper never takes a rate or an increase over a sum, the engine helper's panel reads asleep when the scaler's in-service count is zero, and the percentile helper's panel carries a request count over the same window.
- [ ] `tests/unit/dashboards/test_build.py::test_every_dashboard_follows_the_rules` fails a dashboard, built in the test, with a rate over a sum, an engine panel without the pool's in-service count, a percentile without its count, or a metric name it does not declare, or declares from a file that does not contain it.
- [ ] `tests/unit/dashboards/test_build.py::test_the_build_is_stable` builds every dashboard twice from the example configuration and gets identical bytes, and no two dashboards share an identifier or a title.

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 4.7, 7.2, 7.3
- `docs/design/evidence.md`, E19
