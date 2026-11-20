#!/bin/sh
# The entry point of the vLLM engine's image (system design, section 4.1).
#
# The engine's unit gives the container the pool's secret in RHI_POOL_SECRET, the same variable
# for every engine. vLLM reads its bearer secret from VLLM_API_KEY, so this puts it there and
# then becomes vLLM's server, as the upstream image starts it (`vllm serve`), with the arguments
# the container received and no other. The secret is in no argument.
#
# With no secret vLLM would answer anyone who reaches it, so there is no start without one.
#
# A pool on the fallback loader (section 4.4) has engine flags that begin with one argument of
# this project, --rhi-fetch-model=<the model's location>,<the local folder>. Then the program
# beside this file, fetch_model.py, copies the model to the folder first, and the argument is
# taken off: vLLM is started with the rest, which name the folder as its model. A copy that fails
# starts no vLLM. Any other first argument, as a pool on the streamer has, starts vLLM at once.

set -eu

if [ -z "${RHI_POOL_SECRET:-}" ]; then
  echo "rhi-entrypoint: RHI_POOL_SECRET holds no secret" >&2
  exit 1
fi

case "${1-}" in
  --rhi-fetch-model=*)
    python3 "${0%/*}/fetch_model.py" "$1"
    shift
    ;;
esac

VLLM_API_KEY="$RHI_POOL_SECRET"
export VLLM_API_KEY

exec vllm serve "$@"
