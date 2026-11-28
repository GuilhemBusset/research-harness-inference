+++
id = "E7-36"
title = "Compare a change of the engine's flags as a declared launch-settings treatment"
labels = ["size:L", "kind:app"]
epic = 744
+++

## Context

The evaluation compares two measured configurations of a pool (`check_comparable` in `tools/evaluate/report.py`). The epic says the same experiment "compares host counts, model builds or vLLM settings at matched loads" (#744). The design lists the step budget among the "declared treatment fields" that may differ, and asks to "test the vLLM step budget at fixed model and hardware" (`docs/design/agent-session-evaluation.md`, "Reproducing and comparing runs").

A live run cannot do that today. `resolve` (`tools/evaluate/live.py`) requests four settings: `host_count`, `instance_type`, `model_revision` and `engine_image_digest`. Two releases that differ only in their engine flags therefore request the same settings, and only their launch fingerprints differ. `check_comparable` refuses such a pair as `undeclared-treatment` with the lever `launch_settings`, and nothing is posted. Declaring the step budget would not be enough on its own. `_served` also holds a launch-settings change to an observed gateway profile, and a live run neither requests nor observes one: `resolve` builds `phases.Telemetry` without `gateway_profile_digest`. The pair would then be refused as `unverified-treatment`. So none of the engine levers S5 measured on 2026-11-27 (cache-16bit, step-budget, draft-head; `docs/measurements/s5.md`) can be compared on the course suite, and neither can any other flags-only candidate, such as a higher target concurrency (`--max-num-seqs`). The request side fails silently: a change of the pool's reasoning-effort mapping or cache-salt scope changes neither the release nor the requested settings, so today it compares as a `repeat`.

The design ties each kind of provenance to its own experiment. For flag experiments it asks for the fingerprint of the arguments launched on each host, matched "to the manifest throughout the cell"; it asks for "gateway release/profile provenance for request-side experiments". So the fix makes three changes. The manifest requests the release's launch fingerprint and the named settings its flags give. The manifest requests the gateway profile from the deployed pool entry, so that a request-side change is declared and, while no gateway observation exists, refused as unverified instead of compared as a repeat. And the engines' fingerprints verify a launch-settings change without an observed gateway profile. The tests in `tools/evaluate/tests/test_report.py` that require the gateway's observation for a launch-settings change change with this rule.

How the flags are read: with `rhi_common.launch_settings.normalize`, the contract the fingerprint is made under. `--max-num-batched-tokens` is `step_budget_tokens` and `--max-model-len` is `context_limit_tokens`. `--tensor-parallel-size` is `tensor_parallelism`. `--kv-cache-dtype` is `kv_dtype`, read as the `EFFECTIVE` table of `tools/evaluate/telemetry.py` reads the engine's own value: `fp8` is 8 bits and `auto` is 16 bits. `--enable-prefix-caching` and `--no-enable-prefix-caching` set `prefix_caching`. An option the flags leave out is not filled with vLLM's default, just as `launch_settings` leaves it out. An option that has no setting or no label in `contracts.Settings` is declared through the fingerprint alone: `--max-num-seqs`, `--quantization`, the draft head's `mtp`, the parsers and the loader. Flags that the contract does not know give the fingerprint `unavailable`, and then none of these settings is requested.

The gateway profile is the part of the deployed pool entry that changes the body the gateway forwards to the engine: `request_profile`, `reasoning_effort` and `cache_salt_scope` (`rhi_common.pool_config`). `admin_pools.deployed`, which `resolve` already calls, reads that entry.

The rule for a contradicted setting follows the telemetry's. `tools/evaluate/telemetry.py` compares an engine's effective setting with the request only where the request gave a value. `_served` also compares it where the request gave none, so a 16-bit candidate whose release leaves `--kv-cache-dtype` out reads as contradicted against an 8-bit reference.

This issue does not observe the gateway profile, because the gateway publishes none. It also leaves the observed `launch_fingerprint` of the manifest as None: the hosts' fingerprints stay in the checkpoints, where `_launched_as_requested` reads them. The new field enters the fixture bundle's manifest, so its derived files must be regenerated, and so must the digest that `tools/evaluate/tests/test_cli_examples.py` reproduces.

Suggested branch: `fix/evaluate-engine-flags-treatment`

## Deliverables

- [ ] `tools/evaluate/contracts.py` gives `Settings` the field `launch_fingerprint`, a launch fingerprint or None, and `LEVERS` in `tools/evaluate/report.py` maps it to the lever `launch_settings`.
- [ ] `tools/evaluate/live.py` makes `resolve` request the release's launch fingerprint as `launch_fingerprint`, and `step_budget_tokens`, `context_limit_tokens`, `tensor_parallelism`, `kv_dtype` and `prefix_caching` from the release's engine flags, each None when the flags leave its option out.
- [ ] `tools/evaluate/live.py` makes `resolve` request as `gateway_profile_digest` a SHA-256 digest of the canonical JSON of the deployed pool entry's `request_profile`, `reasoning_effort` and `cache_salt_scope`.
- [ ] `tools/evaluate/report.py` verifies a changed `launch_settings` lever by every serving host's launch fingerprint without requiring an observed gateway profile, and counts a setting as contradicted only where its run requested a value for it.
- [ ] The bundle under `tools/evaluate/fixtures/report/` holds `launch_fingerprint` in its requested and observed settings, and its derived files are the ones `report` regenerates from it.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_controller.py` shows `resolve` requesting `step_budget_tokens` 4096 and the flags' fingerprint as `launch_fingerprint` for a release whose flags hold `--max-num-batched-tokens 4096`, and `step_budget_tokens` None for a release whose flags leave that option out.
- [ ] `tools/evaluate/tests/test_report.py` shows two runs that differ only in their step budget and launch fingerprint, whose hosts launched their own run's fingerprint, and that request one gateway profile digest and observe none, compared as `flags_only` with `launch_settings` verified and the rows `step_budget_tokens` and `launch_fingerprint` in the diff.
- [ ] `tools/evaluate/tests/test_report.py` shows two runs whose requested settings differ only in `launch_fingerprint`, as a change of `--max-num-seqs` alone gives, compared as `flags_only` with `launch_settings` verified.
- [ ] `tools/evaluate/tests/test_report.py` shows a candidate with its own launch fingerprint, no `kv_dtype` requested and its engines observed at 16 bits, against a reference that requests and observes 8 bits, compared as `flags_only` with `launch_settings` verified.
- [ ] `tools/evaluate/tests/test_report.py` shows two runs whose requested gateway profile digests differ, and that observe no gateway profile, refused as `unverified-treatment` with the lever `gateway_profile`.
- [ ] `tools/evaluate/tests/test_report.py` shows a candidate that changes the step budget and observes another instance type than the reference, with neither run requesting one, refused as `undeclared-treatment` with the lever `hosts`.
- [ ] `uv run --locked pytest tools/evaluate/tests` exits 0.
- [ ] After the pool is redeployed with only its step budget changed, the operator compares a run of it with a reference run of the previous release and observes the comparison comment on the experiment issue labelled `flags_only` with `launch_settings` verified (needs gpu:capacity).

## References

- `docs/design/agent-session-evaluation.md`, "Reproducing and comparing runs"
- `docs/measurements/s5.md`
- `tools/evaluate/live.py`
- `tools/evaluate/report.py`
- `services/common/rhi_common/launch_settings.py`
- `services/common/rhi_common/pool_config.py`
- `src/rhi/pools/vllm_command.py`
