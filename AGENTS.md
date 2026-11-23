# Repository conventions

Canonical working conventions for this repository, read by **every** coding agent (Claude Code,
Codex and any other) and by people. `CLAUDE.md` imports this file; do not duplicate conventions
there. Every agent call of the implement loop loads this file, and inside the loop's sandbox it is
the only standing instruction an agent has. Keep it to what holds for every issue: what one
subsystem needs belongs in the design or in the issue.

This repository holds the AWS infrastructure of an inference endpoint: Python CDK, uv and ty,
deployed only by AWS CodePipeline. It is built by a software factory and it is public. The
decisions are in [`docs/design/`](docs/design/README.md) (the system) and
[`docs/adr/0001-software-factory.md`](docs/adr/0001-software-factory.md) (the factory). The code
follows the "Repository layout" section of the system design (section 8.1).

**Status: live.** The repository is public, `main` takes changes only through pull requests
(the ruleset), and the backlog is published as GitHub issues. The implement loop runs when the
owner launches it, after the last steps of
[`docs/factory/green-light.md`](docs/factory/green-light.md) (the factory's own token, the
doctor). No agent launches it on its own initiative, and no agent changes the repository's
settings, its ruleset or its tokens on GitHub.

## Toolchain

- One uv project at the root: `pyproject.toml`, `uv.lock`, Python from `.python-version`, floor
  `requires-python`. Run everything through uv with the lock: `uv run --locked <tool>`. Never
  install with `pip`, and never rely on a global tool, an existing cache or an agent's own
  configuration.
- Every direct dependency is pinned to an exact version (`==`), never a range: `uv add` it (or
  edit `pyproject.toml`) and commit the new `uv.lock` in the same change. Record what a new tool
  installs and where in `setup/README.md`.
- Ruff and ty are configured in `ruff.toml` and `ty.toml`. ty is beta: a bump is its own pull
  request. Ruff's pin is also `required-version` in `ruff.toml`; the pytest pins are also in
  `scripts/check.sh` and in the implement loop. Bump every place together.
- The CDK CLI will be pinned in `package.json` and `package-lock.json` and run from
  `node_modules`, on Node 22 or 24. Never use a global `cdk`.
- Everything here only needs to work on Linux.

## Validating a change

`scripts/check.sh` is the one gate script. A laptop, the implement loop's verifier and CI run the
same code.

```sh
scripts/check.sh hygiene   # the public-repository scanner (python3 only)
scripts/check.sh fast      # hygiene, shell and JSON syntax, the lock, ruff, ty
scripts/check.sh all       # fast, every test, the engine tests, the skill pairs, the backlog lint, the ship test, cdk synth
```

One step is in neither `fast` nor `all`: `scripts/check.sh container-tests` runs the tests under
`tests/container/`, which start a container. It fails when no container runtime answers, and the
agents' host has none, so CI runs it, in its job `container-tests`.

While iterating, run the narrowest check directly:

- one step of the gate: `scripts/check.sh <step>` (`lint`, `types`, `tests`, `synth`, ...);
- one tool's tests: `uv run --locked pytest tools/<tool>/tests`;
- product tests: `uv run --locked pytest tests/<path>`;
- lint and types: `uv run --locked ruff check <paths>`, `uv run --locked ty check <paths>`;
- the engine tests: `scripts/check.sh engine-tests`;
- the backlog drafts: `scripts/backlog.sh lint`.

Fix findings in the code. A Ruff exception is `# noqa: <rule> - <reason>` on its line. A ty
exception is `# ty: ignore[<rule>]` with its reason, only for code that steps outside its
annotations on purpose (a test double) or that ty misreads. Type errors and warnings fail CI like
test failures.

## Public-repository hygiene

Everything written here is public: files, commit messages, issue and pull request text, and
evidence comments. The owner's rule is that nothing identifies the AWS account.

Never write:

- an AWS account ID, or an ARN that carries one;
- an access key, a token, a password, a private key, a student's API key, or any other
  credential;
- a host name or identifier of a deployed resource (API Gateway, CloudFront, load balancer and
  function URL hosts, hosted zone IDs, `vpc-…`, `subnet-…`, `i-…` and similar IDs);
- the real domain, a real e-mail address or phone number, the login app's client ID, the class
  roster, the class schedule, the budget;
- an absolute path into someone's home directory, or the user name of the machine that recorded
  something;
- text that AWS produced (an error message, a resource name): what the system publishes, to
  GitHub or to students, comes from fixed vocabularies (system design, sections 5.3 and 8.4);
- a link to the AWS console, or a build badge;
- `cdk.context.json`, anything under `cdk.out/`, `.env` files, key files, state files.

