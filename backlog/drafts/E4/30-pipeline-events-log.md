+++
id = "E4-30"
title = "The pipeline's events, kept in a log group that Grafana reads"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E1-09", "E2-60"]
+++

## Context

The deployments dashboard shows the pipeline's runs and their results, from the pipeline's events,
which section 7.2 keeps in CloudWatch Logs. CodePipeline publishes its execution, stage and action
state changes to EventBridge; a rule sends this pipeline's to a log group of their own. The reporter
already listens to the same events for the commit statuses (issue E0-12); this rule only keeps
them.

Grafana's role reads named log groups and never a build project's, which holds real configuration
(section 7.4). The pipeline's state-change events carry no configuration value. The log group is in
the pipeline's stack and Grafana in the stage, so the log group has a fixed name by which Grafana's
role names it.

**Retention.** Retention is set on every store (section 6.6; issue E4-18). The design gives no value
for this log group, whose events hold no personal data: the key pipeline_events_retention_days sets
it, 90 days by default, the period of every log group of the stage. It is optional, so that a
configuration in SSM written before this issue loads unchanged; the CDK app reads it when it
synthesizes the pipeline stack.

EventBridge needs a resource policy on the log group to write to it. CDK's log-group target installs
that policy through a custom resource, which would add a function and a role to the pipeline stack;
here the policy is a resource of the template itself, so the stack gains no function and no role, and
the pipeline's reviewed roles in `tests/policy/test_pipeline.py` do not change.

This issue changes `src/rhi/pipeline/`, a protected path (ADR 0001, decision 10 and amendment 1),
and names each file it changes in its Deliverables.

## Deliverables

- [ ] `src/rhi/pipeline/events.py` defines a log group of a fixed name with a retention of pipeline_events_retention_days, an EventBridge rule that sends this pipeline's execution, stage and action state changes to it and nothing else, and the log group's resource policy that lets EventBridge write to it, as a resource of the template; `src/rhi/pipeline/stack.py` adds them to the pipeline stack.
- [ ] `src/rhi/config.py` gains pipeline_events_retention_days, 90 by default; `config.example.toml` gives it; `tests/unit/test_config.py` holds the default.
- [ ] `src/rhi/observability/grafana.py` lets Grafana's task role read that log group, by its name.
- [ ] `tests/synth/test_pipeline_events.py` checks the rule, the log group and Grafana's access to it.
- [ ] Every cdk-nag finding `src/rhi/pipeline/events.py` or `src/rhi/observability/grafana.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_events.toml` or `tests/policy/acknowledged/observability_grafana.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline_events.py tests/synth/test_grafana.py tests/unit/test_config.py` passes: the rule matches this pipeline's state changes only, its one target is the log group, the log group keeps its events 90 days under the example configuration, the log group's resource policy is a resource of the template and no custom resource serves it, Grafana's role may read the log group and still no build project's log group, and a configuration that leaves the key out loads 90.
- [ ] In the account, an execution of the pipeline leaves its state changes in the log group (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.6, 7.1, 7.2, 7.4
