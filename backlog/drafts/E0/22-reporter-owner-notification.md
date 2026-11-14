+++
id = "E0-22"
title = "Reporter: send the owner what a public status may not carry, by SNS"
labels = ["size:M", "kind:infra"]
epic = "E0"
blocked_by = ["E0-12"]
+++

## Context

A commit status is public, so it carries a state, a fixed context and allow-listed parts only, never
text from AWS (section 8.4). The rest of a failure (the failed action's error message, the status
reason of each failed resource) is what a person needs to diagnose it, and section 8.4 sends it to
the owner by SNS: "Anything beyond that goes to the owner by SNS." Some failures in phases 1 and 2
will need the owner to relay a diagnosis to the factory; the design says so.

The pipeline exists from phase 0, before the stage's alarms and their topic (phase 3), so the
pipeline stack has a topic of its own. Its subscriber is the admin's e-mail address, the key of the
configuration that issue E1-03 already holds (section 8.6, step 4), never in the repository: tests
use the example configuration's address. No configuration key is added. The topic's ARN reaches the
function in the environment variable RHI_REPORTER_TOPIC. The owner confirms the subscription once,
from the e-mail it sends.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pipeline/notify.py` adds to the pipeline stack an SNS topic, encrypted and refusing unencrypted transport, with one e-mail subscription to the admin address of the configuration, `src/rhi/pipeline/stack.py` builds it, and `tests/synth/test_reporter.py` checks the topic and its subscription.
- [ ] `services/reporter/rhi_reporter/notify.py` builds, for a failed or errored execution, one message with the commit, the failed stage and action, the action's error details, and each failed resource's logical ID, status code and status reason, as AWS gives them; `tests/unit/reporter/test_notify.py` covers it.
- [ ] `services/reporter/rhi_reporter/handler.py` publishes that message to the topic RHI_REPORTER_TOPIC names after it posts the execution's statuses, and a failure to publish never stops a status from being posted.
- [ ] `src/rhi/pipeline/reporter.py` gives the function the topic's ARN in RHI_REPORTER_TOPIC and lets its role publish to that topic and to no other, and `tests/policy/test_pipeline.py` fails when the role may publish anywhere else.
- [ ] Every cdk-nag finding `src/rhi/pipeline/notify.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_notify.toml`.
- [ ] Every cdk-nag finding `src/rhi/pipeline/reporter.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_reporter.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_notify.py` passes: a recorded failed execution gives one message with the error details and the status reasons, a successful one gives none, and a stub topic that refuses the message leaves the statuses posted.
- [ ] `uv run --locked pytest tests/synth/test_reporter.py tests/policy/test_pipeline.py` passes: one encrypted topic, one e-mail subscription to the example configuration's address, and a role that may publish to that topic only.
- [ ] `scripts/check.sh synth` exits 0 with every `AWS_*` variable unset.
- [ ] The owner confirms the subscription, and the details of a failed execution reach the admin's address (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.4, 8.6, 10, 14
