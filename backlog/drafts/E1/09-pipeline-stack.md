+++
id = "E1-09"
title = "The pipeline: CodePipeline V2 from main, queued, deploying the stage"
labels = ["size:L", "kind:infra"]
epic = "E1"
blocked_by = ["E1-05", "E1-07", "E1-08", "E1-19"]
+++

## Context

The system is deployed only by CodePipeline (requirement R6). Section 8.2 fixes the shape: CDK
Pipelines on CodePipeline V2 in queued execution, so that each commit has its own execution; the
source is this repository through a CodeConnections connection the stack creates, which starts
pending until the owner authorizes it; only a push to `main` starts an execution. CodeBuild is never
triggered by a pull request: on a public repository that would run a stranger's code with a role in
the owner's account (section 8.3). The approval CDK Pipelines offers on widened permissions is kept
as a tripwire for honest mistakes, knowing it does not cover the pipeline's own stack, which updates
earlier in the same run (section 8.4, control 6).

The synth step's content is issue E1-10: reading the real configuration from SSM, the leak check,
synthesis and the tests. Until then this issue's synth step names no configuration source, so that
an execution started before E1-10 fails closed in the loader instead of deploying the example
configuration. The owner's first deployment of the pipeline is therefore an obligation of E1-10,
not of this issue.

**The invariant is already there.** Issue E1-19 wrote `tests/policy/test_pipeline.py`: a reviewed,
ordered list of the pipeline's stages and of its actions other than asset-publishing and
stack-deployment actions, empty until now, and rules for those two kinds of action, for triggers,
webhooks and the permissions boundary. This issue fills the list with the stages and actions it
builds, and the rules must pass on the pipeline as it builds it.

The pipeline stack brings its own cdk-nag findings and IAM statements that CDK Pipelines writes on
every resource. Each is fixed, or acknowledged with its reason in the per-module file of issues
E1-05 and E1-07: for `src/rhi/pipeline/stack.py`, that is
`tests/policy/acknowledged/pipeline_stack.toml`. The resources CDK Pipelines creates are children of
the pipeline construct this module defines, so they belong to that file. That file is most of this
issue's work, which is why it stays large after the invariant moved to its own issue.

This issue creates `src/rhi/pipeline/`, which ADR 0001's first amendment makes a protected path, and
`tests/policy/acknowledged/pipeline_stack.toml`, and changes `tests/policy/test_pipeline.py`, both
under `tests/policy/`, also protected (decision 10). The builder may write them because this issue
names them.

## Deliverables

- [ ] `src/rhi/pipeline/stack.py` defines a CDK Pipelines pipeline of type V2 in queued execution mode, with a source, a synth step that names no configuration source, the self-update, and the stage of the empty app, deployed behind the approval CDK Pipelines asks for when a change widens permissions.
- [ ] The source is a CodeConnections connection created by the stack; the repository's owner and name come from configuration; a push to `main` is the only trigger, and no CodeBuild project has a webhook.
- [ ] Every role the pipeline and the stage create carries the permissions boundary of the bounded bootstrap, by its fixed name.
- [ ] `app.py` instantiates the pipeline stack, the stage is deployed only through it, and `tests/synth/test_pipeline.py` checks the pipeline's template.
- [ ] `tests/policy/test_pipeline.py` lists, in its reviewed list, the stages and the actions this pipeline has other than asset-publishing and stack-deployment actions, in order, and its rules are unchanged.
- [ ] Every cdk-nag finding `src/rhi/pipeline/stack.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pipeline_stack.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline.py` passes: the pipeline is of type V2, in queued mode, with one source on `main`, and the stage's deployment is preceded by the approval on widened permissions.
- [ ] `uv run --locked pytest tests/policy/` passes with the pipeline stack among the templates the policy tests read, `tests/policy/test_pipeline.py` included.
- [ ] `scripts/check.sh synth` exits 0 with the pipeline stack in the app.

## References

- `docs/design/system-design.md`, sections 8.2, 8.3, 8.4
- `docs/adr/0001-software-factory.md`, decision 10 and amendment 1
- `docs/design/evidence.md`, E20
- `tests/policy/acknowledged/README.md`, delivered by the invariants issue
