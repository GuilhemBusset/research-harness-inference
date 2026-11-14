+++
id = "E3-04"
title = "Run vLLM with the settings the engine contract requires, carried in the pool's release"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-04", "E2-32", "E2-42", "E3-03", "E3-07"]
+++

## Context

Engine settings that are part of the contract for vLLM (section 4.1): usage telemetry off, Hugging
Face access off, no request-supplied chat templates, no development or profiling routes, media
fetching restricted to inline data (evidence E23). The design asks for a test that asserts them.

**Where the command is built, and how it reaches the engine.** A pool's release carries its engine
flags, a list of arguments (section 4.3; issue E2-33): `src/rhi/pools/release.py` writes them into
the release parameter when the stack is deployed (issue E2-42), and the engine's container receives
them as its arguments (issue E3-38). So the arguments are built when the app synthesizes, by a module
of the CDK app, `src/rhi/pools/vllm_command.py`, which reads the pool through
`services/common/rhi_common/pool_config.py` as `src/rhi/config.py` already does. Both folders are
package roots of issue E1-02, so no tool's configuration changes. The settings vLLM reads from its
environment are the same for every vllm pool and live in the engine image, with the entry point that
gives vLLM its bearer secret (issue E3-03); the region and the GPUs come from the GPU drop-in (issue
E3-38). Neither the arguments nor the release ever hold the secret.

**Where the image is built from.** `src/rhi/pools/release.py` builds a pool's engine image from the
build context of a Python service that issue E1-16's packaging module gives (issue E2-42), which fits
the mock's image only. The vLLM image holds no package of this project; its build context is its own
folder, `services/engine/vllm/` (issue E3-03). So a vllm pool's release builds its image from that
folder, for the architecture issue E2-42's rule gives its instance types.

**The contract, setting by setting.** The names are vLLM's at version 0.33.0. The builder reads each
in vLLM's source at the tag v0.33.0 (`vllm/envs.py` and the server's argument parser) and the module
cites, for each argument, where the tag defines it; a name the tag spells differently is replaced by
the one with the same effect, and the issue's pull request says so.

| Contract setting | vLLM 0.33 | Where | In every vllm pool |
| --- | --- | --- | --- |
| Usage telemetry off | VLLM_NO_USAGE_STATS=1, DO_NOT_TRACK=1 | image (issue E3-03) | present |
| Hugging Face access off | HF_HUB_OFFLINE=1 | image (issue E3-03) | present |
| No development routes | VLLM_SERVER_DEV_MODE | image (issue E3-03) | absent |
| No profiling routes | VLLM_TORCH_PROFILER_DIR | image (issue E3-03) | absent |
| No profiling routes | `--profiler-config` | arguments | absent |
| No request-supplied chat template | `--trust-request-chat-template` | arguments | absent |
| Media only from inline data | `--allowed-media-domains` naming the one domain `media.invalid` | arguments | present |
| No media from local files | `--allowed-local-media-path` | arguments | absent |
| No request content in the log | `--enable-log-requests`, `--enable-log-outputs` | arguments | absent |
| No model code from the repository | `--trust-remote-code` | arguments | absent, unless the pool records its exception |
| No secret on the command line | `--api-key` | arguments | absent |

vLLM has no option that turns fetching a media URL off. An allow-list that names one domain under
`.invalid`, which RFC 6761 reserves and which never resolves, refuses every URL, while a data URL
names no host and is read as before. That is this issue's reading of "restricted to inline data".

Prompts and responses are not stored (section 6.6). The engine's own log goes to the pool's log
group with every other unit's (issue E2-58), so request logging stays off.

**Weights and model code.** Weights load straight from S3 into GPU memory with the Run:ai streamer
(section 4.4): the model's location is the pool's prefix in the model bucket at its revision,
`s3://<model bucket>/<pool>/<revision>/`, the layout the copy job writes (issue E3-09), with the
bucket's name from the function of `src/rhi/pools/model_store.py` (issue E3-07), left to deployment.
The fallback loader, a download to the EBS volume first, is issue E3-44. Model code from a
repository (`trust-remote-code`) is off by default; a pool that needs it is a reviewed exception
recorded with that pool, and the coursework model does not (section 4.4; evidence E28).

**Configuration.** This issue adds two optional pool fields: trust_remote_code, false by default, and
trust_remote_code_reason, which a pool that sets the first must give. Their values reach the engine
in the release's flags. The example configuration holds no vllm pool until issue E3-10; it documents
the fields in a comment. The serving settings of a pool's release (context, cache, step budget,
parsers, served name, metric buckets) are issue E3-22, on the same module; an engine spread over
several GPUs is phase 4's.

## Deliverables

- [ ] `src/rhi/pools/vllm_command.py` builds, from a vllm pool's configuration, vLLM's arguments: the model at the pool's prefix of the model bucket at its revision, loaded by the Run:ai streamer, and port 8000.
- [ ] Every vllm pool's arguments hold each setting the Context's table marks present among the arguments and none it marks absent there, and the module cites where vLLM 0.33.0 defines each.
- [ ] It passes the option that trusts a model repository's code only for a pool whose trust_remote_code is true, which `services/common/rhi_common/pool_config.py` accepts only with a trust_remote_code_reason; both fields are optional, false and empty by default.
- [ ] `config.example.toml` documents the two fields and their defaults in a comment.
- [ ] `src/rhi/pools/release.py` writes, as a vllm pool's engine flags in its release, the arguments this module builds, builds a vllm pool's engine image from the vLLM image's folder of issue E3-03 as its whole build context, and leaves the mock pool's release as it was.
- [ ] Every cdk-nag finding `src/rhi/pools/release.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_release.toml`.
- [ ] `tests/unit/pools/test_vllm_command.py` covers the arguments, `tests/unit/common/test_pool_config.py` the two fields, and `tests/synth/test_pool_release.py` the flags in the release.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/pools/test_vllm_command.py` passes: for a vllm pool built in the test, each argument the table marks present is there, each it marks absent is not, the model is read from the pool's prefix at its revision, no API key option is there, and the trust option is absent unless the pool records its exception.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a pool without the two fields reads false and no reason, and a pool that sets trust_remote_code without a reason is refused, naming the pool and the field.
- [ ] `tests/synth/test_pool_release.py::test_a_vllm_pool_carries_its_engine_flags` finds, in the release of a vllm pool of a configuration built in the test, the arguments the module builds for it and an engine image whose asset is built from the folder `services/engine/vllm/`, and the mock pool's release unchanged.
- [ ] On a GPU instance of the coursework pool, vLLM started with these arguments passes the contract suite of `tests/contract/` (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 4.4, 6.6
- `docs/design/evidence.md`, E11, E23, E28
