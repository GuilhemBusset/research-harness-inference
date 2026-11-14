+++
id = "E3-44"
title = "vLLM: the fallback loader, the model copied to the EBS volume before vLLM starts"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E1-16", "E3-04", "E3-38"]
+++

## Context

The loader is a per-pool setting (section 4.4): the Run:ai streamer from S3, which issue E3-04
builds, or "download to local disk, then load" as the fallback. AWS's measurements of the streamer
come from an instance with far more network bandwidth than a single-GPU type has, defaults can be
ten times slower, and one model was reported slower than the plain loader (evidence E11); spike S4
compares the two on the coursework pool (issue E3-18), so both must exist before it runs.

vLLM reads a model from a local folder with its default loader, but it does not copy a model out of
S3 by itself. So the engine's image carries one small program of this project,
`services/engine/vllm/fetch_model.py`, which the image's entry point (issue E3-03) runs before vLLM
when the release's arguments ask for a copy: the arguments of a pool on download begin with one
argument of this project, which names the model's location and the local folder, and which the entry
point takes off before it starts vLLM with the rest. For a pool on the streamer the arguments hold no
such argument and the entry point starts vLLM at once, as before.

The program runs inside the upstream vLLM image with what that image ships: the builder checks, at
the pinned digest, that the S3 client it imports is installed there. It reads the bucket with the
instance role's credentials from the instance metadata, which the GPU drop-in's host network lets it
reach (issue E3-38). The folder is in the container's own file system, which Docker keeps on the EBS
volume (issue E2-70), so the engine's unit needs no new mount; the copy is made at every start,
which is what spike S4 measures against the streamer. `services/engine/vllm/` is not a package
root, so the test loads the program by its path, and ruff and ty name the file.

The new pool field is loader, stream by default, so that every pool that leaves it out keeps the
streamer and the deployed configuration needs no new value. Its value reaches the engine in the
release's engine flags (issue E3-04). The example configuration documents it in a comment.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives a vllm pool the field loader, stream or download, stream by default, and refuses any other value, naming the pool and the field.
- [ ] `config.example.toml` documents the field and its default in a comment.
- [ ] `src/rhi/pools/vllm_command.py` gives a pool on download arguments that begin with the copy's argument, naming the model's location and the local folder, and name that folder as vLLM's model with vLLM's default loader, and leaves a pool on stream as it was.
- [ ] `services/engine/vllm/fetch_model.py` copies every file under the model's location to the local folder, `services/engine/vllm/entrypoint.sh` runs it and takes the copy's argument off before it starts vLLM when the arguments begin with that argument, and `services/engine/vllm/Dockerfile` adds the program to the image.
- [ ] `ruff.toml` and `ty.toml` include `services/engine/vllm/fetch_model.py` in their scope.
- [ ] `tests/unit/pools/test_vllm_command.py`, `tests/unit/common/test_pool_config.py` and `tests/unit/engine/test_fetch_model.py` cover the field, both kinds of arguments, the copy and the entry point.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/pools/test_vllm_command.py` passes: a pool on download gets a local model folder on the EBS volume and no streamer option, a pool without the field gets the streamer, and both keep every contract setting of issue E3-04.
- [ ] `uv run --locked pytest tests/unit/engine/test_fetch_model.py` passes: against a stubbed bucket of three files, the entry point has the program copy all three to the folder before it starts the stand-in for vLLM without the copy's argument, and with a streamer pool's arguments copies nothing.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a pool without the field reads stream, and a loader of disk is refused.
- [ ] On the coursework pool deployed with the loader download, vLLM loads the model from the EBS volume and serves (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.4, 5.2, 12
- `docs/design/evidence.md`, E11
