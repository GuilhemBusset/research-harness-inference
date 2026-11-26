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
#   scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new
#   scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 1 \
#       --session existing --issue "$experiment_issue"
#   scripts/evaluate.sh run --resume [<run-id>] <the same request> --for 90m --issue "$experiment_issue"
#   scripts/evaluate.sh status <run-id>
#   scripts/evaluate.sh cancel <run-id>
#   scripts/evaluate.sh publish --resume <run-id>
#   scripts/evaluate.sh compare --issue "$experiment_issue" <reference-run-id> <candidate-run-id>
#
# `run` is one bounded invocation of the operator controller (tools/evaluate/controller.py) on an
# already-deployed pool: preflight, the experiment's issue, the booking, the cells, the cleanup, the
# local report and the terminal comment. It needs the driver's AWS credentials, its GitHub token
# (RHI_EVALUATE_GITHUB_TOKEN) and the experiment's repository (--repository or
# RHI_EVALUATE_REPOSITORY). It prints its progress, the time left, the issue's URL, both verdicts
# and their limiting rows, the report's paths, the cleanup and the publication. Exit codes of `run`:
#
#   0  a completed profile that meets its applicable floors, at every load it measured
#   1  a measured miss of a completed profile
#   2  invalid, incomplete or inconclusive evidence, an invocation that failed (a fault of the
#      driver among them) or was interrupted (its verdict rows still show what it measured), or a
#      refusal
#
# The publication changes no exit code. It is `delivered` once GitHub confirmed every write,
# `pending` while the issue's creation or a comment waits, `refused` for a comment the scan refused:
# `status` shows it, and `publish --resume` delivers what waits with no booking and no GPU (0 once
# nothing is pending, else 2). `--for` bounds the whole invocation, the wait for capacity included.
# `cancel` asks the run's driver to stop; it releases the booking it made. The driver resolves the
# selected pool's gateway, telemetry, canaries and release from its own AWS session, and prepares
# the trial image on this host (tools/evaluate/live.py).
#
# The tool is the package tools/evaluate, run with the locked environment (`uv run --locked`). The
# project does not install it, so this script puts `tools` on the import path itself, with
# `spikes` and the services `spikes/common.py` and `spikes/loadgen.py` import, which the readers of
# evidence need, and the admin, pipeline and engine modules the controller books and measures with:
# it works from any folder and from a shell with no PYTHONPATH.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
import_path="$repo_root/tools:$repo_root/spikes:$repo_root/src:$repo_root/services/common:$repo_root/services/gateway"
import_path="$import_path:$repo_root/services/reporter:$repo_root/services/issuer:$repo_root/services/engine/lifecycle"
import_path="$import_path:$repo_root/admin:$repo_root/dashboards:$repo_root/scripts/pipeline"
exec env PYTHONPATH="$import_path" uv run --locked --project "$repo_root" python -m evaluate.cli "$@"
