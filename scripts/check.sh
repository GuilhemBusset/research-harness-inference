#!/usr/bin/env bash
# The one gate script: the same code on a laptop, in the implement loop's verifier and in CI
# (.github/workflows/ci.yml), so "green here" and "green there" mean the same thing.
#
#   scripts/check.sh hygiene   the public-repository scanner over the tree (python3 only, no uv)
#   scripts/check.sh fast      hygiene, shell and JSON syntax, the lock, ruff, ty
#   scripts/check.sh all       fast, then the tests, the engine tests, the skill pairs, the
#                              backlog lint, the ship script's test, and `cdk synth` without
#                              credentials
#
# A single step can be run by its name: static, lock, lint, types, tests, engine-tests, skills,
# backlog-lint, ship-tests, synth, container-tests. CI's jobs call those. Every step of a run is
# attempted, each result is printed, and the exit status is 1 if any step failed, 2 on a usage
# error.
#
# container-tests runs the tests that start a container (tests/container/). It fails when no
# container runtime answers, so fast and all do not run it: CI's job container-tests does.
#
# This script is a protected path and part of the safety surface
# (docs/adr/0001-software-factory.md, decision 10).
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 2

# No gate may depend on, or see, AWS credentials: pull-request checks run without any
# (docs/design/system-design.md, "Checks on a pull request"). Drop what the caller's shell carries and switch
# off the SDKs' other sources (profile files, the instance metadata service).
while IFS= read -r name; do
  unset "$name"
done < <(compgen -e | grep -E '^(AWS_|AMAZON_|CDK_DEFAULT_)' || true)
export AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
# uv would otherwise load variables from a file the caller's shell names.
unset UV_ENV_FILE
export UV_NO_ENV_FILE=1

# The pins of the engine tests command; they move together with pyproject.toml's.
engine_python=3.12
engine_pytest='pytest==8.3.5'
engine_xdist='pytest-xdist==3.8.0'
skills=(implement ship backlog evaluate)

failed=()

step() {   # step <name> <function>: run one step, print its result, remember a failure
  local name="$1"
  shift
  printf '\n[check] %s\n' "$name"
  if "$@"; then
    printf '[check] %s: ok\n' "$name"
  else
    printf '[check] %s: FAILED\n' "$name"
    failed+=("$name")
  fi
}

do_hygiene() {
  python3 tools/hygiene/scan.py
}

