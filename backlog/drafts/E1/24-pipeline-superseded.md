+++
id = "E1-24"
title = "The pipeline lets a newer execution supersede one that waits between stages"
labels = ["size:M", "kind:infra"]
epic = 3
+++

## Context

`src/rhi/pipeline/stack.py` builds the pipeline in queued mode (evidence E20): one execution per
merge commit, each run through every stage in turn. On 2026-11-26 the operator stopped 12
executions by hand, in three batches, each time so that a newer execution queued behind them, one
that carried a change to the configuration in SSM, could run. 11 merge commits then kept a pending
`deploy/Rhi` status for hours: a stopped execution never reaches the stage Rhi, and `Stopped` is a
verdict, so no later execution covers its commit. The owner chose superseded mode on 2026-11-27.

**What CodePipeline does in superseded mode** (its user guide, "How pipeline executions work"). A
stage holds one execution at a time. An execution that has finished a stage waits in front of the
next one while that stage is locked, and a newer execution that reaches the same point overtakes
it: the waiting one ends with the status `Superseded` and goes no further. An execution is never
superseded inside a stage: it finishes the stage it is in. A failed execution can also be
superseded by a newer one that enters its stage, and then cannot be retried.

**With the deployment hold** (issue #670). The hold is an action of the stage Rhi, before its
deployment actions. An execution that has reached it is inside the stage: it waits there until the
session ends, then deploys its own commit. Newer merges gather in front of Rhi, and each supersedes
the one that waited before it. When the session ends, the execution in the hold deploys, then the
newest one, and the commits in between get no execution of their own. In queued mode each of them
deployed in turn, and each ran the Assets stage first.

**The reporter needs no change.** It gives a commit whose execution ended `Cancelled` or
`Superseded` (`SUPERSEDED_STATUSES` in `services/reporter/rhi_reporter/superseded.py`) the verdict
of the earliest later execution that did not end so (`covers`): on `main`, which takes changes in
order, that execution deploys a commit that holds the superseded one. The pipeline rule already
forwards every execution state change to it. The engine's discharge accepts a success on a later
head of `main` that holds the merge commit, and `loop.toml` lists no deployment context, so the
engine waits for none. `tests/unit/reporter/test_superseded.py` simulates only queued mode today,
where an execution ends `Superseded` when it updates the pipeline; the new simulation shows the
same verdicts when a newer merge overtakes a waiting execution.

**What is lost.** A failed deployment names a batch of commits, its own and those it covers, not
one. An execution the owner starts with **Release change** after an edit of SSM can be overtaken by
a merge; the newer execution synthesizes from SSM again, so it deploys the edit too. The runbooks
that tell the owner to follow that execution by its ID must say so.

**The execution that deploys this change.** Its self-update changes the pipeline's definition, and
CodePipeline then cancels the active execution, once its running actions end, and every pending one,
as on any update of the pipeline (E20). The pipeline does not restart on an update, so the next
merge's execution is the first one in superseded mode, and it covers the cancelled commits.

**Protected paths.** `src/rhi/pipeline/` is protected (ADR 0001, decision 10); the builder may
change `src/rhi/pipeline/stack.py` because this issue names it. No invariant of `tests/policy/`
reads the execution mode.

Suggested branch: `feat/pipeline-superseded`

## Deliverables

- [ ] `src/rhi/pipeline/stack.py` builds the pipeline with the execution mode SUPERSEDED, and its docstring and its comment on the mode say why.
- [ ] `tests/synth/test_pipeline.py` asserts that the synthesized pipeline is of type V2 and in the execution mode SUPERSEDED.
- [ ] `tests/unit/reporter/test_superseded.py` also simulates a pipeline in superseded mode, in which a push while an execution waits ends the waiting execution Superseded, and checks every sequence of at most 6 pushes and runs in that mode as it checks them in queued mode.
- [ ] The docstrings of `services/reporter/rhi_reporter/superseded.py` and `services/reporter/rhi_reporter/handler.py` say that an execution also ends Superseded when a newer one overtakes it between stages, not only when the pipeline updates itself.
- [ ] `docs/design/system-design.md` (section 8.2, and the superseded bullet of section 8.4), `docs/design/evidence.md` (entry E20) and `docs/adr/0001-software-factory.md` (decision 5) describe superseded execution, and none of them says that the pipeline runs queued or gives each commit its own execution.
- [ ] `docs/runbooks/bad-deployment.md`, `docs/runbooks/sessions.md` and `docs/runbooks/park-and-unpark.md` say that a newer merge can supersede an execution waiting between stages, one started with Release change included, and that the newer execution then deploys its commit and the configuration SSM holds.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pipeline.py tests/unit/reporter/test_superseded.py tests/unit/reporter/test_handler.py` passes.
- [ ] `tests/unit/reporter/test_superseded.py` shows that in superseded mode the commit of an execution overtaken while it waited ends, under deploy/Rhi and verify/Rhi, with the states of the execution that overtook it.
- [ ] `bash -c "! git grep -n -i -e 'ExecutionMode.QUEUED' -e 'in queued execution' -e 'V2, queued execution' -e 'runs its executions queued' -e 'queued mode gives each commit' -- src docs"` exits 0.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] After the merge, an execution overtaken while it waited between stages ends Superseded in the pipeline's execution history, and its merge commit's deploy/Rhi status ends with the state of the execution that overtook it, with no execution stopped by hand (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2 and 8.4
- `docs/design/evidence.md`, entry E20
- `docs/adr/0001-software-factory.md`, decisions 5 and 10
- `services/reporter/rhi_reporter/superseded.py`, `covers` and `SUPERSEDED_STATUSES`
- `src/rhi/pipeline/hold.py`, the hold of issue #670
- https://docs.aws.amazon.com/codepipeline/latest/userguide/concepts-how-it-works.html
- https://docs.aws.amazon.com/codepipeline/latest/userguide/execution-modes.html
