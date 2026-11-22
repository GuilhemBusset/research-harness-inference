+++
id = "E3-49"
title = "The weekly canary runs the heartbeat smoke test, querying only the coursework pool's rows"
labels = ["size:S", "kind:infra"]
epic = 5
blocked_by = [171]
+++

## Context

The weekly GPU canary (`src/rhi/pipeline/canary.py`, issue #171) runs, on the coursework pool, the
smoke tests the pipeline's verification runs after a refresh, less one: `release-heartbeat`
(`tests/smoke/deployed/test_release_heartbeats.py`), which reads the rows of the pool's instances in
the stage's heartbeats table, a DynamoDB table the canary's role may not read. The run leaves it out
by its ID, `CANARY_DESELECTED` of `scripts/pipeline/canary_result.py`. So the canary checks the
release each instance serves only through the workspace, whose collector can hold a report back for
minutes, and not through the row each instance writes itself.

The owner decided to grant the canary that read (issue #171, D1): its role may query the heartbeats
table, and only the coursework pool's partition of it, the one pool the canary tests, so that the
weekly run makes every post-refresh smoke test of that pool.

**The grant.** `dynamodb:Query` on the stage's heartbeats table and no other action on it or on any
other table, under the condition that `dynamodb:LeadingKeys` is the coursework pool's name alone,
the form the pools' instance role already uses for its own pool's rows (`src/rhi/pools/role.py`).
The table is named as the verification's step names it, by the ARN pattern that
`rhi.pipeline.verify.heartbeats_table_arn` builds from the pools stack's name and the table's
logical ID, since the pipeline's stack can hold no reference to a resource of the stage. The
wildcard of that pattern stands for the suffix CloudFormation generates; cdk-nag's finding on it is
acknowledged as the verification's is (`tests/policy/acknowledged/pipeline_stack.toml`).

**The table's name.** The smoke test reads it from `RHI_VERIFY_HEARTBEATS_TABLE`. The canary's
script already reads the coursework pool's release parameter for the workspace's address
(`stage_settings`), and the parameter's settings also name the heartbeats table (`heartbeat_table`,
`services/engine/lifecycle/rhi_lifecycle/release.py`), so the script gives the test that name from
there, and the role needs nothing more to find it.

This issue changes `src/rhi/pipeline/` and `tests/policy/`, protected paths (ADR 0001, decision 10
and amendment 1), and names each file it changes in its Deliverables.

## Deliverables

- [ ] `src/rhi/pipeline/canary.py` gives the canary job's role `dynamodb:Query` on the stage's heartbeats table, named by the ARN pattern of `rhi.pipeline.verify.heartbeats_table_arn`, under a `ForAllValues:StringEquals` condition on `dynamodb:LeadingKeys` that admits only the coursework pool's name, and no other DynamoDB action; its docstring says so.
- [ ] `scripts/pipeline/canary_result.py` gives the smoke tests `RHI_VERIFY_HEARTBEATS_TABLE` from the heartbeat_table of the coursework pool's release parameter's settings, no longer has `CANARY_DESELECTED` or the deselection it applied, and its docstring says that the run makes every test the verification makes on the coursework pool after a refresh.
- [ ] `tests/policy/acknowledged/pipeline_canary.toml` lists the query, its table and its condition among the role's actions with its reason, and acknowledges cdk-nag's finding on the table's ARN pattern with its reason.
- [ ] `tests/synth/test_canary.py` checks the role's statements with the query, and `tests/unit/test_canary_result.py` checks the table's name given to the tests and the run without a deselected test.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_canary.py` passes: the job's role holds exactly one DynamoDB statement, whose only action is `dynamodb:Query`, whose only resource is the heartbeats table's ARN pattern, and whose condition admits only the leading key coursework; its other statements are unchanged; and the parked synthesis still has no role.
- [ ] `uv run --locked pytest tests/unit/test_canary_result.py` passes: the smoke tests are given `RHI_VERIFY_HEARTBEATS_TABLE` equal to the release parameter's heartbeat_table, and the run collects every test the verification's run on the coursework pool after a refresh collects, `release-heartbeat` included.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/policy/test_nag_acknowledgements.py tests/policy/test_iam_wildcards.py tests/synth/test_nag.py` passes.
- [ ] In the account, the next weekly canary's run makes `release-heartbeat` and it passes (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 8.2 and 14
- `src/rhi/pipeline/canary.py`
- `src/rhi/pipeline/verify.py`
- `src/rhi/pools/role.py`
- `tests/smoke/deployed/test_release_heartbeats.py`
