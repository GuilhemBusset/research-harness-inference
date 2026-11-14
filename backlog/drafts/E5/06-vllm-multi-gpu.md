+++
id = "E5-06"
title = "vLLM: serve one model across every GPU of a multi-GPU instance"
labels = ["size:M", "kind:app"]
epic = "E5"
blocked_by = ["E3-22", "E3-38"]
+++

## Context

The upgrade model is 186 GB at 8 bits, more than one 96 GB GPU holds. On `g7e.24xlarge` it runs
across the instance's four GPUs (section 4.2), as vLLM's recipe for it was run on four of this GPU
(evidence E28). Issue E3-04 builds vLLM's command for a model on one GPU, in
`src/rhi/pools/vllm_command.py`, and `src/rhi/pools/release.py` carries it into the pool's release.
This issue lets a pool's configuration say how many GPUs its engine spans, and starts the engine
that way.

**The field.** gpus, the number of GPUs a vllm pool's engine spans, is optional with the default 1,
so every pool deployed before this issue runs as it did and the deployed configuration needs no new
value. It reaches the engine in the release's engine flags. The release also carries the number
itself, as the settings key gpus of issue E2-33's release, which the lifecycle agent reads on the
instance to decide whether to extend the launch hook (issue E5-02). It writes that key only for a
pool that spans more than one GPU, so the releases of the pools already deployed do not change, and
the agent learns to read the key (issue E5-02) before the upgrade pool, the first such pool, enters
the configuration (issue E5-03). The example configuration documents the field in a comment until the upgrade pool
gives it (issue E5-03).

**Shared memory.** vLLM's workers spread over several GPUs share data through shared memory, which a
container does not get by default; vLLM's container guide gives two ways, the host's IPC namespace
(the option --ipc=host) or a larger private shared memory (the option --shm-size). This issue takes
the second, 16 GiB: the engine's container then keeps its own IPC namespace and cannot read the
shared memory of the instance's other processes (the lifecycle agent, the collector, the DCGM
exporter), while vLLM's workers get what they need. The design gives no size; 16 GiB is small beside
the instance's memory, and spike S6 shows whether it is enough (issue E5-05). The drop-in is the one
every GPU instance installs (issue E3-38), so a one-GPU engine gets the same shared memory and does
not use it.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives a vllm pool the optional field gpus, 1 by default, and refuses a value below 1, naming the pool and the field.
- [ ] `config.example.toml` documents the field and its default in a comment.
- [ ] `src/rhi/pools/vllm_command.py` spreads the model over that many GPUs, in the layout of vLLM's recipe for Qwen3.8-Flash-Next on four RTX PRO 6000 GPUs, and adds nothing for a pool on one GPU.
- [ ] `src/rhi/pools/release.py` writes the pool's number of GPUs as the settings key gpus of the release of a pool that spans more than one GPU, and writes no such key for any other pool.
- [ ] Every cdk-nag finding `src/rhi/pools/release.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_release.toml`.
- [ ] `services/engine/units/gpu/rhi-engine.service.d/gpu.conf` gives the engine's container, beside what issue E3-38 gives it, a private shared memory of 16 GiB and not the host's IPC namespace.
- [ ] `tests/unit/pools/test_vllm_gpus.py` covers the command and the drop-in for one GPU and for four, and `tests/synth/test_pool_release.py` the number in the release.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/pools/test_vllm_gpus.py` passes: a pool spanning 4 GPUs gets a command that spreads the model over 4 and still carries every contract setting of issue E3-04, a pool on one GPU gets the same command as before this issue, and the drop-in gives the container 16 GiB of shared memory and not the host's IPC namespace.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/pools/test_vllm_command.py` passes, and the first refuses a pool whose engine spans 0 GPUs.
- [ ] `tests/synth/test_pool_release.py::test_the_release_carries_the_gpus` finds the key gpus at 4 in the release of a pool of a configuration built in the test that spans 4 GPUs, and no such key in the release of a pool that leaves the field out.

## References

- `docs/design/system-design.md`, sections 4.1, 4.2
- `docs/design/evidence.md`, E28
- vLLM's recipes: https://github.com/vllm-project/recipes
- vLLM's container guide: https://docs.vllm.ai/en/stable/deployment/docker.html
