# Contributing

The working conventions for this repository (the toolchain, what must never be written in a
public repository, branching, and how `main` is protected) live in one place:
[`AGENTS.md`](AGENTS.md), read by people and by every coding agent alike. This file is a short
pointer; **`AGENTS.md` is the source of truth.**

Most changes here are made by the repository's software factory from issues
([ADR 0001](docs/adr/0001-software-factory.md)). A change made by hand follows the same path and
passes the same gates.

## First time here

Follow [`setup/README.md`](setup/README.md): install the prerequisites and activate the git
hooks.

## Making a change

1. Branch off `main`: `git checkout -b <type>/<short-description>` (for example
   `feat/gateway-auth`, `fix/null-deref`, `docs/runbook`). `main` is protected: no direct commits
   or pushes.
2. Make the change and run `scripts/check.sh fast` (or `all`) before pushing.
3. Land it through a pull request: run `/ship` (Claude Code) or `$ship` (Codex), or
   `scripts/ship.sh` and `gh pr create`. The `required` check must be green. Do **not** bypass the
   hooks with `--no-verify` or `--force`.

## What never goes in

This repository is public and deploys to a real AWS account. No account ID, no ARN with an
account, no credential, no deployed host name, no real domain, no personal e-mail address, in
files, commit messages, issues or pull requests. Use the placeholders listed in
[`AGENTS.md`](AGENTS.md#public-repository-hygiene). The hooks and CI run
[`tools/hygiene/`](tools/hygiene/README.md) to catch what a pattern can catch.

Pull requests never receive AWS credentials or secrets, from a fork or otherwise, and nothing in
CI deploys.
