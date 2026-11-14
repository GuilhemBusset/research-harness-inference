+++
id = "E0-08"
title = "Let a verified deployment discharge what its smoke tests decide, and hand the deployment contexts to the owner"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E0-07", "E0-12", "E0-13", "E0-19", "E0-21", "E0-29", "E1-18", "E2-64"]
+++

## Context

The engine waits for a deployment only for the status contexts listed in the table deploy of
`tools/implementation-loop/loop.toml` (ADR 0001, decision 5; the phase itself is issue E0-07), and
that list is empty until the pipeline reports. Once the reporter posts `deploy/<stage>` (issue
E0-12) and the pipeline's verification step posts `verify/<stage>` (issue E2-64), the list can name
them.

**Who turns them on.** `loop.toml` is on the safety surface, and `docs/factory/green-light.md`,
step 11, has the owner set the contexts through a pull request of their own, after listing what
the pipeline actually posts on a merge commit. That is the owner's item below; the loop never fills
the list. The owner does it only once the conditions that keep a context from staying pending for
ever hold: the reporter's secret is in Secrets Manager (issue E1-18), a superseded execution gets a
verdict (issue E0-13), and the deployment approval releases the landing claim (issue E0-07). The
blockers of this issue are there so that the owner's item appears only after them.

ADR 0001 also asks to move AWS-only items from the owner to status contexts as smoke tests appear
(decision 6, Consequences). An acceptance item that needs aws:deployed-environment is classified
`owner` by the engine, whatever a prompt says, so the move has to be mechanical. The verification
step runs every smoke test, the deployed-only ones under `tests/smoke/deployed/` included, against
the deployed endpoint (section 8.2, step 6), and a merge is accepted only once its verification
context reports success. So an item that needs aws:deployed-environment and names a test under the
folder tests/smoke/ is proven by that success, when the verification's description says its tests
ran and passed. A verification can also succeed without running them: one that found no canary
key or no listener has a description of its own (issue E0-21), and so has the parked state (issue
E4-38). So the engine compares the description with the key verified_description of the table
deploy, which the owner sets with the contexts. The reporter's status module names the description
of a verification whose tests ran and passed as one constant, VERIFIED_DESCRIPTION, and the pending
description of an approval that waits for the owner as another, APPROVAL_DESCRIPTION (issue E0-09).
The engine does not import the reporter's code; `tests/unit/test_deploy_settings.py` holds each
description set in `loop.toml` equal to its constant instead, so that a typo, or another member of
the vocabulary, in the owner's pull request fails CI. This issue waits for E0-21 so that, by the
time the owner sets the contexts, a verification that ran no test can no longer post the
description of one whose tests passed. Issue E0-19 adds the same kind of discharge for container tests, from a CI job's check run;
this issue reuses its mechanism with a commit status of the merge commit as the source.

The stage's name is the one the pipeline stack of `src/rhi/pipeline/` deploys. A gateway deployment
takes 1800 seconds to drain (sections 8.5 and 17); the timeout only decides when the issue stops
polling and waits for the supervisor.

This issue changes the engine and `loop.toml`, protected paths on the safety surface (ADR 0001,
decision 10); the builder may change the files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/loop.toml` gains, in its table deploy, the key verified_description, an empty string, which `tools/implementation-loop/implement_loop/config.py` reads; the values of contexts, timeout_s and approval_description are left as they stand.
- [ ] `tools/implementation-loop/implement_loop/engine.py` discharges, after the deploying phase, an obligation that needs aws:deployed-environment and whose item names a test under the folder tests/smoke/, when a configured context of the form verify/<stage> succeeded on the merge commit with a description equal to a non-empty verified_description: the item is recorded as proven with that status as its evidence, and is not listed for the owner; every other obligation, and every obligation when that condition fails, is handled as before.
- [ ] `tools/implementation-loop/prompts/plan.md` tells the builder that such an item is proven by the deployment verdict, and that an item about the deployed system should name the smoke test that decides it.
- [ ] `tests/unit/test_deploy_settings.py` reads the table deploy of `tools/implementation-loop/loop.toml` and fails when a context is not of the form deploy/<stage> or verify/<stage>, when a non-empty verified_description differs from the constant VERIFIED_DESCRIPTION of the reporter's status module, or when a non-empty approval_description differs from its constant APPROVAL_DESCRIPTION.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_deploying.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_deploying.py` passing on a deploy table built in the test, and with no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_deploying.py::test_a_verified_deployment_discharges_a_smoke_test_item` shows an issue whose only obligation names a test under tests/smoke/ ending done once the fake GitHub's verification status succeeds on the merge commit with the configured description.
- [ ] `tools/implementation-loop/tests/test_deploying.py::test_only_a_verified_smoke_test_item_is_discharged` shows a smoke-test item staying the owner's when the verification succeeded with another description, such as the one for a parked system, and an item that names no smoke test, or that needs gpu:capacity, staying the owner's.
- [ ] `uv run --locked pytest tests/unit/test_deploy_settings.py` passes on `tools/implementation-loop/loop.toml` as it stands, and its tests fail a deploy table, built in the test, whose verified_description is another member of the reporter's vocabulary than VERIFIED_DESCRIPTION, such as the description of a verification that ran no test, or whose context has another form.
- [ ] The owner lists in the table deploy of `tools/implementation-loop/loop.toml`, through a pull request of their own, the deployment and verification contexts the pipeline posts, a timeout_s of at least 1800, and the descriptions APPROVAL_DESCRIPTION and VERIFIED_DESCRIPTION of the reporter's status module (needs human:owner-decision).
- [ ] After a merge, the engine's run waits for the two statuses of the merge commit and closes the issue on their success (needs aws:deployed-environment).

## References

- `docs/adr/0001-software-factory.md`, decisions 5 and 6, amendment 4 and Consequences
- `docs/design/system-design.md`, sections 8.2, 8.4, 8.5, 8.6, 17
- `docs/factory/green-light.md`
