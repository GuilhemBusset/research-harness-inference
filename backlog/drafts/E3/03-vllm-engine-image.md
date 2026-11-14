+++
id = "E3-03"
title = "The vLLM engine image: a thin layer over upstream 0.33 or later, pinned by digest"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E1-02", "E1-16", "E2-70"]
+++

## Context

The engine image is a thin layer over the upstream vLLM image pinned by digest, built by the
pipeline into ECR (section 4.3). The coursework pool needs vLLM 0.33 or later (sections 4.2 and 6.1):
from 0.33 the engine no longer drops the request's `thinking` field, and it defines the metric names
section 7.2 reads (evidence E3, E28). vLLM published 98 security advisories in 2026 (evidence E23),
so the pin is what a patch changes, and a digest is the only pin that cannot move.

**What the layer adds, and why it is in the image.** A pool's release carries the engine's flags as
a list of arguments and nothing else of the engine (issue E2-33), and the engine's unit starts the
container of the release's image with the pool's secret read from Secrets Manager (issue E2-70). So
what vLLM needs in its environment travels in the image:

- the contract settings that vLLM reads from its environment, the same for every vllm pool (section
  4.1; evidence E23): VLLM_NO_USAGE_STATS=1 and DO_NOT_TRACK=1, so that no usage telemetry is sent,
  and HF_HUB_OFFLINE=1, so that vLLM never calls Hugging Face; and none of VLLM_SERVER_DEV_MODE and
  VLLM_TORCH_PROFILER_DIR, which open development and profiling routes. The names are vLLM's at the
  tag v0.33.0 (`vllm/envs.py`);
- one entry point of this project, `services/engine/vllm/entrypoint.sh`, which puts the pool's
  secret, from RHI_POOL_SECRET, the variable in which the engine's unit gives it to the container (issue E2-70), into
  VLLM_API_KEY, the variable vLLM reads its bearer secret from, and then starts vLLM's server with
  the release's arguments. The secret never appears among the arguments.

The arguments themselves are issue E3-04's.

**The build context.** Issue E1-16's packaging module gives a build context to a Python service's
image only: `rhi_common`, the service's package, its Dockerfile and its group's requirements from
`uv.lock`. This image holds no package of this project and installs nothing from the lock, so that
context does not fit it. Its build context is its own folder, `services/engine/vllm/`, which holds
the Dockerfile, the entry point and the version file and nothing else, as the Grafana image's is
its own folder (issue E2-59). A change elsewhere in the repository then leaves its asset hash as it
was, and starts no instance refresh. Issue E3-04 has a vllm pool's release build the image from
that folder.

## Deliverables

- [ ] `services/engine/vllm/Dockerfile` starts from the upstream vLLM image named by digest, at version 0.33.0 or later, sets VLLM_NO_USAGE_STATS=1, DO_NOT_TRACK=1 and HF_HUB_OFFLINE=1 in the image's environment, sets neither VLLM_SERVER_DEV_MODE nor VLLM_TORCH_PROFILER_DIR, and makes this project's entry point the image's.
- [ ] `services/engine/vllm/entrypoint.sh` sets VLLM_API_KEY from RHI_POOL_SECRET, the variable in which the engine's unit gives the container the pool's secret, and starts vLLM's server with the arguments the container receives, adding none.
- [ ] `services/engine/vllm/VERSION` records the upstream version that digest is, for the advisory check of issue E4-25, and `services/engine/vllm/` holds nothing but the Dockerfile, the entry point and that file, since it is the image's whole build context.
- [ ] `tests/unit/engine/test_vllm_image.py` reads the Dockerfile and the version, and runs the entry point with a stand-in for vLLM.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_vllm_image.py` passes, and fails when the Dockerfile names its base by tag or when the recorded version is below 0.33.0.
- [ ] `tests/unit/engine/test_vllm_image.py::test_the_contract_environment` finds the three variables set to 1 in the Dockerfile and neither of the two others.
- [ ] `tests/unit/engine/test_vllm_image.py::test_the_secret_reaches_vllm` runs the entry point with a secret in the unit's variable and a stand-in for vLLM, and finds VLLM_API_KEY holding it and the arguments passed on unchanged, the secret among none of them.
- [ ] `bash -n services/engine/vllm/entrypoint.sh` exits 0.
- [ ] In the account, the pipeline builds the image into ECR and an instance of the coursework pool runs it (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 4.3, 6.1
- `docs/design/evidence.md`, E3, E23, E28
