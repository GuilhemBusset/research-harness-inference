+++
id = "E2-88"
title = "The stateful-resource invariant: each kind of state keeps its protections and its logical ID"
labels = ["size:S", "kind:test"]
epic = "E2"
blocked_by = ["E1-07"]
+++

## Context

There is one environment, so state is protected (section 8.5): point-in-time recovery, deletion
protection and a retain policy on the DynamoDB tables, and a test that fails when a stateful
resource's logical ID changes. A rename by a coding agent would otherwise replace the key table and
void every student's key mid-course. Section 8.4 lists "retention and deletion protection on stateful
resources and their logical IDs unchanged" among the invariants.

Not every kind of state has all three protections. The stage will hold four kinds, and each gets
those its type has:

| Type | Issue | Protections |
| --- | --- | --- |
| DynamoDB table | E2-49 | point-in-time recovery, deletion protection, retain on deletion and on replacement |
| Managed Prometheus workspace | E2-57 | retain on deletion and on replacement (the type has neither of the other two) |
| S3 bucket | E3-07, E4-37 | retain on deletion and on replacement |
| Cognito user pool | E2-85 | deletion protection, retain on deletion and on replacement |

The invariant comes before the state it protects (issue E2-49 creates the first tables), so it passes
on the app as it stands, with an empty record, and is proven on templates built in the test. It reads
the stage's templates, not the pipeline stack's, whose artifact bucket CDK Pipelines owns. A resource
is recorded by its stack and its logical ID; each issue that adds state records it.

This issue creates `tests/policy/test_stateful_resources.py` and
`tests/policy/stateful_logical_ids.json`. `tests/policy/` is a protected path (ADR 0001, decision
10): the builder may write the files named below because this issue names them.

## Deliverables

- [ ] `tests/policy/test_stateful_resources.py` holds the four stateful types of Context with the protections each needs, and fails when a resource of the stage of one of those types lacks one of its protections, is missing from `tests/policy/stateful_logical_ids.json`, or has another logical ID than the one recorded for its stack.
- [ ] `tests/policy/stateful_logical_ids.json` records, by stack and logical ID, every stateful resource of the stage, none yet, and the test fails on a recorded resource that the stage no longer holds.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_stateful_resources.py` passes on the app as it stands.
- [ ] `tests/policy/test_stateful_resources.py::test_detects_a_rename` fails when a recorded table's construct is renamed in a template built in the test, and `tests/policy/test_stateful_resources.py::test_detects_a_missing_protection` fails for each protection of the table of Context removed from a table, a workspace, a bucket and a user pool built in the test.

## References

- `docs/design/system-design.md`, sections 8.4, 8.5
