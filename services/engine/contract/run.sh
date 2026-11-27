#!/bin/bash
# The engine contract check on an instance (system design, "The engine contract"): the four tests
# of tests/contract/test_engine_contract.py that an engine at an address answers, run against the
# node's own engine on the loopback address. The document rhi-engine-contract runs it, as root,
# with bash: the release's archive gives no file the executable mode (rhi.pools.contract_check).
#
# What it uses of the instance, as the units do:
# - the release's settings in /run/rhi/release.env, which the boot program wrote: the region, the
#   name of the pool's secret and RHI_ENGINE, the engine's row of metric names;
# - the pool's secret, read as the engine's unit reads it (rhi-engine.service): from Secrets
#   Manager with the instance role, held in the memory of this shell and given to the container
#   on its standard input only, never in an argument, an --env option or a file;
# - the Python image the lifecycle agent's unit names by digest, which the host holds, read from
#   the release's own copy of that unit;
# - /opt/rhi/release/contract, the suite, its packages and its libraries, mounted read-only as the
#   container's working directory, where Python finds them;
# - the host's network, so that http://127.0.0.1:8000 is the engine's port.
#
# What leaves the node. Run Command keeps a command's output in the account, so this script prints
# only a fixed vocabulary, which any reader of the output may copy: one line
# `rhi-contract <test> <outcome>` for each of the four tests, the outcome passed, failed, error,
# skipped or missing (the test did not run, or the run stopped before pytest), then
# `rhi-contract exit <status>`, and it exits with that status, pytest's. pytest's own report goes
# nowhere: a failure's report carries the engine's answers and the test's arguments, the secret
# among them. The plugin rhi_contract_outcomes.py keeps each test's outcome in memory and writes
# only the test's name and its outcome to the descriptor 3, which the container's shell opens on
# its standard output; this script keeps a line only if it is one of those words. Every
# program's standard error, AWS's text included, goes nowhere.
#
# RHI_CONTRACT_ROOT, only for the tests, puts the paths above under a folder of their own.

set -uo pipefail

root="${RHI_CONTRACT_ROOT:-}"
release="$root/opt/rhi/release"
contract=/opt/rhi/release/contract
address=http://127.0.0.1:8000
tests=(test_inference test_health test_metrics test_bearer_secret)
declare -A outcomes=()

exec 2>/dev/null

report() {
  local name
  for name in "${tests[@]}"; do
    printf 'rhi-contract %s %s\n' "$name" "${outcomes[$name]:-missing}"
  done
  printf 'rhi-contract exit %s\n' "$1"
  exit "$1"
}

region="" secret_id="" engine=""
while IFS='=' read -r name value; do
  case "$name" in
    RHI_REGION) region="$value" ;;
    RHI_SECRET_ID) secret_id="$value" ;;
    RHI_ENGINE) engine="$value" ;;
  esac
done <"$root/run/rhi/release.env" || report 1
[ -n "$region" ] && [ -n "$secret_id" ] && [ -n "$engine" ] || report 1

mapfile -t images < <(grep --only-matching --extended-regexp 'python:[^[:space:]@]+@sha256:[0-9a-f]{64}' \
  "$release/units/rhi-lifecycle.service")
[ "${#images[@]}" -eq 1 ] || report 1

secret="$(aws secretsmanager get-secret-value --region "$region" --secret-id "$secret_id" \
  --query SecretString --output text)" || report 1
[ -n "$secret" ] || report 1

nodes=()
for name in "${tests[@]}"; do
  nodes+=("tests/contract/test_engine_contract.py::$name")
done
# The container's first process: a shell that reads the secret into RHI_POOL_SECRET, where the
# suite reads it, sends its own output and pytest's nowhere, keeping the descriptor 3 on its
# standard output for the plugin, and becomes pytest.
inner='IFS= read -r RHI_POOL_SECRET; [ -n "$RHI_POOL_SECRET" ] || exit 4; export RHI_POOL_SECRET; '
inner+='exec 3>&1 >/dev/null 2>&1; '
inner+='exec python -m pytest -p no:cacheprovider -p rhi_contract_outcomes --import-mode=importlib "$@"'
output="$(docker run --rm --interactive --network host \
  --volume "$release/contract:$contract:ro" --workdir "$contract" \
  --env "RHI_ENGINE_URL=$address" --env "RHI_ENGINE_NAME=$engine" --env PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint /bin/sh "${images[0]}" -c "$inner" rhi-contract "${nodes[@]}" \
  < <(printf %s "$secret"))"
status=$?
unset secret

while read -r name outcome rest; do
  [ -z "$rest" ] || continue
  for known in "${tests[@]}"; do
    if [ "$name" = "$known" ]; then
      case "$outcome" in
        passed | failed | error | skipped) outcomes[$known]="$outcome" ;;
      esac
    fi
  done
done <<<"$output"
report "$status"
