+++
id = "E0-12"
title = "Reporter: an EventBridge rule and a function in the pipeline stack"
labels = ["size:M", "kind:infra"]
epic = "E0"
blocked_by = ["E0-09", "E0-10", "E0-11", "E1-10", "E1-16"]
+++

## Context

Step 7 of the pipeline (section 8.2): an EventBridge rule on the pipeline's state invokes a function
that posts a commit status to GitHub. With it, every merge commit ends with a verdict the factory
can read, which is the second exit criterion of phase 0. The failure report checks the logical IDs
it names against that commit's own synthesized template (section 8.4), so the function reads the
templates the same execution synthesized. The smoke tests do not exist yet in phase 0: the handler
gives the report empty allow-lists for smoke test IDs and lifecycle stages, and issues E0-21 and
E0-26 fill them. The deployed check needs a pipeline that deploys, which issue E1-10 completes. The
function's asset is built from `uv.lock` without Docker by the packaging module of issue E1-16.

**The reporter's secret: referred to, never created.** The credential is the status reporter App of
section 8.6, step 3, whose private key only the owner holds. The stack refers to the secret by a
fixed name, rhi/reporter/github-app, kept as one constant of `src/rhi/pipeline/reporter.py`, and
does not create it. The owner creates it, with the JSON fields app_id, installation_id and
private_key that the client of issue E0-10 reads, through the walkthrough of issue E1-18 or by hand.
This is a choice: a secret the stack created would hold a generated placeholder the handler would
have to tell from a real value, and it would be a stateful resource of the pipeline stack whose
loss on a rename voids the owner's value. Referring to it by name lets the stack deploy cleanly
before the owner has stored anything.

**While the secret is absent**, or lacks one of its fields, the handler posts no status, logs one
fixed line that says so, and returns without an error, so that EventBridge does not retry. A merge
commit then simply has no status. Nothing waits on that: the engine's list of deployment contexts
stays empty until the owner, having seen statuses on merge commits, fills it (issue E0-08).

The function receives the secret's name in RHI_REPORTER_SECRET and the repository, as owner/name
from the configuration key of issue E1-03, in RHI_REPORTER_REPOSITORY (issue E0-10). No new
configuration key is added.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pipeline/reporter.py` adds to the pipeline stack an EventBridge rule on this pipeline's execution, stage and action state changes and a function that handles them, `src/rhi/pipeline/stack.py` builds it, and `tests/synth/test_reporter.py` checks the rule.
- [ ] `services/reporter/rhi_reporter/handler.py` turns an event into statuses with the status module, the report module and the client, and gives the report the logical IDs of the templates that the same execution synthesized, read from the pipeline's artifact store, and empty allow-lists for smoke test IDs and lifecycle stages; `tests/unit/reporter/test_handler.py` covers it.
- [ ] `src/rhi/pipeline/reporter.py` refers to the reporter's secret by the name rhi/reporter/github-app, kept as one constant, without creating it, and gives the function that name in RHI_REPORTER_SECRET and the configured repository in RHI_REPORTER_REPOSITORY.
- [ ] While the secret is absent or lacks one of its three fields, the handler posts no status, logs one fixed line that names neither the secret's value nor AWS's message, and returns without an error.
- [ ] The function's role may read the reporter's one secret, the events of this pipeline and of its stacks, and the synthesized templates of this pipeline's executions, and nothing else; `tests/policy/test_pipeline.py` fails when it may do more.
- [ ] Every cdk-nag finding `src/rhi/pipeline/reporter.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_reporter.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_handler.py` passes: a recorded sequence of events for a successful and for a failed execution gives the expected statuses, in order, on a stub client, and the failed one names only logical IDs of the templates the stub artifact store holds for that execution.
- [ ] `tests/unit/reporter/test_handler.py::test_no_secret_no_status` shows an absent secret, and one without private_key, leaving the stub client without a call and the handler returning normally.
- [ ] `uv run --locked pytest tests/synth/test_reporter.py tests/policy/test_pipeline.py` passes: the rule matches this pipeline only, the stack creates no secret, and the role is as narrow as stated.
- [ ] The owner creates the secret rhi/reporter/github-app with the status reporter App's ID, its installation ID on this repository and its private key, in the fields app_id, installation_id and private_key (needs aws:account).
- [ ] After a merge to `main`, the merge commit shows the deployment status of the stage with the pipeline's result, which is the second exit criterion of phase 0 (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2, 8.4, 8.6, 14
- `docs/adr/0001-software-factory.md`, amendments 1 and 4
