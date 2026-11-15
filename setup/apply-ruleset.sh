#!/usr/bin/env bash
# One-command, idempotent application of the repository's server-side ruleset:
#
#   setup/pr-only-main.json   pull-request-only changes to the default branch, the `required`
#                             CI check green on the merged head, a code owner's approving
#                             review for a change to a path listed in .github/CODEOWNERS (the
#                             protected paths), no force-push, no deletion, and nobody on the
#                             bypass list.
#
# The definition in the repository binds nothing until this script has applied it, and applying
# it needs a repository admin: it is the owner's step, again after each change to the JSON file.
# To see the review bind afterwards, take a pull request that touches a protected path:
# `gh pr view <number> --json reviewDecision` prints REVIEW_REQUIRED until the owner approves.
# GitHub reads CODEOWNERS from the base branch, so the file must be on the default branch first.
#
# A ruleset is server state of one repository: it is not inherited by forks or new repositories,
# so the definition is checked in and this script applies it, matched by name (created if
# missing, updated otherwise). Run it once per repository, after the green light
# (docs/factory/green-light.md), with a gh CLI authenticated as a repository admin:
#
#   ./setup/apply-ruleset.sh
#
# Until it has run, the audit workflow (.github/workflows/pr-only.yml) and the local git hooks
# are the only backstop. Fails if the ruleset cannot be applied. Requires: gh (authenticated), jq.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

ruleset_file=setup/pr-only-main.json
name="$(jq -r .name "$ruleset_file")"

existing_ids="$(gh api --paginate "repos/$repo/rulesets" \
  --jq ".[] | select(.name == \"$name\") | .id")"
existing_id="${existing_ids%%$'\n'*}"

if [ -n "$existing_id" ]; then
  gh api --method PUT "repos/$repo/rulesets/$existing_id" \
    --input "$ruleset_file" >/dev/null
  echo "[ruleset] Updated ruleset '$name' (id $existing_id) on $repo."
else
  gh api --method POST "repos/$repo/rulesets" \
    --input "$ruleset_file" >/dev/null
  echo "[ruleset] Created ruleset '$name' on $repo."
fi
