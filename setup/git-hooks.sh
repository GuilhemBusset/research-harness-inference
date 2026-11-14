#!/usr/bin/env bash
# One-time, agent-neutral setup: activate this repository's git hooks.
#
# The hooks under .githooks/ protect `main` (no direct commit, no direct push) and run the
# public-repository hygiene scanner on every commit and push, but they are inert until
# core.hooksPath points at them. This script wires that up. Run it by hand once per clone, after
# the green light (docs/factory/green-light.md): no agent calls it, so activation does not depend
# on any one agent. Safe to run repeatedly.
#
#   ./setup/git-hooks.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "[setup] core.hooksPath -> .githooks (protected main and the hygiene scan are active in this clone)."
