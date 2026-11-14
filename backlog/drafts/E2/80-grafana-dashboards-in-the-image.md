+++
id = "E2-80"
title = "Grafana's image carries the dashboards built from the deployed configuration"
labels = ["size:S", "kind:infra"]
epic = "E2"
blocked_by = ["E2-60", "E2-81"]
+++

## Context

Dashboards are files in Grafana's container image; nothing imperative (section 7.4). They are built
from the configuration (issue E2-61), and the real configuration, pools included, is read from SSM
by the pipeline's synth step and never committed (sections 8.2 and 10). So the dashboards are built
at synthesis, from the configuration that is being deployed, into the image's build context: their
rows follow the deployed pools, and the inference dashboard shows the deployed pools' expected
values (section 4.7). In CI the same synthesis uses the example configuration.

The image's Dockerfile copies the dashboard files of its build context's dashboards folder (issue
E2-59). A build that is the same for the same configuration keeps the image's asset hash, so Grafana
is not redeployed by an unrelated change. The build runs at synthesis with no container runtime: the
image itself is built by the pipeline's asset step.

This issue may change `tests/policy/acknowledged/`, a protected path (ADR 0001, decision 10); the
builder may write the file named below because this issue names it.

## Deliverables

- [ ] `src/rhi/observability/grafana.py` declares the Grafana image as an asset whose build context, assembled at synthesis, holds the files of the Grafana image's folder and, in its dashboards folder, the dashboards that the build of issue E2-61 writes from the loaded configuration.
- [ ] Every cdk-nag finding `src/rhi/observability/grafana.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/observability_grafana.toml`.
- [ ] `tests/synth/test_grafana_dashboards.py` synthesizes the stage and checks the image asset.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_grafana_dashboards.py` passes: the image asset synthesized from the example configuration holds the fleet dashboard with one row per pool, two syntheses give the same asset hash, and a configuration with one more pool gives another.
- [ ] `scripts/check.sh synth` exits 0 with every `AWS_*` variable unset.
- [ ] In the account, the admin signed in to Grafana finds the fleet dashboard with one row for each deployed pool (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.7, 7.3, 7.4, 8.2, 10
