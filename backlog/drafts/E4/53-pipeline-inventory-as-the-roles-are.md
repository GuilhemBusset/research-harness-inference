+++
id = "E4-53"
title = "Pipeline acknowledgements: the verification and hold roles as they are"
labels = ["size:S", "kind:test"]
epic = 6
blocked_by = [691]
+++

## Context

`tests/policy/acknowledged/pipeline_stack.toml` is the acknowledgement file of the pipeline stack
(system design, section 8.4). Its `[wildcard_actions]` table is what `tests/policy/test_iam_wildcards.py`
admits on every resource for any role that module creates, and its comments are the inventory of
the roles CDK Pipelines creates for the steps of `src/rhi/pipeline/verify.py`,
`src/rhi/pipeline/weights.py` and `src/rhi/pipeline/hold.py`. Since the pools stopped refreshing
their instances, the file no longer says what those roles hold:

- The wildcard table still admits autoscaling:DescribeAutoScalingGroups and
  autoscaling:DescribeInstanceRefreshes, with the reason that the verification's decision finds
  each GPU pool's group and waits for its refresh. No role of the pipeline stack holds either
  action now, and `tests/policy/test_pipeline.py` refuses both on the verification role only: on
  any other role of the module the two entries would admit them with no reason that holds. An
  acknowledgement is accepted only with its reason, and this one has none left.
- The inventory of the verification role still lists those two actions, gives
  codepipeline:ListPipelineExecutions a refresh as its reason, and omits what the booking added:
  cloudformation:DescribeStackResource on the pools stack, and dynamodb:GetItem and
  dynamodb:PutItem on the items of the pool-state table whose partition key is the name of a pool
  the step books (the mock pool's and the coursework pool's). The decision now reads when the
  execution started so as to compare the release parameters' versions with it
  (`scripts/pipeline/gpu_verification.py`).
- The inventory of the hold role, and the nag reason for the pool-state table's name pattern, say
  that the hold step reads the session switch. Once issue 691 is merged it reads each pool's session
  and no switch. The verification step reads and writes items of the same table, and finds its
  name in the same pools stack, so the nag reasons for those two name patterns serve both steps.

Only this file changes: no role, no test, nothing deployed. `tests/policy/` is a protected path,
which this issue names.

## Deliverables

- [ ] `tests/policy/acknowledged/pipeline_stack.toml` has no entry for autoscaling:DescribeAutoScalingGroups or autoscaling:DescribeInstanceRefreshes in its wildcard_actions table, and its comments list neither action.
- [ ] The inventory of the verification role in `tests/policy/acknowledged/pipeline_stack.toml` lists each action the role holds, with the reason it holds it: cloudformation:DescribeStackResource on the pools stack, dynamodb:GetItem and dynamodb:PutItem on the pool-state items of the pools the step books, and codepipeline:ListPipelineExecutions for the time the execution started, which the decision compares the release parameters' versions with, among them.
- [ ] The inventory of the hold role in `tests/policy/acknowledged/pipeline_stack.toml` says that the hold step reads the session of each pool, and names no session switch.
- [ ] The nag reasons in `tests/policy/acknowledged/pipeline_stack.toml` for the pools stack's name pattern and for the pool-state table's name pattern each name the hold step and the verification step, and name no session switch.

## Acceptance criteria

- [ ] `uv run --locked python -c 'from pathlib import Path; import tomllib; actions = tomllib.loads(Path("tests/policy/acknowledged/pipeline_stack.toml").read_text())["wildcard_actions"]; assert not {"autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeInstanceRefreshes"}.intersection(actions)'` exits 0.
- [ ] `git grep -n -i -e "autoscaling:" -e "session switch" -- tests/policy/acknowledged/pipeline_stack.toml` prints nothing.
- [ ] `uv run --locked pytest tests/policy/test_acknowledged.py tests/policy/test_iam_wildcards.py tests/policy/test_nag_acknowledgements.py tests/policy/test_pipeline.py tests/synth/test_pipeline_verify.py tests/synth/test_pipeline_hold.py` passes.

## References

- `docs/design/system-design.md`, section 8.4
- `tests/policy/acknowledged/pipeline_stack.toml`
- `tests/policy/acknowledged/README.md`
- `src/rhi/pipeline/verify.py`
- `src/rhi/pipeline/hold.py`
- `scripts/pipeline/gpu_verification.py`