Write instead: `123456789012` or `111122223333` for an account, `example.com` for a domain and
its addresses, paths relative to the repository.

Real configuration comes from the deployment environment, never from the repository: stacks are
environment-agnostic, the account and region come from the deploying credentials, and values such
as the domain are read from SSM at deploy time. `config.example.toml` holds placeholders only.
There are no context lookups: `cdk synth --lookups=false` must work with no credentials. See
"Keeping the repository public-safe" in the system design (section 10).

`tools/hygiene/scan.py` enforces what a pattern can express, in the git hooks, in the implement
loop's verifier and in CI ([`tools/hygiene/README.md`](tools/hygiene/README.md)). A value that is
public on purpose and still matches a rule is allowed on its line with
`hygiene: allow <rule> (<reason>)`; do not add it to `tools/hygiene/hygiene.toml`. No public
pattern can name the real domain or a revealing resource name. The owner's machine also checks
for the real values, from a file outside the repository that agents never see; everywhere else,
keeping them out is up to whoever writes.

If a real identifier or credential ever reaches a commit, deleting it in a later commit is not a
fix: it stays in the history. Stop, say so, and leave the decision to the owner.

## AWS

- Agents hold no AWS credentials and never look for any. Never run `aws`, `cdk deploy`,
  `cdk bootstrap`, `cdk destroy`, or anything else that needs an account.
- What agents can prove: `cdk synth --lookups=false` with no credentials, assertions over the
  synthesized templates, the invariants in `tests/policy/`, cdk-nag, unit and contract tests.
- Deployment happens only through CodePipeline, after a merge to `main`. Its result comes back as
  commit statuses on the merge commit (`deploy/<stage>`, later `verify/<stage>`), readable with
  `gh`.
- What only a real account can show (a quota, a deployed environment, GPU capacity, the console)
  is never claimed as done. It is named as an obligation of the owner, with the capability it
  needs and the exact command or observation that would discharge it.

## Where tests live

- A tool's tests live with the tool: `tools/<tool>/tests/`.
- Product tests live under `tests/`, in the folder that matches what they check:

  | Folder | What goes there |
  | --- | --- |
  | `tests/unit/<package>/` | Pure code, one folder per package (`common`, `gateway`, `scaler`, `issuer`, `client`, ...) |
  | `tests/synth/` | Assertions over the synthesized templates |
  | `tests/policy/` | The security invariants; protected (below) |
  | `tests/contract/` | The engine contract (system design, section 4.1), run against each engine adapter, the mock first |
  | `tests/replay/`, `tests/captures/` | Replayed harness traffic, and the scrubbed captures it replays: example host, no key, no address, no student |
  | `tests/smoke/` | The pipeline's verification; locally it runs against an in-process stack |
  | `tests/container/` | Tests that can only be made by starting a container. Not in the default test run: CI's job `container-tests` runs them (`scripts/check.sh container-tests`), and an acceptance item that rests on one ends with `(needs host:container-runtime)`. See [`tests/container/README.md`](tests/container/README.md) |

- Security invariants over the synthesized templates live under `tests/policy/` (such as: no
  ingress from the internet to the fleet, a hard cap on fleet size, no wildcard IAM; the system
  design lists them under "The trust boundary", section 8.4). `tests/policy/` is a protected path:
  a change to an invariant needs the issue to say so and the examiner to approve.

A test guards behaviour someone relies on. It does not pin wording or presentation: assert the
properties the issue names, never a whole template as a snapshot, which every later construct
would have to rewrite. A test never needs AWS credentials or the network.

## Branching and merging

In the implement loop the engine owns git and GitHub: builders and examiners never commit, push
or open pull requests. The rest of this section is for people and interactive sessions.

- `main` is **protected**. Every change lands through a pull request: no direct commit, no direct
  push, no force-push.
- Work on branches named `<type>/<short-description>` (`feat/gateway-auth`, `fix/null-deref`,
  `docs/runbook`, `chore/bump-ty`). The implement loop names its own branches (`loop/issue-<n>`).
- To land a change, open a pull request against `main`. The shared `ship` skill automates it
  (bring the branch up to date with `main`, push it, open or update the pull request): `/ship` in
  Claude Code, `$ship` in Codex, `scripts/ship.sh` plus `gh pr create` from a terminal.
- A merge to `main` is, once the pipeline exists, a deployment to a real AWS account.

## The backlog

The backlog is this repository's GitHub issues: one tracking issue per epic, its work as
sub-issues, linked by their dependencies. The issue on GitHub is the source of truth.

