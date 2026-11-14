+++
id = "E2-64"
title = "The pipeline verifies each deployment against the deployed endpoint"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-10", "E2-09", "E2-12", "E2-40", "E2-42", "E2-43", "E2-52", "E2-56", "E2-57", "E2-63", "E2-72", "E2-90"]
+++

## Context

Step 6 of the pipeline (section 8.2): after the stage is deployed, a CodeBuild step runs smoke tests
against the deployed endpoint with a canary roster entry: health, rejection without a key, the
forbidden routes, a full wake, hold and completion on the mock pool, metrics present. The tests are
issue E2-63; the canary entry and its secret, rhi/canary-key, are made by the admin CLI (issue
E2-56). The pipeline's verification also asserts that the served release equals the deployed one
(section 4.3), which the deployed-only smoke tests compare from the lifecycle agent's identity metric
and the pool's release parameter. The deployed mock pool's engine reads its pool's secret at start
(issue E2-43). A failed verification fails the pipeline; the fix is a forward fix or a revert
through the same pipeline (section 8.5). Its result is the verification status of the merge commit.

**Which action is the verification.** In each stage, the verification is the post-deployment step
whose action is named Verify: issue E0-09 maps an action of that name to the context
`verify/<stage>`, so this step's action carries that name, and a test holds it.

**How the step learns about the stage.** The step needs the endpoint's address, the Prometheus
workspace's query address and each pool's release parameter. The stacks that hold them declare each
as a stack output, and the step receives those outputs as environment variables, through CDK
Pipelines' outputs of a deployed stack: nothing is looked up, and the pipeline's own template holds
no value of the configuration. While https_listener is false the gateway stack still declares the
endpoint's output, with the value none, so that the step's inputs never change shape. The variables
are the ones the smoke tests read (issue E2-63). The step also receives the pipeline's execution ID,
from the pipeline's execution-ID variable. Issues E0-21 and E0-26 pass their own values to the step
the same way. Outputs are not resources, so declaring them brings no cdk-nag finding to the three
stacks and changes none of their acknowledgement files.

**When no test runs.** Two states of a correct deployment leave nothing to test. While https_listener
is false (issue E2-47) the endpoint has no listener. And the owner runs the canary command once
(issue E2-56), but a merge can deploy before that: while the secret rhi/canary-key does not exist or
holds no key, there is no key to test with. In either state the script runs no test and exits 0, and
the step's post-build phase writes, in the pipeline's artifact bucket, the object
verification/<execution ID>/not-run, whose one line is the reason: listener-off or canary-missing.
The verification is then reported as not run, never as passed or failed. Reporting it so is the
reporter's (issue E0-21), which reads under verification/ and nowhere else; a failing step would
revert a good merge, and a pending one would hold the engine's landing claim until the owner acts.
The same prefix later holds the IDs of the failed smoke tests (issue E0-21) and the last lifecycle
stages (issue E0-26).

The reporter names the IDs of failed smoke tests in its failure report, from allow-listed parts only
(section 8.4). Each smoke test already carries the stable ID that issue E2-63 gives it and lists in
its list of IDs; this step adds no test and no ID, and deselects tests by those IDs. Keeping the IDs
of the failed tests where the reporter reads them, and naming them in the failure report, is issue
E0-21, which follows this one and extends this step: nothing of the reporter is built here.

The step runs the tests against the deployed stack; it builds no image. The step runs outside the
private network. The smoke tests that call the forbidden routes through the internal load balancer
cannot run from there, and section 6.5 lets only the gateway reach that load balancer; how the
verification reaches it is left to the owner, so this step deselects those tests by their IDs. The
forbidden routes at the gateway's hop run. The same tests on the coursework pool, when a change
touches the engine image, the machine image or a pool, are issue E3-12; the weekly canary is issue
E3-13.

This issue changes `src/rhi/pipeline/` and `tests/policy/`, both protected paths (ADR 0001, decision
10 and amendment 1); the builder may change the files named below because this issue names them. An
acknowledgement's key under `[nag]` is the exact cdk-nag finding id (issue E1-05).

## Deliverables

- [ ] `src/rhi/pipeline/verify.py` defines a step whose action is named Verify and that runs `scripts/pipeline/verify.py` against the deployed stage, and `src/rhi/pipeline/stack.py` runs it after the stage's deployment.
- [ ] The step receives the endpoint's address, the workspace's query address and each pool's release parameter's name as environment variables from stack outputs that `src/rhi/gateway/stack.py`, `src/rhi/observability/stack.py` and `src/rhi/pools/stack.py` declare, and the pipeline's execution ID from its execution-ID variable.
- [ ] `scripts/pipeline/verify.py` reads the canary's key from the secret rhi/canary-key, runs the smoke tests, the deployed-only ones included, deselects by their IDs those that must reach the internal load balancer, and exits non-zero when one fails, which fails the step and the execution.
- [ ] While https_listener is false, or while the secret rhi/canary-key is missing or holds no key, the script runs no test and exits 0, and the step's post-build phase writes in the pipeline's artifact bucket the object verification/<execution ID>/not-run, whose one line is listener-off or canary-missing.
- [ ] The step's role may read the secret rhi/canary-key, query the Prometheus workspace, read the pools' release parameters and write under the prefix verification/ of the pipeline's artifact bucket; nothing else.
- [ ] Every cdk-nag finding `src/rhi/pipeline/verify.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_verify.toml` or `tests/policy/acknowledged/pipeline_stack.toml`.
- [ ] `tests/policy/test_pipeline.py` records the new step and its role, `tests/synth/test_pipeline_verify.py` checks the step, its action's name and its inputs, and `tests/unit/test_verify_script.py` checks the script.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline_verify.py tests/policy/test_pipeline.py` passes: the verification is the action named Verify, after the deployment, and its post-build phase copies the script's reason, when there is one, to the key verification/<execution ID>/not-run; its inputs are outputs of the three stacks and the execution ID; and its role reads one secret and writes under verification/ only.
- [ ] `uv run --locked pytest tests/unit/test_verify_script.py` passes: with no canary key it runs no test, exits 0 and leaves the reason canary-missing for the post-build phase; with the endpoint's output reading none, the reason listener-off; with a key it deselects the tests that call the forbidden routes through the internal load balancer and no other; and a failed test makes it exit non-zero.
- [ ] In the account, the verification passes: `tests/smoke/test_smoke.py` passes against the deployed endpoint with a canary key, so a key works end to end against the mock pool with a completed stream in each wire format, and, on a run that found the pool cold, a wake and a hold, which is the first exit criterion of phase 1 (needs aws:deployed-environment).
- [ ] In the account, the scaler's scenarios pass in the synth step, and the forbidden-route tests at the gateway's hop pass in the verification while those through the internal load balancer stay deselected, which is the second exit criterion of phase 1 as far as this step can reach (needs aws:deployed-environment).
- [ ] The owner decides how the verification reaches the internal load balancer to call the forbidden routes through it, which section 6.5 lets only the gateway reach (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 6.5, 8.2, 8.4, 8.5, 14
