+++
id = "E0-09"
title = "Reporter: map pipeline events to commit statuses from a fixed vocabulary"
labels = ["size:M", "kind:app"]
epic = "E0"
blocked_by = ["E1-02"]
+++

## Context

Agents hold no AWS credentials, so the commit status is their only view of a deployment (section
8.4). The status is public. It carries a state, a fixed context and a description chosen from an
enumerated set, and never text from AWS: error messages contain bucket names, addresses and resource
IDs that no deny-list reliably catches.

This is the pure part of the reporter: a function from an event to a status. It calls nothing. The
failure report (issue E0-11), superseded executions (issue E0-13) and the measurements (issue E0-14)
build on its contexts and states. GitHub refuses a status description longer than 140 characters.

**One exception to "one member of the enumeration".** Section 8.4 has the reporter mark a
superseded commit and name the commit whose execution covers it. A commit SHA is a name from this
repository, not text from AWS, so the vocabulary allows exactly one member to be followed by more:
the superseded member, followed by a space and the covering commit's full SHA, which the module
checks is 40 hexadecimal characters. Every other description is one member, alone.

**The approval on widened permissions** (section 8.4, control 6) pauses the deployment until the
owner acts, often in phases 1 to 3 (section 8.6). The engine releases its landing claim during that
pause (issue E0-07), so the status must say it: a manual approval that waits for a person has a
pending description no other event gives. A CodePipeline approval that is rejected, or that expires
after seven days, ends the execution as a failure, with a description of its own.

**Which action is the verification.** Each stage the pipeline deploys has one post-deployment step
whose action is named Verify: the smoke tests against the deployed endpoint (issue E2-64), which
names its step so. An event of that action reports under verify/<stage>; every other event of the
stage reports under deploy/<stage>. A rule on the name, rather than on the action's category or its
position, holds whatever other steps later issues add to the stage.

**Two descriptions the engine reads by value.** The engine learns from its own configuration which
description means a verification whose tests ran and passed, and which one means an approval waiting
for the owner (issues E0-07 and E0-08), and a test holds each value set there equal to a constant of
this module: VERIFIED_DESCRIPTION, which a success of the action named Verify gives, and
APPROVAL_DESCRIPTION. Later issues add descriptions for a verification that passed without running a
test (issues E0-21 and E4-38); none of them may equal VERIFIED_DESCRIPTION.

## Deliverables

- [ ] `services/reporter/rhi_reporter/status.py` maps a pipeline event (the stage, the action, the action's category, its state, the commit) to a commit status: a state among pending, success, failure and error, a context, and a description.
- [ ] The contexts are fixed, built from the stage's name as the pipeline names it: an event of the action named Verify reports under verify/<stage>, and every other event of the stage, and a failure before the stage's deployment (source, synthesis, self-update, assets), under deploy/<stage>.
- [ ] The description is one member of an enumeration defined in the module, none longer than 140 characters, except that the member for a superseded commit is followed by a space and a full commit SHA of 40 hexadecimal characters, which the module checks; no field of the event other than those five is read, and a status carries no link: no console address ever reaches GitHub.
- [ ] A manual approval that waits for a person maps to the pending state under the deployment context with the description the module names as the constant APPROVAL_DESCRIPTION, which no other event maps to, and one that is rejected or expires maps to the failure state with a description of its own.
- [ ] A success of the action named Verify maps to the success state under verify/<stage> with the description the module names as the constant VERIFIED_DESCRIPTION, which says that the verification's tests ran and passed.
- [ ] `tests/unit/reporter/test_status.py` covers the mapping.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_status.py` passes: every pipeline state the module accepts maps to exactly one status, an unknown state maps to the error state with a fixed description, an event of the action named Verify reports under verify/<stage> with VERIFIED_DESCRIPTION when it succeeds, and an event of any other action of that stage under deploy/<stage>.
- [ ] `tests/unit/reporter/test_status.py::test_nothing_from_aws_is_copied` feeds events whose other fields hold an account number, an ARN and a bucket name, all built in the test, and finds none of them in any status.
- [ ] `tests/unit/reporter/test_status.py::test_an_approval_has_descriptions_of_its_own` shows a waiting approval giving the pending state with APPROVAL_DESCRIPTION, which no other event gives, a rejected one giving the failure state, and the superseded member refusing anything but a 40-character hexadecimal SHA after it.

## References

- `docs/design/system-design.md`, sections 8.2, 8.4, 8.6
