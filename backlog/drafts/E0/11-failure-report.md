+++
id = "E0-11"
title = "Reporter: a structured failure report made of names and enumerations only"
labels = ["size:M", "kind:app"]
epic = "E0"
blocked_by = ["E0-09"]
+++

## Context

A red status alone does not tell the factory what to fix. Section 8.4 adds a structured report to a
failure, as further commit statuses on the same commit, one per fact. Each is assembled only from
allow-listed parts: the stage and action that failed, the logical IDs of failed resources (checked
against the names in that commit's own synthesized template), CloudFormation's status codes, the
IDs of failed smoke tests, and the last lifecycle stage an instance reached. Enumerations and names
from this repository only: no free text, and no commit comment. ADR 0001, amendment 4, records the
same for the factory: the reporter writes commit statuses and nothing else.

This is the pure part: a function from events and allow-lists to statuses. Three of the allow-lists
come from elsewhere and are supplied by the caller: the logical IDs of the templates synthesized from
the same commit (issue E0-12), the smoke tests' IDs (`tests/smoke/ids.py`, issue E2-63, wired in by
issue E0-21), and the stages of an instance's start (the lifecycle agent's enumeration, issue E2-37).
CloudFormation's resource status codes are a public, fixed list, kept in the module.

Each fact keeps its own status, so each needs its own context. The engine reads a fact as a status
whose context is the stage's deployment or verification context followed by a slash and more, and
gives those facts to the fix-forward of a failed deployment (issues E0-07 and E0-29). GitHub refuses
a description longer than 140 characters.

## Deliverables

- [ ] `services/reporter/rhi_reporter/report.py` builds the report from the pipeline's and CloudFormation's events: the failed stage and action, each failed resource's logical ID with its status code, each failed smoke test's ID, and the last lifecycle stage an instance reached.
- [ ] A logical ID, a smoke test's ID and a lifecycle stage are kept only when they are in the allow-list the caller supplies for them; a status code only when it is a member of the module's enumeration of CloudFormation's resource status codes.
- [ ] Anything else is dropped and counted, and the count is the only trace of it in the report.
- [ ] The report is encoded as further commit statuses on the same commit, one per fact, each with a context made of the stage's deployment or verification context, a slash and a suffix that tells the facts apart, and a description of at most 140 characters made of those allow-listed parts.
- [ ] The module writes nothing but commit statuses: it has no call that creates a commit comment.
- [ ] `tests/unit/reporter/test_report.py` covers the report and its encoding.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_report.py` passes: a recorded failure of two resources and one smoke test gives one status for the failed stage and action, two for the resources and one for the test, with the right names and codes, each under its own context.
- [ ] `tests/unit/reporter/test_report.py::test_free_text_never_leaves` feeds events whose reason fields hold an account number, an ARN and an address, all built in the test, and finds none of them in any status.
- [ ] `tests/unit/reporter/test_report.py::test_a_name_outside_the_allow_lists_is_dropped` gives a failed resource whose logical ID is not in the supplied template, and a smoke test ID that is not in the supplied list, and finds both counted and absent from every status.

## References

- `docs/design/system-design.md`, section 8.4
- `docs/adr/0001-software-factory.md`, amendment 4
