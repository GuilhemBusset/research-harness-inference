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
| `cdk synth` and the synthesis tests | Node.js 24 with npm, as `node` on the `PATH` | The version [`.node-version`](../.node-version) names; Node 22 works too, and the CDK supports no other. See [Node and the CDK CLI](#node-and-the-cdk-cli) |
| `scripts/check.sh container-tests` | A container runtime that answers `docker info` | Only for the tests under `tests/container/`, which CI runs. `fast` and `all` do not need it |
| `setup/owner-setup.sh` | The AWS CLI with the owner's credentials, `gh` logged in as a repository admin, `uv`, and the CDK CLI for its bootstrap step | The owner's one-time steps. Its `--dry-run` needs `uv` alone |

No AWS credentials are needed for anything else in this repository, and none should be reachable
from the shell an agent runs in. Deployment is CodePipeline's.

## Python environment

```sh
uv sync --locked
```

Creates `.venv/` (ignored by git) with the packages locked in [`uv.lock`](../uv.lock), at the
exact versions in [`pyproject.toml`](../pyproject.toml): the CDK libraries the app runs on
(aws-cdk-lib, constructs and cdk-nag), boto3 (the AWS SDK, with which the app reads its
configuration from SSM and writes it there), the tools (pytest, pytest-xdist, PyYAML, Ruff, ty,
moto and cfn-lint) and the libraries the services import. `scripts/check.sh` does this itself.
Remove it with `rm -rf .venv`.

The services' libraries are in dependency groups, one per service and named after it, listed in
the [installation inventory](#installation-inventory). The dev group, which `uv sync` installs,
includes every one of them, so the tests and ty see every library. A library a service imports
goes into that service's group, at one exact version, the same in every group that names it.

A deployed service never gets the dev group. [`src/rhi/packaging.py`](../src/rhi/packaging.py)
gives it its own group and nothing more, at the versions in `uv.lock`:

- a Lambda function's bundle holds `rhi_common`, the service's package and the group's libraries,
  which uv installs as wheels for the Lambda runtime's Python and architecture. This happens at
  `cdk synth`, on the machine that synthesizes, with no container: uv downloads the wheels from
  the package index into its cache, as `uv sync` does, and installs them into the bundle under
  `cdk.out/` (ignored by git). The synthesis tests skip it and download nothing;
- a container image's build context holds `rhi_common`, the service's package, its Dockerfile and
  a requirements file, exported from `uv.lock` for the group with every hash, which the Dockerfile
  installs when the pipeline builds the image.

## Node and the CDK CLI

```sh
npm ci --ignore-scripts
```

Creates `node_modules/` (ignored by git) with the CDK CLI, the only dependency of
[`package.json`](../package.json), at the exact version in
[`package-lock.json`](../package-lock.json). `scripts/check.sh synth` does this itself, and runs
the CLI as `node_modules/.bin/cdk`. Never install or run a global `cdk`. Remove it with
`rm -rf node_modules`.

Two things run on Node: the CDK CLI, and the synthesis tests, because aws-cdk-lib works through
jsii, which starts the `node` it finds on the `PATH`. The CDK supports Node 22 and 24. The
repository says which Node it expects in two places and enforces neither:

- [`.node-version`](../.node-version) names Node 24, for version managers;
- `package.json` declares the engines Node 22 or 24. npm prints a warning when another Node runs
  it, and still installs.

Nothing here installs Node. Get Node 24 from a version manager, which then selects it in this
repository from `.node-version`:

| Version manager | How it picks up `.node-version` |
| --- | --- |
| [mise](https://mise.jdx.dev/) | Only once idiomatic version files are on for Node: `mise settings add idiomatic_version_file_enable_tools node`. Then `mise install` installs the newest Node 24 |
| [fnm](https://github.com/Schniz/fnm) | `fnm install`, then `fnm use`, both with no argument; or on every `cd` when the shell is set up with `fnm env --use-on-cd` |
| [nodenv](https://github.com/nodenv/nodenv) | Reads the file, and looks for an installed version of exactly that name: `24` resolves once an alias of that name exists, which the nodenv-aliases plugin creates |
| `actions/setup-node` | Its `node-version-file` input, given `.node-version` |

A version manager changes the `node` of a shell it is active in. The implement loop's sandbox finds
`node` on the `PATH` of the host that runs it, so making Node 24 the default there is the owner's
step.

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
- a pull request that changes a path listed in [`.github/CODEOWNERS`](../.github/CODEOWNERS), the
  protected paths and nothing else, also needs the owner's approving review;
  `tests/policy/test_codeowners.py` holds that file to the engine's lists;
- no force-push, no deletion;
- an empty bypass list: the rule binds the owner and the factory alike.

A ruleset is server state of one repository. A clone, a fork or a copy does not carry it, which
is why the definition is checked in and why `.github/workflows/pr-only.yml` audits `main` after
the fact. Re-running the script updates the ruleset in place; it fails if the ruleset cannot be
applied. On a personal account without a paid plan, GitHub enforces rulesets on public
repositories only: the green-light checklist says how to check that the rule really binds.

## `owner-setup.sh`: the owner's one-time steps in the AWS account

```sh
setup/owner-setup.sh --dry-run
```

Walks through what only the owner can do, once (system design, section 8.6): the GPU quotas, the
domain, the GitHub Apps, the configuration in SSM, billing alerts, the bootstrap, the GitHub
connection, the repository's settings and the roster.
[`docs/runbooks/owner-setup.md`](../docs/runbooks/owner-setup.md) describes each step and how to
check it. The owner runs it, with their own credentials, from a shell no agent runs in.

It asks before each command it runs, and `--dry-run` prints every step and every command and
calls neither AWS nor GitHub. One step runs on its own with `--step <name>`: the configuration
step, `--step configuration --config <file>`, is also how the configuration is changed later.
The Apps step, `--step apps --reporter-key <file>`, reads each GitHub App back with `gh` and
stores the status reporter's credentials in the Secrets Manager secret the reporter reads.
The values step, `--step values --config <file>`, adds the configuration's private values to the
hygiene scanner's list of local values (`tools/hygiene/README.md`, "Values only the owner knows").

It installs nothing and writes nothing to the working tree. Python keeps no bytecode, and uv's
project environment and the CDK's output go to a temporary folder, under `TMPDIR`, that is removed
when the script ends: it needs no `uv sync` first, and leaves no `.venv` and no `cdk.out`.

## Installation inventory

| Component | Source and destination |
| --- | --- |
| `git`, `bash`, `python3`, `uv`, `gh`, `jq`, the agent CLIs, bubblewrap | Installed by the contributor; nothing in this repository installs or changes a global tool |
| Python tools (pytest, pytest-xdist, Ruff, ty, moto with its DynamoDB extra, and cfn-lint, which lints the synthesized templates and the templates under `setup/` in `tests/synth/test_cfn_lint.py`) and PyYAML, the parser of the test that reads CI's workflow | The dev group. Exact versions and hashes in `uv.lock`; installed by `uv sync --locked` into `.venv/` |
| CDK libraries (aws-cdk-lib, constructs, cdk-nag) | Exact versions and hashes in `uv.lock`; installed by `uv sync --locked` into `.venv/`, the project environment |
| boto3, the AWS SDK, for the app itself | In the project's dependencies for the ssm source and the writer of `src/rhi/config.py` (and the owner's walkthrough, `setup/owner-setup.sh`) only, at the version the service groups carry. With its `crt` extra, which brings botocore's `crt` extra and through it awscrt, the AWS Common Runtime: a compiled wheel the SDK needs to use a profile signed in with `aws login`. The service groups carry boto3 without the extra, so no bundle or image installs awscrt. Exact versions and hashes in `uv.lock`; installed by `uv sync --locked` into `.venv/` |
| Service libraries: the groups below | One dependency group per service in `pyproject.toml`, exact versions and hashes in `uv.lock`. `uv sync --locked` installs them all into `.venv/`, because the dev group includes every group. A function's bundle and an image install their service's group from the lock, and nothing else: uv installs the wheels into the bundle under `cdk.out/` at `cdk synth`, and an image's Dockerfile installs the requirements file exported from the lock with its hashes (`src/rhi/packaging.py`) |
| Group `gateway` | Starlette, httpx, uvicorn (without its native extras), prometheus-client and boto3, the AWS SDK |
| Group `mock-engine` | Starlette, uvicorn and prometheus-client |
| Group `scaler` | boto3, protobuf and cramjam |
| Group `reporter` | boto3, PyJWT and cryptography |
| Group `issuer` | boto3 |
| Group `lifecycle` | boto3 and prometheus-client |
| Group `admin` | boto3 |
| Group `cli` | keyring |
| CDK CLI (`aws-cdk`) | Exact version and integrity hash in `package-lock.json`; installed by `npm ci --ignore-scripts` into `node_modules/`, never globally |
| Node.js 24, the version `.node-version` names | Installed by the contributor, usually with a version manager that reads `.node-version` (mise with idiomatic version files on, fnm, nodenv, `actions/setup-node` through `node-version-file`). The CDK CLI and the synthesis tests run on it; nothing in this repository installs or enforces it |
| pytest for the engine tests | `uv run --no-project --with pytest==8.3.5 --with pytest-xdist==3.8.0` fetches them, and Python 3.12 if needed, into uv's per-user cache |
| Git hooks | Checked in under `.githooks/`; `git-hooks.sh` points this clone's `.git/config` at them |
| GitHub ruleset | `pr-only-main.json`; applied to the `origin` repository only when an admin runs `apply-ruleset.sh` |
| The owner's walkthrough | `owner-setup.sh` installs nothing. While it runs, uv installs the project's own dependencies, at the versions in `uv.lock`, into a temporary folder outside the working tree, which the script removes. Its values step writes one file, for the owner alone: the scanner's list of local values, which is the file `HYGIENE_LOCAL_VALUES` names or else `research-harness-inference/hygiene-values` under the owner's configuration folder (`XDG_CONFIG_HOME`, by default `.config` in the home directory). It refuses a list inside the working tree |
| Hygiene scanner | Checked in under `tools/hygiene/` (standard-library Python); installs nothing |
| Implement loop | Checked in under `tools/implementation-loop/` (standard-library Python); where its run state and worktrees live is described in `docs/factory/operating.md` |
| Skills | Checked in under `.claude/skills/` and `.agents/skills/`; no global skill installation, no MCP server |
| CI tools | `astral-sh/setup-uv` installs the pinned uv on the runner; gitleaks and actionlint are downloaded at a pinned version and verified against a pinned SHA-256 (`.github/workflows/ci.yml`) |

When adding a repository-wide tool, put its installer and manifest here or at the root, pin it,
add it to this inventory, and verify the setup on a clean machine.
