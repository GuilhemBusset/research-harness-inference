#!/usr/bin/env bash
# Terminal entry point of the evaluation of agent sessions (docs/design/agent-session-evaluation.md,
# "How the operator runs it").
#
#   scripts/evaluate.sh plan --suite course-v1 --profile qualify --students 8 --instances 1
#
# `plan` previews the work, the time and the host-hours of a profile. It is offline: it needs no
# credentials and no network. Add `--json` for one JSON document; `--for 3h` plans bookings of at
# most that long, and `--rate` prices them at a rate of your own (tools/evaluate/cli.py).
#
#   scripts/evaluate.sh report .cache/eval/<run>
#   scripts/evaluate.sh compare .cache/eval/reference .cache/eval/candidate
#   scripts/evaluate.sh export .cache/eval/<run> --to docs/evaluations/<name>
#
# These read saved evidence bundles, offline too: `report` regenerates a run's scorecard and its
# digest, `compare` prints the treatment diff of two runs and then their comparison, and `export`
# writes a validated, scanned copy of a bundle for review in a pull request (tools/evaluate/report.py).
#
# The tool is the package tools/evaluate, run with the locked environment (`uv run --locked`). The
# project does not install it, so this script puts `tools` on the import path itself, with
# `spikes` and the services `spikes/common.py` and `spikes/loadgen.py` import, which the readers of
# evidence need: it works from any folder and from a shell with no PYTHONPATH.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
import_path="$repo_root/tools:$repo_root/spikes:$repo_root/services/common:$repo_root/services/gateway:$repo_root/services/reporter"
exec env PYTHONPATH="$import_path" uv run --locked --project "$repo_root" python -m evaluate.cli "$@"
