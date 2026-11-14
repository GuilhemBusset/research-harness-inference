+++
id = "E1-19"
title = "The pipeline invariant: a reviewed list of steps, and rules for assets, deployments, triggers and the boundary"
labels = ["size:M", "kind:test"]
epic = "E1"
blocked_by = ["E1-07", "E1-08"]
+++

## Context

`tests/policy/test_pipeline.py` is the invariant on the pipeline's own steps and roles (section 8.4,
control 2): a change to what the pipeline runs, or to what its roles may do, shows up as a change to
this protected file, which the examiner reviews. The design also forbids two things on a public
repository: CodeBuild triggered by a pull request, which would run a stranger's code with a role in
the owner's account (section 8.3), and any trigger other than a push to `main` (section 8.2). Every
role a deployment creates carries the permissions boundary of the bounded bootstrap (issue E1-08).

**What the reviewed list holds, and what rules hold instead.** The list holds, in order, the
pipeline's stages and every action that is neither an asset-publishing action nor a stack's
deployment action: the source, the synth step, the self-update, the approval on widened
permissions, and every step later issues add (the verification, the reporter's wiring, the spike
jobs). Every stack a later issue adds to the stage brings its own deployment actions, and every file
or image asset brings an asset-publishing action; listing those one by one would make every
construct issue of the backlog change this protected file. They are held by rules instead: every
asset-publishing action is a CodeBuild action of the pipeline's asset stage, and every stack's
deployment action is a CloudFormation action of a stage that deploys the application, after the
approval on widened permissions; and, for every action, the rules on triggers, webhooks and the
permissions boundary. An issue that only adds a stack or an asset to the stage therefore leaves the
list as it is; an issue that adds or changes a step, a job, a rule or a role of the pipeline names
`src/rhi/pipeline/stack.py`, and names `tests/policy/test_pipeline.py` when it changes what that
test holds.

**The invariant comes before the pipeline**, as the stateful-resource invariant comes before the
tables. The app has no pipeline yet, so the list is empty and the test passes on the app as it
stands; its checks are proven on pipelines built in the test with CDK Pipelines. Issue E1-09 then
adds the pipeline and fills the list with what it builds.

This issue creates `tests/policy/test_pipeline.py` under `tests/policy/`, a protected path (ADR
0001, decision 10); the builder may write it because this issue names it.

## Deliverables

- [ ] `tests/policy/test_pipeline.py` holds the reviewed, ordered list of the pipeline's stages and of its actions other than asset-publishing and stack-deployment actions, empty while the app has no pipeline, and fails when the synthesized pipeline of the policy tests' templates differs from that list.
- [ ] `tests/policy/test_pipeline.py` also fails when an asset-publishing action is outside the asset stage or is not a CodeBuild action, when a stack's deployment action is not a CloudFormation action that comes after the approval on widened permissions, when the pipeline has a trigger other than a push to `main`, when a CodeBuild project has a webhook, and when a role lacks the boundary of the bounded bootstrap, by its fixed name.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_pipeline.py` passes on the app as it stands.
- [ ] `tests/policy/test_pipeline.py::test_detects_an_unlisted_action` fails a pipeline, built in the test, that has one action more than its reviewed list, neither an asset-publishing nor a stack-deployment action.
- [ ] `tests/policy/test_pipeline.py::test_a_new_stack_or_asset_needs_no_list_change` shows the test's checks accepting a pipeline, built in the test, whose stage holds one more stack with one file asset while the reviewed list is unchanged, and refusing the same pipeline when that stack's role lacks the boundary.
- [ ] `tests/policy/test_pipeline.py::test_each_rule_refuses_its_breach` fails pipelines, built in the test, that have a trigger on a pull request, a CodeBuild project with a webhook, an asset-publishing action outside the asset stage, and a stack's deployment action before the approval on widened permissions, one each.

## References

- `docs/design/system-design.md`, sections 8.2, 8.3, 8.4
- `docs/adr/0001-software-factory.md`, decision 10
- `docs/design/evidence.md`, E20
