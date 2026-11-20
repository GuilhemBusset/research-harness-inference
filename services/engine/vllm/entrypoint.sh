#!/bin/sh
# The entry point of the vLLM engine's image (system design, section 4.1).
#
# The engine's unit gives the container the pool's secret in RHI_POOL_SECRET, the same variable
# for every engine. vLLM reads its bearer secret from VLLM_API_KEY, so this puts it there and
# then becomes vLLM's server, as the upstream image starts it (`vllm serve`), with the arguments
# the container received and no other. The secret is in no argument.
#
# With no secret vLLM would answer anyone who reaches it, so there is no start without one.

set -eu

if [ -z "${RHI_POOL_SECRET:-}" ]; then
  echo "rhi-entrypoint: RHI_POOL_SECRET holds no secret" >&2
  exit 1
fi

VLLM_API_KEY="$RHI_POOL_SECRET"
export VLLM_API_KEY

exec vllm serve "$@"
