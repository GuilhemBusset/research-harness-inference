+++
id = "E0-34"
title = "Implement loop: discharge a delivered issue's smoke-test items from a later verification on main, without waiting for it"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = 2
+++

## Context

An item that needs `aws:deployed-environment` and names a smoke test is proven by the pipeline's
verification, which runs the smoke tests of the commit it deployed against the deployed endpoint
(ADR 0001, decision 6). The engine already knows how, but only synchronously: `_verified_discharged`
in `tools/implementation-loop/implement_loop/engine.py` reads the verdict the deploying phase
recorded on the issue's merge commit, and the deploying phase exists only when `[deploy] contexts`
of `tools/implementation-loop/loop.toml` is set, which holds the merge queue until the pipeline
answers. That waiting gate (pull request #524) stays off: `contexts` stays empty. So today every
such item is left to the owner, and the issue stays `delivered`.

This issue adds the same proof after the fact, without holding anything. A new optional key of
`[deploy]`, `discharge_contexts`, lists verification contexts (`verify/<stage>`) the engine reads
only to discharge, and never waits for. On each run, for each issue of the run's plan that is
`delivered` and holds an obligation for `aws:deployed-environment` on an item that names a test
file under `tests/smoke/`, the engine reads each discharge context's status on the issue's merge
commit (its pull request's merge commit as GitHub records it), and, when that is not a success with
the verified description, on the head of `main` the run fetched, if that commit's history contains
the merge commit. A status discharges the item when all of what `_verified_discharged` requires
holds on that commit: `verified_description` is set and the status succeeded with exactly it;
`unverified_tests` is given and names none of the item's tests; the item's text ends with no other
capability; and every test it names is a file of that commit. The engine then records the status
as the item's evidence (the commit status kind it already has), adds its row to the evidence
comment of the issue's pull request, ticks the item's box with a link to that comment, and removes
the obligation; `_reconcile_delivered` closes the issue as done once every box is ticked. Anything
less discharges nothing, and the obligation stays the owner's. A read GitHub does not answer
discharges nothing and is tried again on the next run.

**The values.** `discharge_contexts = ["verify/Rhi"]`, the stage the pipeline deploys;
`verified_description` is the reporter's `VERIFIED_DESCRIPTION`
(`services/reporter/rhi_reporter/status.py`); `unverified_tests` names the smoke test files of which
the verification's own run leaves a test out (`scripts/pipeline/verify.py`), today
`tests/smoke/deployed/test_gpu_release.py` alone. `tests/unit/test_deploy_settings.py` already
holds the descriptions equal to the reporter's constants and `unverified_tests` equal to what the
verification leaves out; it also holds each discharge context to a `verify/<stage>` context the
reporter posts. `contexts` and `approval_description` keep their values.

**Two engines meet.** `load_deploy` refuses a key it does not know, so the key and the code that
reads it land in one commit. As `_verified_discharged` reads its keys, the new code reads the new
field with a default, so that this `engine.py` beside an older `config.py` discharges nothing. The
change is a local patch of the vendored engine: `tools/implementation-loop/UPSTREAM.md` records it,
as it records the synchronous discharge.

This issue changes `tools/implementation-loop/`, a protected path on the safety surface (ADR 0001,
decision 10), and names each file it changes in its Deliverables.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/config.py` gives `Deploy` the field discharge_contexts, empty by default, and `load_deploy` reads it as a list of contexts of the form verify/<stage>, refusing any other type or form.
- [ ] `tools/implementation-loop/implement_loop/engine.py` discharges, on each run and for each delivered issue of the plan, the obligations described in the Context from a discharge context's status on the merge commit or on the head of `main` whose history contains it, ticks the item, records the evidence and the event obligations_discharged, and never enters the deploying phase, holds the merge queue or waits for a discharge context.
- [ ] Every change to the engine is under `tools/implementation-loop/`, and its GitHub client makes no kind of write it did not make before: the discharge edits an issue's body, a pull request's evidence comment and an issue's state, as the evidence step does.
- [ ] `tools/implementation-loop/loop.toml` sets discharge_contexts to verify/Rhi, verified_description to the reporter's VERIFIED_DESCRIPTION and unverified_tests to the file of the GPU-only smoke test alone, the one the verification's run on the mock pool leaves a test out of, keeps contexts empty, and its comments describe the new key.
- [ ] `tools/implementation-loop/tests/test_discharge.py` covers the discharge against the fake GitHub, and the tests of `tools/implementation-loop/tests/test_deploying.py` that held the committed table to its neutral values hold it to the new ones.
- [ ] `tests/unit/test_deploy_settings.py` checks that each discharge context is a verify/<stage> context the reporter posts, and still holds the committed descriptions and unverified_tests to the reporter and the verification.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the local change, file by file, with its reason, and the delivered row of `docs/factory/operating.md` says that a later verification can discharge an owner item that names a smoke test.

## Acceptance criteria

- [ ] `tools/implementation-loop/tests/test_discharge.py::test_no_merge_waits_on_a_discharge_context` passes: with discharge_contexts set and contexts empty, an issue merges, its evidence is recorded and the next issue lands, with no deploying phase, no hold of the merge queue and no status read before the issue is delivered.
- [ ] `tools/implementation-loop/tests/test_discharge.py::test_a_later_verification_discharges_and_the_issue_is_done` passes: a delivered issue whose only obligation is such an item, whose merge commit later carries a success of verify/Rhi with the verified description, has its box ticked with the evidence on the next run and ends done and closed; and so does one whose merge commit has no such status while the head of main, whose history contains it, has one.
- [ ] `tools/implementation-loop/tests/test_discharge.py::test_what_discharges_nothing` passes: a success with another description, a pending or failed status, an item naming a test among unverified_tests, an item naming a test the commit does not hold, an item with another capability, a context not among discharge_contexts, a success on a commit whose history does not contain the merge commit, and an unanswered read each leave the obligation and the box as they were.
- [ ] `scripts/check.sh engine-tests` passes.
- [ ] `uv run --locked pytest tests/unit/test_deploy_settings.py` passes with the committed table.
- [ ] After this merge, a delivered issue whose remaining item names a smoke test that the next green verification of `main` runs is ticked and closed by the following run of the loop (needs aws:deployed-environment).

## References

- `docs/adr/0001-software-factory.md`, decisions 5, 6 and 10
- `tools/implementation-loop/implement_loop/engine.py`
- `tools/implementation-loop/tests/test_deploying.py`
- `tests/unit/test_deploy_settings.py`
- `services/reporter/rhi_reporter/status.py`
