+++
id = "E5-03"
title = "The upgrade pool: Qwen3.8-Flash-Next on four 96 GB GPUs, for scheduled sessions"
labels = ["size:M", "kind:infra"]
epic = "E5"
blocked_by = ["E2-72", "E3-27", "E3-40", "E4-02", "E5-01", "E5-02", "E5-06", "E5-08"]
+++

## Context

The upgrade pool serves scheduled sessions (section 4.2): Qwen3.8-Flash-Next, 176B parameters with
6B active, in its publisher's 8-bit build of 186 GB, under vLLM on `g7e.24xlarge` (four 96 GB
RTX PRO 6000 GPUs, 96 vCPUs), at $16.57 an hour. Its context is 262,144 tokens. It has the
coursework model's chat template and parsers (`qwen3_coder` for tool calls, `qwen3` for reasoning),
so it gets the coursework model's request profile (sections 4.2, 6.1 and 6.2, step 4). It needs no
model code from its repository (section 4.4), so its entry records no exception.

**What the entry names, and where each value comes from.**

- The model: the repository and revision of the manifest of issue E5-08, which takes the
  repository from vLLM's recipe for the model; nothing here names a repository of its own.
- The served name: the pool's name, upgrade, as for every vllm pool (issue E3-22); it holds no
  slash, which Claude Code requires (evidence E28).
- Beyond the parsers and the context, the design gives no engine setting for this pool, and this
  issue sets none; its engine spans the instance's 4 GPUs (issue E5-06).
- The largest output is not a number the design gives. As for the coursework pool (issue E3-10),
  Claude Code asks for up to 32,000 output tokens on its main turns (evidence E15), and the body
  filter clamps to the pool's largest output, so 32,000 leaves Claude Code's requests as sent.
- `target_concurrency`, which every pool gives (issue E2-04) and which is also the engine's limit on
  requests running at once (issue E3-22): the design gives none for this pool; section 4.7
  estimates what it serves up to 32 streams, so this issue sets 32.
- `wake = "scheduled-only"`: a schedule or an admin command starts it, never a student's request,
  and it keeps no warm instance between sessions (sections 4.2 and 11.2), so `warm_instances` is 0
  and its group has no warm pool (issue E2-72). One instance carries the class (decision 3), and the
  quota the owner requests for it is 96 more G-family vCPUs, one instance (section 8.6): its
  `max_instances` is 1. Its price is above the $5 an hour above which a pool cannot wake on demand
  (sections 11.3 and 17).
- Its GPU-hour ceilings are the owner's (decision 6). Section 11.3's rule is that the daily ceiling
  covers a session's instances for the session's length plus 80 minutes of pre-warm and idle tail;
  the longest session is four hours (assumption A2).

Its pre-warm lead and its measured start come from spike S6 (issue E5-05); until then the defaults
of issues E4-05 and E2-40 apply. Its expected values on the inference dashboard are issue E5-07. Its
group gives its instances the 150 GB volume of every GPU pool (issue E3-40).

**What the owner does, and when.** The example configuration holds the entry for tests and
synthesis. What runs in the account is the owner's configuration in SSM, so merging this issue starts
nothing, and a deployment without the pool in SSM builds no upgrade pool. Before the pool enters the
configuration in SSM, its image parameter must hold a value (issue E3-01) and its weights must be
copyable: the owner writes the image parameter by hand, as for the coursework pool (issue E3-40), and
the copy job copies the weights at the next deployment that has the pool.

## Deliverables

- [ ] `config.example.toml` adds g7e.24xlarge to the allowed instance types and gains a pool named upgrade: engine vllm, the repository and revision of the manifest of issue E5-08, the instance type g7e.24xlarge alone at 16.57 dollars an hour, gpus 4, max_instances 1, target_concurrency 32, warm_instances 0, wake scheduled-only, a context length of 262,144 tokens, a largest output of 32,000 tokens, the tool-call parser qwen3_coder and the reasoning parser qwen3.
- [ ] The upgrade pool's entry gives the request profile and effort mapping of the coursework pool and records no exception that trusts model code.
- [ ] The upgrade pool's daily GPU-hour ceiling covers one instance for 4 hours plus 80 minutes, and its monthly ceiling is at least its daily ceiling.
- [ ] `tests/synth/test_upgrade_pool.py` checks the upgrade pool's group and entry, and `tests/unit/gateway/test_upgrade_profile.py` checks the request profile a request for it gets.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_upgrade_pool.py` passes: the upgrade group has a maximum of 1, no warm pool and the instance type g7e.24xlarge alone, its daily ceiling is at least 5 hours and 20 minutes, and its monthly ceiling is at least its daily one.
- [ ] `tests/synth/test_upgrade_pool.py::test_cannot_wake_on_demand` shows the configuration loader refusing the upgrade entry once its wake is set to on-demand.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/test_config.py tests/policy/test_fleet_cap.py` passes with the pools of `config.example.toml`, and `scripts/check.sh synth` exits 0 with them.
- [ ] `uv run --locked pytest tests/unit/gateway/test_upgrade_profile.py` passes: a Messages request for the upgrade pool with reasoning effort high, a system message in mid-conversation and a document block reaches the engine with effort xhigh, that message as user content in its place and the block replaced by a line of text, exactly as the same request for the coursework pool does.
- [ ] The owner decides whether scheduled sessions run on the upgrade pool or on several coursework instances, after a reading of the upgrade model's licence, decision 3 of the design (needs human:owner-decision).
- [ ] The owner sets the upgrade pool's daily and monthly GPU-hour ceilings, part of decision 6 of the design (needs human:owner-decision).
- [ ] The account's G-family vCPU quota is raised by 96 for the upgrade pool (needs aws:account).
- [ ] Before adding the upgrade pool to the configuration in SSM, the owner writes the parameter that `src/rhi/pools/image.py` names for it with the ID of AWS's current base GPU image with the open-source NVIDIA driver on Ubuntu 22.04, then adds the pool, with its ceilings, to the configuration in SSM (needs aws:account).
- [ ] The weights copy job of issue E3-09 leaves exactly the files of the upgrade model's manifest under the upgrade pool's prefix at its revision (needs aws:deployed-environment).
- [ ] A session window of the schedule starts the upgrade pool and a scheduled session is served by it, which is the exit criterion of phase 4 (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.2, 4.4, 6.1, 6.2, 8.6, 11.3, 14, 15, 17
- `docs/design/evidence.md`, E5, E15, E28
