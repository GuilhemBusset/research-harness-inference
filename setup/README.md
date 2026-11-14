# Repository setup

What a machine needs to work in this repository, and the one-time scripts that switch its gates
on. Run every command from the repository root.

**The owner runs these scripts, at the steps of [`docs/factory/green-light.md`](../docs/factory/green-light.md)
that call them.** No agent runs them.

## Prerequisites

| For | Needed | Notes |
| --- | --- | --- |
| Any change | `git`, `bash`, Python 3.12 or newer as `python3` | The hygiene scanner and the hooks are standard-library Python and Bash |
| `scripts/check.sh fast` and `all` | [uv](https://docs.astral.sh/uv/) | CI pins uv 0.11.28 (`.github/workflows/ci.yml`). uv fetches the Python in `.python-version`, and Python 3.12 for the engine tests |
| The implement loop | The GitHub CLI (`gh`) logged in, the Claude Code and Codex CLIs both logged in, bubblewrap (`bwrap`) | See [`docs/factory/operating.md`](../docs/factory/operating.md); `scripts/implement.sh doctor` checks all of it |
| `setup/apply-ruleset.sh` | `gh` logged in as a repository admin, `jq` | Once per repository |
| `cdk synth`, once the CDK app exists | Node.js 22 or 24 with npm | The CDK CLI does not support other Node versions. It is installed from `package-lock.json` into `node_modules/`, never globally |
| `scripts/check.sh container-tests` | A container runtime that answers `docker info` | Only for the tests under `tests/container/`, which CI runs. `fast` and `all` do not need it |

No AWS credentials are needed for anything in this repository, and none should be reachable from
the shell an agent runs in. Deployment is CodePipeline's.

## Python environment

```sh
uv sync --locked
```

Creates `.venv/` (ignored by git) with the packages locked in [`uv.lock`](../uv.lock): pytest,
pytest-xdist, PyYAML, Ruff and ty, at the exact versions in [`pyproject.toml`](../pyproject.toml).
`scripts/check.sh` does this itself. Remove it with `rm -rf .venv`.

## `git-hooks.sh`: activate the git hooks (every clone, after the green light)

```sh
bash setup/git-hooks.sh
```

Sets the clone's `core.hooksPath` to `.githooks/`. From then on:

- `pre-commit` refuses a commit on `main` or `master` (the first commit of a new repository
  excepted) and scans the staged change with [`tools/hygiene/scan.py`](../tools/hygiene/README.md);
- `pre-push` refuses a push to `main` or `master` and scans the tree at the tip of every pushed
  branch.

It is safe to repeat, changes nothing but this clone's `.git/config`, and is undone with
`git config --unset core.hooksPath`. Run it yourself, once per clone; agents do not activate it.
The hooks then run for everyone who commits in the clone, including the implement loop, which is
why `.githooks/` is a protected path.

What the hooks are not: a guarantee. `core.hooksPath` is relative, so each work tree runs the
hooks, the scanner and the `hygiene.toml` of its own checkout, as they are on disk: a work tree
checked out at a commit without `.githooks/` runs none, and an uncommitted edit to the scanner
applies at once. `git commit --no-verify` skips them, and so do `git merge`, `git cherry-pick`
and `git am`, which never run `pre-commit`. `pre-push` reads the tip of a branch, not the commits
below it. CI runs the committed scanner on a clean machine, over the tree (job `hygiene`) and
over the history (job `history`), both part of `required`: that is the check that counts.

## `apply-ruleset.sh`: apply the GitHub ruleset (once per repository, after the green light)

```sh
bash setup/apply-ruleset.sh
```

Creates or updates, matched by name, the ruleset defined in
[`pr-only-main.json`](pr-only-main.json) on the repository `gh` resolves from the `origin`
remote:

- every change to the default branch goes through a pull request;
- the `required` status check, reported by GitHub Actions (`.github/workflows/ci.yml`), must be
  green on the head that is merged;
- no force-push, no deletion;
- an empty bypass list: the rule binds the owner and the factory alike.

A ruleset is server state of one repository. A clone, a fork or a copy does not carry it, which
is why the definition is checked in and why `.github/workflows/pr-only.yml` audits `main` after
the fact. Re-running the script updates the ruleset in place; it fails if the ruleset cannot be
applied. On a personal account without a paid plan, GitHub enforces rulesets on public
repositories only: the green-light checklist says how to check that the rule really binds.

## Installation inventory

| Component | Source and destination |
| --- | --- |
| `git`, `bash`, `python3`, `uv`, `gh`, `jq`, the agent CLIs, bubblewrap, Node.js | Installed by the contributor; nothing in this repository installs or changes a global tool |
| Python tools (pytest, pytest-xdist, Ruff, ty) and PyYAML, the parser of the test that reads CI's workflow | Exact versions and hashes in `uv.lock`; installed by `uv sync --locked` into `.venv/` |
| pytest for the engine tests | `uv run --no-project --with pytest==8.3.5 --with pytest-xdist==3.8.0` fetches them, and Python 3.12 if needed, into uv's per-user cache |
| Git hooks | Checked in under `.githooks/`; `git-hooks.sh` points this clone's `.git/config` at them |
| GitHub ruleset | `pr-only-main.json`; applied to the `origin` repository only when an admin runs `apply-ruleset.sh` |
| Hygiene scanner | Checked in under `tools/hygiene/` (standard-library Python); installs nothing |
| Implement loop | Checked in under `tools/implementation-loop/` (standard-library Python); where its run state and worktrees live is described in `docs/factory/operating.md` |
| Skills | Checked in under `.claude/skills/` and `.agents/skills/`; no global skill installation, no MCP server |
| CI tools | `astral-sh/setup-uv` installs the pinned uv on the runner; gitleaks and actionlint are downloaded at a pinned version and verified against a pinned SHA-256 (`.github/workflows/ci.yml`) |

When adding a repository-wide tool, put its installer and manifest here or at the root, pin it,
add it to this inventory, and verify the setup on a clean machine.
