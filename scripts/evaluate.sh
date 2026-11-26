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
# The tool is the package tools/evaluate, run with the locked environment (`uv run --locked`). The
# project does not install it, so this script puts `tools` on the import path itself: it works
# from any folder and from a shell with no PYTHONPATH.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec env PYTHONPATH="$repo_root/tools" uv run --locked --project "$repo_root" python -m evaluate.cli "$@"
