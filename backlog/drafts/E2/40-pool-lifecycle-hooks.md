+++
id = "E2-40"
title = "A pool's lifecycle hooks, with explicit timeouts"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-41", "E2-42"]
+++

## Context

The lifecycle transitions are specified, not left to defaults (section 4.3): without explicit
timeouts a hook waits an hour, and a dead agent bills an hour of GPU per attempt. Entry to the warm
pool has 15 minutes on the coursework pool. Promotion to service has twice the measured start.
Leaving service has `drain_seconds` plus a margin. One launch hook serves the first two transitions,
so it carries the longer timeout and the agent keeps each transition's own deadline, abandoning the
instance when it passes (issue E2-34).

Until spike S1 has measured a start, the launch hook's timeout is 15 minutes for the coursework pool,
and the margin added to the drain for the termination hook is 60 seconds (section 17). The design gives
no figure for the mock pool, which uses the same 15 minutes. The measured start is the
pool's optional setting of issue E2-04. The multi-GPU pools extend their hook by heartbeats (section
4.3), in phase 4.

The launch hook's default result abandons the instance, so the hooks come last among a pool's
pieces: once they exist, an instance enters service only when its agent completes the hook, which
needs the role (issue E2-41), the secret (issue E2-43) and the release (issue E2-42). The hooks are
named `rhi-launch` and `rhi-termination`, the names the release parameter already gives the agent
(issue E2-42).

## Deliverables

- [ ] `src/rhi/pools/hooks.py` adds to each pool's group a launch hook named rhi-launch and a termination hook named rhi-termination, and `src/rhi/pools/stack.py` adds them to the pools stack.
- [ ] The launch hook's timeout is 15 minutes while configuration holds no measured start for the pool, and afterwards the longer of 15 minutes and twice that measured start; its default result abandons the instance.
- [ ] The termination hook's timeout is the pool's drain_seconds plus 60 seconds, and its default result lets the instance go.
- [ ] Every cdk-nag finding `src/rhi/pools/hooks.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_hooks.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/synth/test_pool_hooks.py` checks both hooks of the mock pool.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_hooks.py` passes: the mock pool's hooks are named rhi-launch and rhi-termination, its launch hook has a timeout of 900 seconds with no measured start and of 1200 seconds with a measured start of 600, and its termination hook one of 360 seconds.
- [ ] In the account, with the mock pool's desired capacity set to 1, an instance comes from the warm pool, its agent completes the launch hook and its target turns healthy, which shows the machine image carries what the boot program needs (needs aws:deployed-environment).
- [ ] In the account, after a release changes, every instance in service and in the warm pool reports the new release (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 17
- `docs/design/evidence.md`, E10
