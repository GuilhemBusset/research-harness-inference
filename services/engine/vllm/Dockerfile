# The vLLM engine's image (system design, sections 4.1 and 4.3): a thin layer over the upstream
# vLLM image, which the pipeline builds into ECR so that an instance pulls it from the account's
# own registry, by the digest its pool's release names.
#
# Built from the context src/rhi/packaging.py's `upstream_image_context` gives: this file alone.
# The layer adds nothing yet: what a GPU pool's engine is started with (its flags, its weights)
# belongs to the pool's release and to the unit that runs the image.
#
# The base is the upstream release the design's evidence was read at (docs/design/evidence.md),
# pinned by the digest of its multi-architecture index, which is published for linux/amd64 and
# linux/arm64. No platform is fixed here: the pipeline gives the one of the pool's instance
# types, linux/amd64 for the GPU pools. An update is a change to this line.
FROM vllm/vllm-openai:v0.33.0@sha256:d825baf9ce4b1b6bfc5ecc7b79880c7e6748c65a72d697ce3d47607aafa84c6e