do_static() {   # every shell script parses; the ruleset is valid JSON
  local rc=0 f
  for f in scripts/*.sh setup/*.sh .githooks/*; do
    [ -f "$f" ] || continue
    bash -n "$f" || rc=1   # `bash -n a b` parses only `a`, so one file at a time
  done
  python3 -m json.tool setup/pr-only-main.json >/dev/null || rc=1
  return "$rc"
}

do_lock() {   # fails when uv.lock no longer matches pyproject.toml; never rewrites it
  uv sync --locked
}

do_lint() {
  uv run --locked ruff check .
}

do_types() {
  # Without PYTHONPATH: since 0.0.104, ty puts its entries before the roots of ty.toml, and the
  # implement loop's verifier sets it to the engine of the main checkout, which would then stand
  # for the `implement_loop` of the branch under check.
  env -u PYTHONPATH uv run --locked ty check
}

do_tests() {
  uv run --locked pytest
}

skip_report() {   # skip_report <step> <junit file>: what pytest collected and what it skipped
  # In CI everything a test needs is installed, so a skipped test there is lost coverage and
  # fails the step; on another machine skips are listed and tolerated. A run that collected
  # nothing fails everywhere.
  python3 - "$1" "$2" <<'EOF'
import os
import sys
import xml.etree.ElementTree as ET

step = sys.argv[1]
try:
    suites = list(ET.parse(sys.argv[2]).getroot().iter("testsuite"))
except ET.ParseError:
    sys.exit(f"[check] {step}: pytest wrote no report")
tests = sum(int(s.get("tests", 0)) for s in suites)
skipped = [f'{c.get("classname")}::{c.get("name")}' for s in suites for c in s.iter("testcase")
           if c.find("skipped") is not None]
print(f"[check] {step}: {tests} collected, {len(skipped)} skipped")
for name in skipped:
    print(f"[check]   skipped: {name}")
if tests == 0:
    sys.exit(f"[check] {step}: nothing was collected")
if skipped and os.environ.get("CI"):
    sys.exit(f"[check] {step}: a test skipped in CI; install what it needs")
EOF
}

do_engine_tests() {
  # The implement loop's own tests, on the oldest Python it supports. A skipped test fails the
  # step under CI (skip_report).
  local junit rc=0
  junit="$(mktemp)"
  uv run --no-project --python "$engine_python" --with "$engine_pytest" --with "$engine_xdist" \
    python -m pytest -q -n auto -p no:cacheprovider --junitxml="$junit" tools/implementation-loop/tests || rc=1
  skip_report engine-tests "$junit" || rc=1
  rm -f "$junit"
  return "$rc"
}

do_container_tests() {
  # The tests that can only be made by starting a container (tests/container/README.md;
  # docs/design/system-design.md, "Checks on a pull request"). The folder is named to pytest,
  # because pyproject.toml keeps it out of a run that names no path. Without a runtime the step
  # fails instead of skipping, so a green result always means the tests ran. A skipped test
  # fails the step under CI (skip_report).
  if [ -z "$(find tests/container -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit 2>/dev/null)" ]; then
    echo "[check] container-tests: tests/container/ holds no test yet, nothing to run"
    return 0
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "[check] container-tests: no container runtime answers (\`docker info\` failed); CI's job container-tests runs these tests" >&2
    return 1
  fi
  local junit rc=0
  junit="$(mktemp)"
  uv run --locked pytest -p no:cacheprovider --junitxml="$junit" tests/container || rc=1
  skip_report container-tests "$junit" || rc=1
  rm -f "$junit"
  return "$rc"
}

do_skills() {   # Claude Code reads .claude/skills/, Codex reads .agents/skills/: the copies never differ
  local rc=0 name
  for name in "${skills[@]}"; do
    diff -u ".claude/skills/$name/SKILL.md" ".agents/skills/$name/SKILL.md" || rc=1
  done
  return "$rc"
}

do_backlog_lint() {   # every issue draft is one the implement loop can finish (backlog/README.md)
  if [ -z "$(find backlog/drafts -type f -name '*.md' -print -quit 2>/dev/null)" ]; then
    echo "[check] backlog-lint: backlog/drafts/ holds no draft, nothing to lint"
    return 0
  fi
  bash scripts/backlog.sh lint
}

do_ship_tests() {   # repeated publication never needs --force; runs offline, with the real hooks
  bash scripts/test-ship.sh
}

do_synth() {
  # `cdk synth` must work with no credentials and no context lookups
  # (docs/design/system-design.md, "Keeping the repository public-safe"). The CDK CLI is the one package.json and
  # package-lock.json pin, never a global install.
  if [ ! -f cdk.json ]; then
    echo "[check] synth: no cdk.json yet, nothing to synthesize (the CDK app arrives with the first backlog epic)"
    return 0
  fi
  if [ ! -f package.json ] || [ ! -f package-lock.json ]; then
    echo "[check] synth: cdk.json exists but package.json and package-lock.json do not pin the CDK CLI" >&2
    return 1
  fi
  if [ -n "${CI:-}" ] || [ ! -x node_modules/.bin/cdk ]; then
    npm ci --ignore-scripts --no-audit --no-fund || return 1
  fi
  # Against the example configuration, whatever source the caller's environment names
  # (docs/design/system-design.md, "Checks on a pull request"): the loader fails closed when
  # RHI_CONFIG is unset. The variable is set for this command only, so no other step sees it.
  RHI_CONFIG=example node_modules/.bin/cdk synth --lookups=false --quiet
}

usage() {
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

case "$1" in
  hygiene)      step hygiene do_hygiene ;;
  fast)
    step hygiene do_hygiene
    step static do_static
    step lock do_lock
    step lint do_lint
    step types do_types
    ;;
  all)
    step hygiene do_hygiene
    step static do_static
    step lock do_lock
    step lint do_lint
    step types do_types
    step tests do_tests
    step engine-tests do_engine_tests
    step skills do_skills
    step backlog-lint do_backlog_lint
    step ship-tests do_ship_tests
    step synth do_synth
    ;;
  static)       step static do_static ;;
  lock)         step lock do_lock ;;
  lint)         step lint do_lint ;;
  types)        step types do_types ;;
  tests)        step tests do_tests ;;
  engine-tests) step engine-tests do_engine_tests ;;
  skills)       step skills do_skills ;;
  backlog-lint) step backlog-lint do_backlog_lint ;;
  ship-tests)   step ship-tests do_ship_tests ;;
  synth)        step synth do_synth ;;
  container-tests) step container-tests do_container_tests ;;
  *)
    usage
    exit 2
    ;;
esac

if [ "${#failed[@]}" -gt 0 ]; then
  printf '\n[check] FAILED: %s\n' "${failed[*]}"
  exit 1
fi
printf '\n[check] %s: all steps passed\n' "$1"
