# The vLLM engine's image (system design, sections 4.1 and 4.3): a thin layer over the upstream
# vLLM image, which the pipeline builds into ECR so that an instance pulls it from the account's
# own registry, by the digest its pool's release names.
#
# The build context is this folder and nothing else: this file, entrypoint.sh and VERSION. The
# image holds no package of this project and installs nothing.
#
# A pool's release carries the engine's flags as arguments and nothing else of the engine, so what
# vLLM reads from its environment is set here, the same for every vllm pool.
#
# The base is the upstream release the design's evidence was read at (docs/design/evidence.md),
# pinned by the digest of its multi-architecture index, which is published for linux/amd64 and
# linux/arm64. No platform is fixed here: the pipeline gives the one of the pool's instance
# types, linux/amd64 for the GPU pools. An update is a change to this line and to VERSION, which
# records the upstream version this digest is.
FROM vllm/vllm-openai:v0.33.0@sha256:d825baf9ce4b1b6bfc5ecc7b79880c7e6748c65a72d697ce3d47607aafa84c6e

# The contract settings (section 4.1). The first two each turn off vLLM's usage report
# (vllm/envs.py and vllm/usage/usage_lib.py at the tag); with the third, vLLM and the Hugging
# Face library it loads a model with never call Hugging Face. The variables that open the
# development and profiling routes are not set, here or in the entry point.
ENV VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 \
    HF_HUB_OFFLINE=1

COPY entrypoint.sh /usr/local/bin/rhi-entrypoint.sh

# This project's entry point in place of upstream's `vllm serve`, which it ends by running. Exec
# form, and the shell is named, so the file's mode does not matter; the image has no command of
# its own, so what the container is given after the entry point is vLLM's arguments.
ENTRYPOINT ["/bin/sh", "/usr/local/bin/rhi-entrypoint.sh"]
