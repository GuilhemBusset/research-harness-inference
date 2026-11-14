+++
id = "E0-01"
title = "Report the outcome of each deployment as a commit status"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

The pipeline deploys every merge commit. Nothing tells the loop how that went, so an issue is
closed on a merge that may never have deployed.

## Deliverables

- [ ] `infra/pipeline/status_reporter.py` posts a `deploy/<stage>` commit status for the commit a pipeline execution deployed.
- [ ] `tests/unit/test_status_reporter.py` covers a successful, a failed and a superseded execution.

## Acceptance criteria

- [ ] `uv run pytest tests/unit/test_status_reporter.py` passes with no AWS credentials in the environment.
- [ ] A failure summary is redacted before it is posted: `tests/unit/test_status_reporter.py::test_summary_is_redacted` fails when an account ID reaches the description.
- [ ] After a merge to `main`, the merge commit shows a `deploy/prod` status within the pipeline's run (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, section 9
