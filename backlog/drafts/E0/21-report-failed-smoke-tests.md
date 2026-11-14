+++
id = "E0-21"
title = "Reporter: name the failed smoke tests of a verification in its failure report"
labels = ["size:M", "kind:infra"]
epic = "E0"
blocked_by = ["E0-12", "E2-63", "E2-64"]
+++

## Context

The failure report of section 8.4 names, among its allow-listed parts, the IDs of failed smoke
tests. The report module takes their allow-list from its caller (issue E0-11), and the reporter's
handler gives it an empty one until the smoke tests exist (issue E0-12). Issue E2-63 gives every
smoke test a fixed ID, listed in `tests/smoke/ids.py`, and issue E2-64 runs them as the pipeline's
verification step, the post-deployment step whose action is named Verify (issue E0-09). This issue
joins the two: the verification step keeps the IDs of the tests that failed where the reporter can
read them, and the handler names them in the report of a failed verification, under the stage's
verification context.

**Where the marks are kept, so that a failure does not lose them.** A CodeBuild action that fails
may leave no output artifact in the pipeline, which is exactly the case this issue is for. So the
step does not rely on its output artifact. CodeBuild runs a build's post-build phase whether its
build phase passed or failed; in that phase the step writes its marks itself, to the pipeline's
artifact bucket, under verification/<pipeline execution ID>/, the execution's ID given to the step
as the pipeline's execution-ID variable. Issue E2-64 already does so for not-run, and gives the
step's role its write under that prefix; this issue adds failed-smoke-tests beside it. The reporter
receives the execution's ID with the event and reads the objects under that prefix. These are the
only places the marks are kept:

| Key under verification/<execution ID>/ | What it holds | Issue |
| --- | --- | --- |
| failed-smoke-tests | the IDs of the smoke tests that failed, one per line | this one |
| last-stages | the last lifecycle stage of each instance of the pools tested, one per line | E0-26 |
| not-run | one line, the reason no test ran: canary-missing or listener-off (issue E2-64), or parked (issue E4-38) | E2-64, E4-38 |

A missing object gives an empty list, or, for not-run, a verification whose tests ran.

**A verification that ran no test is not a verification that passed.** Issue E2-64's step runs no
test, and does not fail, while the canary's key is missing or the endpoint has no listener, and
leaves not-run with the reason. The
engine discharges an owner item about the deployed system only on the description
VERIFIED_DESCRIPTION of the status module (issues E0-08 and E0-09). So when not-run is present, the
handler posts the verification context as success with a description that says no test ran and
why, one per known reason, and a description of its own for a reason it does not know (the parked
reason before issue E4-38 adds its description); none equals VERIFIED_DESCRIPTION.

**How the reporter's function gets the allow-list.** The handler keeps only the IDs that
`tests/smoke/ids.py` lists, so a test's message, an address or a host name never reaches a status.
The function's bundle holds the reporter's package and `rhi_common` only (issue E1-16), and the
handler must not import from the tests tree, so the list reaches it as data: the construct reads
the IDs from `tests/smoke/ids.py` at synthesis and gives them to the function in the environment
variable RHI_SMOKE_TEST_IDS, separated by commas. A change to the list then changes the function's
configuration, and the same deployment carries it. The report's other outside part, the last
lifecycle stage an instance reached, is issue E0-26's: the handler keeps giving an empty allow-list
for it.

**Acknowledgements.** The verification step's CodeBuild project and role are created by CDK
Pipelines inside the pipeline construct of `src/rhi/pipeline/stack.py`, so what this issue adds to
that role is acknowledged in `tests/policy/acknowledged/pipeline_stack.toml`, beside the files of
the step's and the reporter's modules, as issue E2-64 does.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them. It also extends E2-64's synthesis
test of the step.

## Deliverables

- [ ] `src/rhi/pipeline/verify.py` and `src/rhi/pipeline/stack.py` have the verification step write in its post-build phase, to the pipeline's artifact bucket, verification/<execution ID>/failed-smoke-tests, the IDs of the smoke tests that failed, one per line and nothing else, beside the not-run mark of issue E2-64; the step's role keeps the write under the prefix verification/ that E2-64 gave it and gains nothing else.
- [ ] `services/reporter/rhi_reporter/handler.py` reads failed-smoke-tests for a failed verification of that execution and gives the report those IDs, with the IDs that the environment variable RHI_SMOKE_TEST_IDS lists as their allow-list; a missing object or variable gives an empty list.
- [ ] `src/rhi/pipeline/reporter.py` gives the function, in RHI_SMOKE_TEST_IDS, the IDs that tests/smoke/ids.py lists, read at synthesis and separated by commas, and lets its role read under the prefix verification/ of the artifact bucket and nothing more than before; `tests/policy/test_pipeline.py` holds both roles to that.
- [ ] For a verification that passed and left not-run, the handler posts the verification context as success with a description of `services/reporter/rhi_reporter/status.py` that says no test ran because of canary-missing, another for listener-off, and a third for any other reason, none equal to VERIFIED_DESCRIPTION; without not-run it posts VERIFIED_DESCRIPTION.
- [ ] Every cdk-nag finding `src/rhi/pipeline/verify.py`, `src/rhi/pipeline/reporter.py` or `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_verify.toml`, `tests/policy/acknowledged/pipeline_reporter.toml` or `tests/policy/acknowledged/pipeline_stack.toml`.
- [ ] `tests/unit/reporter/test_handler.py`, `tests/unit/reporter/test_status.py`, `tests/synth/test_reporter.py` and `tests/synth/test_pipeline_verify.py` cover the change.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_handler.py` passes: a recorded failed verification whose failed-smoke-tests names two listed IDs and one unlisted string gives one status per listed ID under the verification context and the unlisted string only as a count, and one with no such object gives no smoke-test status.
- [ ] `uv run --locked pytest tests/synth/test_reporter.py tests/policy/test_pipeline.py tests/synth/test_pipeline_verify.py` passes: the step writes failed-smoke-tests in its post-build phase under the prefix verification/, its role may write there and nowhere new, the reporter's role reads that prefix and is otherwise as narrow as before, and the function's RHI_SMOKE_TEST_IDS equals the list of `tests/smoke/ids.py`.
- [ ] `tests/unit/reporter/test_handler.py::test_a_verification_that_ran_nothing_is_not_verified` gives a passed verification whose not-run holds canary-missing, one holding listener-off and one holding parked three descriptions that say no test ran, each different from VERIFIED_DESCRIPTION, and one without not-run VERIFIED_DESCRIPTION.
- [ ] After a deployment whose verification fails, the merge commit carries a status that names the failed smoke test by its ID (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2, 8.4
- `docs/adr/0001-software-factory.md`, amendment 4
