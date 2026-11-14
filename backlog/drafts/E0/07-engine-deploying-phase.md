+++
id = "E0-07"
title = "Engine: wait for the deployment verdict before an issue is done"
labels = ["size:L", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

Today the engine's only external verdict is the `required` check of a pull request. A merged change
is then deployed by the pipeline, and nothing tells the loop how that went. ADR 0001, decision 5,
closes that: the pipeline posts a commit status on each merge commit, `deploy/<stage>` and, once
smoke tests exist, `verify/<stage>` (issues E0-09 to E0-12), and the engine reads them in a
`deploying` phase between `merged` and the evidence step. With contexts configured, a merge is
accepted only when they report success; a failure takes the existing broken-main path; no verdict
within the timeout makes the issue `waiting`, which the supervisor resumes. The landing claim is
held through the wait, so one merge maps to one deployment and one verdict. The contexts list is
empty until the pipeline reports, and then nothing waits.

Turning the contexts on is the owner's: `tools/implementation-loop/loop.toml` is on the safety
surface, and `docs/factory/green-light.md`, step 11, has the owner list the contexts through a pull
request of their own once statuses appear on merge commits (issue E0-08 carries that obligation).
This issue adds the table with neutral values.

**A deployment that waits for a person.** The stage is deployed behind the approval CDK Pipelines
asks for when a change widens permissions (section 8.4, control 6), and section 8.6 expects it
often in phases 1 to 3. While the pipeline waits there, the reporter posts the deployment context
as pending with a description of its own (issue E0-09). Holding the landing claim through that
pause would stop all landing until the owner acts, so the engine releases the claim and the issue
waits for the owner instead, with a kind of its own that the owner sees in `status`. Executions run
queued, one per commit, so a later merge's execution waits behind the paused one and still gets its
own verdict. The engine learns that description from the key approval_description of the table
deploy, which the owner sets with the contexts. A CodePipeline approval expires after seven days;
an expired or rejected approval is reported as a failure (issue E0-09) and takes the broken-main
path like any other.

Amendment 4 of the ADR replaces the commit comment of decision 5: the deployment report is commit
statuses only. This issue gives the fix-forward a summary that names the failed context and its
state; issue E0-29 adds the reporter's facts to it.

A deployment can take long: a gateway deployment drains for 1800 seconds (section 8.5), and the
pipeline holds deployments from one hour before to one hour after a scheduled session (section
8.2). The timeout only decides when the issue stops polling and waits for the supervisor; it never
fails an issue. Its value here, 3600 seconds, is a starting point the owner may change; the design
gives none.

The scaffolding did not build this phase: the engine has no `deploy_status` and no `deploying`
phase. This issue changes the engine's state machine, its configuration and `loop.toml`, protected
paths on the safety surface (ADR 0001, decision 10); the builder may change the files named below
because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/github.py` gains deploy_status(sha, context), which reads the commit statuses of a commit and returns the latest state and description of that context, and the in-memory GitHub of the tests gains the same.
- [ ] `tools/implementation-loop/loop.toml` gains a table deploy with the keys contexts, an empty list, timeout_s, 3600, and approval_description, an empty string, and `tools/implementation-loop/implement_loop/config.py` reads it and refuses any other key in that table.
- [ ] `tools/implementation-loop/implement_loop/engine.py` and `tools/implementation-loop/implement_loop/model.py` add a phase deploying between merged and the evidence step, which holds the landing claim until every configured context has a final state, except while the deployment waits for an approval as stated below.
- [ ] With an empty contexts list the phase is passed at once and nothing waits.
- [ ] Success of every configured context is recorded as evidence of the issue, with the commit and the contexts.
- [ ] A failure or an error of a configured context takes the existing broken-main path, one fix-forward then a revert pull request, and the fix-forward's feedback names the failed context and its state.
- [ ] When a configured context's latest status is pending with a description equal to a non-empty approval_description, and no configured context failed, the engine releases the landing claim and the issue waits with a new kind owner_approval, defined in `tools/implementation-loop/implement_loop/model.py`, which has no timeout of its own and which the supervisor resumes; resumed, the issue reads the statuses of the same merge commit again.
- [ ] A context still pending with any other description, or absent, when timeout_s has passed makes the issue waiting, which the supervisor resumes; the wait starts again from the same merge commit and never merges twice.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_deploying.py` covers the phase.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_deploying.py` covering success, failure, no verdict at the timeout, and an empty contexts list, and with no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_deploying.py::test_the_landing_claim_is_held` shows that a second issue does not land while the first waits for its verdict.
- [ ] `tools/implementation-loop/tests/test_deploying.py::test_an_approval_releases_the_claim` shows a pending status with the configured approval description leaving the issue waiting owner_approval, a second issue landing meanwhile, and the first issue ending done once the fake GitHub's status turns to success.
- [ ] `tools/implementation-loop/tests/test_deploying.py::test_a_resumed_wait_does_not_merge_again` resumes an issue that waited for its verdict and finds one merge.

## References

- `docs/adr/0001-software-factory.md`, decision 5 and amendment 4
- `docs/design/system-design.md`, sections 8.2, 8.4, 8.5, 8.6
- `docs/factory/green-light.md`
