+++
id = "E0-13"
title = "Reporter: give a verdict to commits whose execution was superseded"
labels = ["size:M", "kind:app"]
epic = "E0"
blocked_by = ["E0-09", "E0-12"]
+++

## Context

The pipeline runs queued, one execution per commit, but a self-update of the pipeline can cancel
executions that were still queued (section 8.4; evidence E20). Without care, the commits of those
executions never get a status, and the factory waits for a verdict that will not come. The reporter
marks such a commit as superseded and names the commit whose execution covers it, so every merge
ends with a verdict.

The description follows the vocabulary of issue E0-09: its superseded member, followed by a space
and the covering commit's full SHA, is the one description allowed to carry more than a member. To
find the cancelled executions the function's role must list this pipeline's executions, which the
policy test of issue E0-12 pins, so this issue changes the role and that test.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `services/reporter/rhi_reporter/superseded.py` decides, from the pipeline's executions, which commits were cancelled by a self-update and which later execution covers each.
- [ ] A superseded commit gets, under the deployment and the verification contexts of the stage, a pending status whose description is the vocabulary's superseded member followed by a space and the covering commit's full SHA, and then, once the covering execution ends, that execution's final state under the same contexts.
- [ ] `services/reporter/rhi_reporter/handler.py` applies it on every execution's end.
- [ ] `src/rhi/pipeline/reporter.py` and `src/rhi/pipeline/stack.py` let the function's role list this pipeline's executions, and nothing more than before; `tests/policy/test_pipeline.py` holds the role to that.
- [ ] Every cdk-nag finding `src/rhi/pipeline/reporter.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_reporter.toml`.
- [ ] `tests/unit/reporter/test_superseded.py` covers the decision, and `tests/synth/test_reporter.py` the role.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_superseded.py` passes: of three queued commits whose first execution updates the pipeline, the two cancelled ones end with the state of the execution that covered them, and name its commit.
- [ ] `tests/unit/reporter/test_superseded.py::test_every_commit_ends_with_a_verdict` checks, over generated sequences of executions, that no merged commit is left without a final status.
- [ ] `uv run --locked pytest tests/synth/test_reporter.py tests/policy/test_pipeline.py` passes: the role may list this pipeline's executions and no other pipeline's, and is otherwise as before.

## References

- `docs/design/system-design.md`, section 8.4
- `docs/design/evidence.md`, E20
