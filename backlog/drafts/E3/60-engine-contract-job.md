+++
id = "E3-60"
title = "Spike job engine_contract: in a booked session, send rhi-engine-contract to the pool's instances and record each contract test's outcome"
labels = ["size:M", "kind:test"]
epic = 5
blocked_by = ["E3-59"]
+++

## Context

Issue E3-59 puts the engine contract suite on every instance and gives the pools stack one Systems
Manager document, `rhi-engine-contract`, with no parameter, that runs it there against the
instance's own engine and prints only the lines `rhi-contract <test> <outcome>` and
`rhi-contract exit <status>`. Only the account's administrator may send it. This issue makes the
check a job the owner starts, as every spike that needs AWS is (section 12), so that issue #149's
last item can be shown by a build of a job on `main`, with no person on an instance.

**The new right, and why it is the owner's.** A spike job reaches a pool through the gateway, as a
canary student, and its role holds no action of Auto Scaling, EC2 or IAM (`rhi.pipeline.spikes`;
`tests/policy/test_pipeline.py`, `SPIKES_MAY`). This job needs one thing more: to send that one
document to a pool's instances, and to read what each invocation printed. The document takes no
parameter and runs only the release's own script, whose digest the boot program checked, so the
right runs the contract check on an instance and nothing else; but it is a right to run a command
on GPU instances that hold their pool's secret, which no role of the pipeline has had. It is
granted to the shared role of the spike jobs, because `SpikeJobs` gives every job one role
(`rhi.pipeline.spikes`, "One role, one log group and one key"): every spike job may then send that
document. Decided by the orchestrator on 2026-11-27: the shared role of the spike jobs sends the
document, since the document takes no parameter and runs only the release's own contract check,
which a role of the job's own would not make narrower in effect; the owner reverses it by editing
this issue.

**What the role gains**, and nothing else: `ssm:SendCommand` on the document
`rhi-engine-contract` of the stage's account and region, and on the EC2 instances whose tag
`rhi-pool` names a pool of the stage's configuration, under the condition
`ssm:resourceTag/rhi-pool` (every instance of a pool carries the tag, from its launch template's
tag specifications, `rhi.pools.group`); and `ssm:ListCommandInvocations`, which AWS grants only on
every resource, and which gives each invocation's status and what its step printed. The job sends
by the tag, so it names no instance and needs no read of EC2 or Auto Scaling.

**The session.** The job books nothing: as spikes S3 and S5 do, it runs in a session the owner
booked, which it checks before it sends anything (`common.require_session`): active, with the
instances in service it is given, lasting the run's minutes. A rehearsal on the mock pool runs in
a session of the mock pool booked the same way, with the admin command.

**What it records**, one JSON object a line on standard output, as spike S3 writes its lines, each
with its UTC time and kind: `job` (the pool and its engine); for each instance the command reached, in order of appearance and
never by its ID, `instance_check` (the invocation's status, one of Run Command's statuses the job
lists or `other`; each of the four tests and its outcome, read from the `rhi-contract` lines of the
output, any other line dropped; the exit status the output gives); and `job_failed` (an error's
type, never its text, and the session's reason when it refused the run). The job exits 0 only when
at least the instances it was given answered and every one reported each of the four tests
`passed`.

Suggested branch: `feat/engine-contract-job`

## Deliverables

- [ ] `spikes/engine_contract.py` reads `RHI_SPIKE_POOL` (`coursework` unless given), `RHI_SPIKE_INSTANCES`, `RHI_SPIKE_RUN_MINUTES` and `RHI_SPIKE_POOL_STATE_TABLE`, checks the pool's session with `common.require_session` before it sends anything, sends the document `rhi-engine-contract` to the instances whose tag `rhi-pool` is the pool's name, and waits for every invocation to end within the run's minutes.
- [ ] `spikes/engine_contract.py` writes the lines `job`, `instance_check` and `job_failed` the Context describes, names each instance by its order of appearance and never by its ID, keeps from an invocation's output only the lines `rhi-contract <test> <outcome>` and `rhi-contract exit <status>` whose test and outcome are of the vocabulary of issue E3-59, and exits 0 only on the condition the Context gives.
- [ ] `src/rhi/pipeline/spikes.py` lists the job `SpikeJob("engine_contract", "engine_contract.py")` in `JOBS`, and gives the shared role the two statements of the Context and no other, its docstring's list of what the role may do saying so.
- [ ] `tests/policy/test_pipeline.py` lets a spike job's role send `ssm:SendCommand` only on the document `rhi-engine-contract` and on instances under the condition on the tag `rhi-pool`, and `ssm:ListCommandInvocations` only, beside what `SPIKES_MAY` already lists, and still refuses every other action, those of EC2, Auto Scaling and IAM among them.
- [ ] `tests/policy/acknowledged/pipeline_spikes.toml` acknowledges, each with its reason, the findings the two statements bring: the instances named by pattern under the tag's condition, and `ssm:ListCommandInvocations` on every resource, with the inventory in its comments updated.
- [ ] `tests/unit/spikes/test_engine_contract.py` drives the job with a stand-in for Systems Manager and the pool-state records, and asserts: a session that refuses the run gives one line `job_failed` with its reason and no command sent; the lines and the exit status for instances that all passed, for one that failed a test, and for one whose invocation timed out; an output line outside the vocabulary written nowhere; and no instance ID in any line.
- [ ] `tests/synth/test_spike_jobs.py` asserts the project `rhi-spike-engine_contract` and the role's two statements on the synthesized pipeline stack.
- [ ] `docs/design/system-design.md`, in section 4.1, names the spike job `engine_contract` as what sends the document `rhi-engine-contract` in a session the owner booked.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_engine_contract.py tests/synth/test_spike_jobs.py tests/policy` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A build of `rhi-spike-engine_contract` on `main` with `RHI_SPIKE_POOL=mock`, in a session of the mock pool, succeeds and writes one `instance_check` line per instance in service, each with the four tests `passed` (needs aws:deployed-environment).
- [ ] A build of `rhi-spike-engine_contract` on `main`, in a session of the coursework pool, succeeds and writes one `instance_check` line per instance in service, each with the four tests `passed`: issue #149's last item (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 8.4 and 12
- `src/rhi/pipeline/spikes.py`
- `tests/policy/test_pipeline.py`, `SPIKES_MAY`
- `tests/policy/acknowledged/pipeline_spikes.toml`
- `spikes/common.py`, `require_session`
- `spikes/s3_harness_task.py`, its lines and `run_job`
- `src/rhi/pools/group.py`, the launch template's tags