New work enters as issues the implement loop can finish, drafted first so that it reaches GitHub
in the shape the engine reads. A draft is a file under [`backlog/drafts/`](backlog/README.md),
one per issue (`## Deliverables` and `## Acceptance criteria` as checkboxes, a size label,
backticked paths as zones). `scripts/backlog.sh lint` checks it with the engine's own parser,
`scripts/backlog.sh plan` shows the order, and `scripts/backlog.sh publish` creates the GitHub
issues (a dry run unless `--apply` is given); a draft joins a published epic, or waits for a
published issue, by its number. Use the shared `backlog` skill to write one (`/backlog` in Claude
Code, `$backlog` in Codex). Drafts are reviewed in a pull request and published from its head
once its CI is green; the pull request is then closed unmerged, so `main` never holds a draft.

Every acceptance item is either checkable by a command or a test, or names the capability it
needs (`(needs aws:deployed-environment)`). An item with neither does not pass the lint.

## Implementing issues with the loop

To implement an epic or an issue end to end, use the shared `implement` skill (`/implement <n>` in
Claude Code, `$implement <n>` in Codex, or `scripts/implement.sh supervise` from a terminal). A
deterministic engine (`tools/implementation-loop/`, vendored; ADR 0001) works each open
sub-issue in a sandbox with no AWS material: Claude Fable plans and builds it, Codex examines and
certifies the exact commit that lands, and an issue closes only with evidence. See
[`docs/factory/operating.md`](docs/factory/operating.md).

While a run is active, nobody, person or agent, edits its worktrees, branches or issues, commits
or merges for it, ticks its checkboxes, or relays questions to it. The operator launches the run
and reports its result.

## Protected paths and the safety surface

A builder changes these only when the issue names them, or the examiner approves a typed
`protected_change` request (ADR 0001, decision 10). The lists that bind are `PROTECTED_*` and
`SAFETY_SURFACE_*` in `tools/implementation-loop/implement_loop/config.py`; this section repeats
them for people.

- the engine, `tools/implementation-loop/`;
- the hygiene scanner and its allow-list, `tools/hygiene/`;
- `.github/` and `.githooks/`;
- the agents' own configuration: `.claude/`, `.agents/` and `.codex/` (a hook or a setting added
  there runs in the owner's session, outside the loop's sandbox);
- the gate scripts: `scripts/check.sh`, `scripts/check-loop-guard.sh`, `scripts/implement.sh`,
  `scripts/ship.sh`;
- the ruleset, `setup/pr-only-main.json`;
- the invariants, `tests/policy/`;
- the pipeline stack, `src/rhi/pipeline/`, and the bootstrap policies, `setup/bootstrap/`: a
  merge to `main` is deployment rights in the account. Neither is on the safety surface.

A fault-repair run of the loop may never change the safety surface, whatever any agent approves:
the verifier and its pytest plugin, the gate module, the live adapter, the sandbox module, the
engine's `config.py`, `loop.toml`, the supervisor, the hygiene scanner and its allow-list,
`scripts/check.sh`, `scripts/check-loop-guard.sh`, `.github/`, `.githooks/`, `.claude/`,
`.agents/`, `.codex/` and the ruleset. A fault whose fix needs one of them stays filed for the
owner.

## Enforcement

The guarantees hold under any agent, or a bare terminal, because they are enforced at layers that
do not depend on one. In the order a change meets them:

- the git hooks (`.githooks/`, activated per clone by `setup/git-hooks.sh`): no commit or push to
  `main` or `master`, and a hygiene scan of the staged change and of every pushed branch;
- the implement loop's verifier, in the sandbox, on the exact head: the hygiene scan, the issue's
  commands, the adopted probes and protected paths; then the examiner's certificate for that head,
  which reviews every change to a test, and which from an issue's fourth examination on only an
  unmet item or a finding that names a safety invariant withholds. When the land step brings a
  certified head up to date with `main` without a conflict, the merged head keeps that
  certificate: it is verified again on its exact head and `required` runs on it before the merge,
  but it is not examined again. A head that resolves a conflict is examined;
- CI (`.github/workflows/ci.yml`), with no AWS credentials and no secrets: `scripts/check.sh` and
  the history scan, ending in one job, `required`;
- the GitHub ruleset (`setup/pr-only-main.json`): a pull request for every change to `main`,
  `required` green on the merged head, no force-push, no deletion, no bypass. This is the hard
  server-side guarantee; `.github/workflows/pr-only.yml` audits `main` for copies that lack it;
- after a merge, the pipeline's commit statuses. "Merged" is not "works in AWS".

If a hook fires, a check fails or a push is rejected, fix the cause. Do **not** bypass with
`--no-verify` or `--force`, and do not weaken a check to make it pass.
