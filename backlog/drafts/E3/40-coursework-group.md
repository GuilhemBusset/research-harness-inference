+++
id = "E3-40"
title = "The coursework pool's group: a 150 GB volume, two stopped warm instances, and its synthesis"
labels = ["size:S", "kind:infra"]
epic = "E3"
blocked_by = ["E2-72", "E2-90", "E3-06", "E3-10"]
+++

## Context

The example configuration's coursework pool (issue E3-10) synthesizes into a group like every pool
(issue E2-71). What it needs beyond the mock pool is disk: the engine image of vLLM, Docker's data
and the engine's compile cache live on the EBS volume (section 4.3), and a stopped warm instance
costs only its disk, about $12 a month for 150 GB (section 5.2). So a GPU pool's instances get a
volume of 150 GB; the mock pool's keep the size issue E2-71 gives them.

This issue checks the coursework pool's synthesized group, and carries the owner's two steps that
put the pool in the account, and the first observation of it, which also needs a GPU quota (issue
E1-13) and the gateway reachable through the public load balancer (issue E2-90). The steps wait for
this issue, not for issue E3-10, because the pool can serve only once both its volume (this issue)
and the vLLM row its lifecycle agent counts requests by (issue E3-06) are merged: a pool in SSM
before then fails its launches until the scaler suspends it, and a later volume change starts no
instance refresh. The launch template reads the image from its parameter when the stack is deployed
(issue E3-01), so the owner writes the parameter first; the promotion job of issue E3-02 cannot give
that first value, as it starts its canary from the pool's own launch template.

## Deliverables

- [ ] `src/rhi/pools/group.py` gives the instances of a GPU pool, a pool whose engine is not mock, an EBS volume of 150 GB, and leaves the mock pool's volume as it was.
- [ ] Every cdk-nag finding `src/rhi/pools/group.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_group.toml`.
- [ ] `tests/synth/test_coursework_pool.py` checks the coursework pool's group, warm pool, instance type and volume on the app with the example configuration.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_coursework_pool.py tests/policy/test_fleet_cap.py` passes: the coursework group has a maximum of 2, the instance type g7e.2xlarge alone and a 150 GB volume, its warm pool prepares 2 stopped instances, and the mock pool's volume is unchanged.
- [ ] `scripts/check.sh synth` exits 0.
- [ ] Before adding the coursework pool to the configuration in SSM, the owner writes the parameter that `src/rhi/pools/image.py` names for the coursework pool, with the ID of AWS's current base GPU image with the open-source NVIDIA driver on Ubuntu 22.04 in the account's region (needs aws:account).
- [ ] After writing the image parameter, the owner adds the coursework pool of `config.example.toml`, with its manifest's revision, to the configuration in SSM and sets its key default_pool to coursework (needs aws:account).
- [ ] In the account, a student's request wakes the coursework pool from its warm pool, is held, and is answered by the model in each wire format (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.2, 4.3, 5.2
- `docs/design/evidence.md`, E10
