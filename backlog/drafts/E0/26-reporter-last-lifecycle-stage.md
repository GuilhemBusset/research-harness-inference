+++
id = "E0-26"
title = "Reporter: name the last lifecycle stage an instance reached when a verification fails"
labels = ["size:M", "kind:infra"]
epic = "E0"
blocked_by = ["E0-21", "E2-37", "E2-64"]
+++

## Context

The failure report of section 8.4 names, among its allow-listed parts, the last lifecycle stage an
instance reached. It is what tells the factory, which holds no AWS credentials, where a start that
never served stopped: at the release check, the image, the weights, the warm-up. The report module
takes the allow-list of stages from its caller (issue E0-11), and the reporter's handler gives it an
empty one (issue E0-12).

Issue E2-37 names the stages of a start as an enumeration, in the lifecycle agent's stages module
(`services/engine/lifecycle/rhi_lifecycle/stages.py`), and puts the last stage reached in each
instance's heartbeat row. Issue E0-21 has the verification step write its marks, in its post-build
phase, to the pipeline's artifact bucket under verification/<execution ID>/, because a failed
CodeBuild action may leave no output artifact; the handler reads them from there. This issue adds
the mark last-stages to that prefix: on a failed verification, the step reads the heartbeat rows of
the pools it tested and writes the last stage each instance reached, one per line, and the handler
names them in the report, keeping only the members of E2-37's enumeration. Nothing else of a row
reaches a status: no instance ID, no address.

**How the step addresses the heartbeat table.** The table is created by `src/rhi/pools/tables.py`
in the pools stack of the stage (issue E2-49), and a CDK Pipelines step cannot hold a reference to a
resource of the stage it follows: the pipeline stack and the stage are separate assemblies, and CDK
refuses a reference between them. An exported value read with an import would not do either: the
pipeline updates itself before the stage deploys, so its first update would wait for an export that
does not exist yet. So:

- the table's name reaches the step at run time the way the pools' release parameters do (issue
  E2-64): the pools stack declares it as a stack output, and the step receives it as an environment
  variable through CDK Pipelines' outputs of a deployed stack;
- the role's permission names the table by an ARN that the pipeline stack builds at synthesis: its
  own partition, region and account, which are the stage's (the stage names no environment, issue
  E1-12), and the name CloudFormation gives a table that has no name of its own, the stack's name, a
  hyphen, the table's logical ID, a hyphen and a generated suffix. The pipeline stack takes the
  pools stack's name and the heartbeat table's logical ID from the stage it builds. That logical ID
  is fixed and recorded (issues E2-49 and E2-88), and logical IDs hold no hyphen, so the pattern
  matches that one table.

The step may read heartbeat rows by pool, and nothing else of the table.

**How the reporter's function gets the allow-list.** The function's bundle holds the reporter's
package and `rhi_common` only (issue E1-16), so the enumeration reaches it as data, as the smoke
test IDs do (issue E0-21): the construct imports the stages module at synthesis, which the app can
do because it puts every package root on its import path (issue E1-04), and gives the function the
members' names in the environment variable RHI_LIFECYCLE_STAGES, separated by commas.

**Acknowledgements.** The step's project and role are created by CDK Pipelines inside the pipeline
construct of `src/rhi/pipeline/stack.py`, so the new statement is acknowledged in
`tests/policy/acknowledged/pipeline_stack.toml` if cdk-nag reports it, beside the files of the
step's and the reporter's modules.

This issue changes `src/rhi/pipeline/`, `tests/policy/test_pipeline.py` and
`tests/policy/acknowledged/`, protected paths (ADR 0001, decision 10 and amendment 1); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pipeline/verify.py` and `src/rhi/pipeline/stack.py` have the verification step, when its tests failed, write in its post-build phase verification/<execution ID>/last-stages to the pipeline's artifact bucket: the last lifecycle stage reached by each instance of the pools it tested, read from their heartbeat rows, one stage name per line and nothing else.
- [ ] `src/rhi/pools/stack.py` declares the heartbeat table's name as a stack output, which the step receives as an environment variable, and the step's role may read the table's items by pool, on the ARN built at synthesis from the pipeline stack's partition, region and account and the name pattern of Context, and gains nothing else; `tests/policy/test_pipeline.py` holds it to that.
- [ ] `services/reporter/rhi_reporter/handler.py` reads last-stages for a failed verification and gives the report those stages, with the names that the environment variable RHI_LIFECYCLE_STAGES lists as their allow-list; a missing object or variable gives an empty list.
- [ ] `src/rhi/pipeline/reporter.py` gives the function, in RHI_LIFECYCLE_STAGES, the names of the members of the lifecycle agent's stages enumeration, read at synthesis and separated by commas, and its role is as narrow as before.
- [ ] Every cdk-nag finding `src/rhi/pipeline/verify.py`, `src/rhi/pipeline/reporter.py`, `src/rhi/pipeline/stack.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pipeline_verify.toml`, `tests/policy/acknowledged/pipeline_reporter.toml`, `tests/policy/acknowledged/pipeline_stack.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/unit/reporter/test_handler.py`, `tests/synth/test_pipeline_verify.py` and `tests/synth/test_reporter.py` cover the change.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/reporter/test_handler.py` passes: a failed verification whose last-stages names the stage weights and one string outside RHI_LIFECYCLE_STAGES gives a status naming weights under the verification context, and the other string only as a count.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_verify.py tests/policy/test_pipeline.py tests/synth/test_reporter.py` passes: the step writes last-stages in its post-build phase, its role may read the heartbeat table and nothing new besides, the function's RHI_LIFECYCLE_STAGES equals the members of the stages enumeration, and the reporter's role is as narrow as before.
- [ ] `tests/synth/test_pipeline_verify.py::test_the_heartbeat_grant_names_one_table` shows the role's table ARN naming the table as the pools stack's name, a hyphen, the heartbeat table's logical ID in the synthesized pools template, a hyphen and a wildcard, and the name CloudFormation would give any other table of the stage's templates not matching it.
- [ ] After a deployment whose verification fails because an instance never served, the merge commit carries a status that names the last stage that instance reached (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 8.4
- `docs/adr/0001-software-factory.md`, amendment 4
