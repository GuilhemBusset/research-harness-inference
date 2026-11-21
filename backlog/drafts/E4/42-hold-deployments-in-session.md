+++
id = "E4-42"
title = "Hold the pipeline's deployment while a session is on"
labels = ["size:M", "kind:infra"]
epic = 6
blocked_by = ["E4-40"]
+++

## Context

There is one environment, and a deployment that touches a pool refreshes its instances (section
4.3), which in a class would cost the students their warm instances and their cached transcripts;
a gateway deployment drains for up to 30 minutes (section 8.5). So the pipeline's deployment waits
while a session is on (sections 5.6 and 8.2): a step before the stage's deployment reads the
session switch (issue E4-40) and passes once it reads off.

**What the step reads.** The switch is an item of the deployed stage's pool-state table, partition
key `_session`, sort key `switch`. The step finds the table's name by the table's logical ID in the
pools stack, as the admin CLI finds its tables (`admin/rhi_admin/main.py`), and the spike jobs
already take that stack's name and the table's logical ID from the stage as text
(`src/rhi/pipeline/spikes.py`, `pool_state_table_arn`): the pipeline's stack, another assembly,
holds no reference to a resource of the stage. The step passes at once when the item is missing
(the switch reads off), and when the pools stack or its table does not exist yet, as before the
first deployment of the stage.

**How long it waits.** While the switch reads on, the step reads it again every 60 seconds. A
session is a few hours (assumption A2); the step's timeout is CodeBuild's limit for one build, 36
hours, so that only a session left on for a day and a half fails the execution (section 17).

**Where it runs.** Among the stage's steps before its deployment
(`src/rhi/pipeline/stack.py`, `add_stage(..., pre=[...])`), after the permissions check, so that an
approval the owner gives is not lost while the step waits.

**What its role holds.** Reading the one stack resource of the pools stack that names the table,
and reading items of that table; CDK Pipelines also gives every step's role what any step needs,
the same for each: writing the step's build logs and reports, and reading and writing the
pipeline's artifact bucket with its key. Nothing beyond those.

This issue changes `src/rhi/pipeline/` and `tests/policy/`, protected paths (ADR 0001, decision 10
and amendment 1), and names each file it changes in its Deliverables.

## Deliverables

- [ ] `scripts/pipeline/deploy_hold.py` reads the session switch from the pool-state table of the deployed stage, found by its logical ID in the pools stack; it exits 0 at once when the switch reads off, when the item is missing, or when the stack or the table does not exist yet, and while it reads on it reads it again every 60 seconds until it reads off, then exits 0.
- [ ] `src/rhi/pipeline/hold.py` defines a step that runs it, with the pools stack's name and the table's logical ID as environment variables taken from the stage as text, a timeout of 36 hours, and a role that may read that stack resource and the table's items and holds nothing else beyond what CDK Pipelines gives every step's role.
- [ ] `src/rhi/pipeline/stack.py` runs that step among the stage's steps before its deployment, after the permissions check.
- [ ] `tests/policy/test_pipeline.py` records the step and its role in its reviewed list, and every cdk-nag finding `src/rhi/pipeline/hold.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason in `tests/policy/acknowledged/pipeline_stack.toml`.
- [ ] `tests/unit/test_deploy_hold.py` covers the script against stubbed clients and a clock the test controls, and `tests/synth/test_pipeline_hold.py` checks the step's place, its timeout, its environment and its role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_deploy_hold.py` passes: the switch off, the item missing and the stack missing each exit 0 at once with one read; the switch on for three reads and then off exits 0 after the fourth read, 180 seconds later on the test's clock.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_hold.py tests/policy/test_pipeline.py` passes: the step runs before the stage's deployment and after the permissions check, its timeout is 36 hours, its environment names the pools stack and the table's logical ID, and its role reads one stack resource and the one table and nothing else beyond what CDK Pipelines gives every step's role.
- [ ] In the account, a merge while a session is switched on deploys only after the session is switched off, and a merge while it is off deploys at once (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 5.6, 8.2, 8.5 and 17
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 2
- `src/rhi/pipeline/spikes.py`
- `admin/rhi_admin/main.py`
