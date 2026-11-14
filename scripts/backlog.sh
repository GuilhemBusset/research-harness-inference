#!/usr/bin/env bash
# Terminal entry point of the backlog tooling (the `backlog` skill; docs/adr/0001-software-factory.md,
# decision 7). The backlog is drafted as files in backlog/drafts/ and published as GitHub issues.
#
#   scripts/backlog.sh lint               # every draft against the implement loop's input contract
#   scripts/backlog.sh plan [--zones]     # the order and waves the dependencies give
#   scripts/backlog.sh publish            # dry run: what would be created on GitHub; nothing is sent
#   scripts/backlog.sh publish --apply    # create it: issues, sub-issue and dependency links, labels
#
# The tool is tools/backlog/backlog.py (standard library only) and needs Python 3.12+ and git;
# `publish --apply` also needs an authenticated GitHub CLI. Format and lifecycle: backlog/README.md.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$repo_root/tools/backlog/backlog.py" "$@"
