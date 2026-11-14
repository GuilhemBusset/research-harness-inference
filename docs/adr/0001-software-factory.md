# ADR 0001 — The software factory: the implement loop, vendored, sandboxed and taught about deployments

- **Status:** Proposed. The owner is going through the green light (`docs/factory/green-light.md`): the repository is public, its ruleset is active and the backlog is published as GitHub issues
- **Date:** 2026-11-11
- **Deciders:** Guilhem Busset (repo owner)
- **Context:** the implement loop of [`GuilhemBusset/coding-agent-cursus`](https://github.com/GuilhemBusset/coding-agent-cursus)
  at commit `a0dc6df` (engine 1.0.7) and its ADRs
  [0008](https://github.com/GuilhemBusset/coding-agent-cursus/blob/main/docs/adr/0008-implement-loop.md),
  [0011](https://github.com/GuilhemBusset/coding-agent-cursus/blob/main/docs/adr/0011-autonomous-implement-loop.md) and
  [0012](https://github.com/GuilhemBusset/coding-agent-cursus/blob/main/docs/adr/0012-builder-examiner-loop.md);
  the system design under `docs/design/`

## Context

This repository will hold the AWS infrastructure of an inference endpoint: Python CDK, uv, ty,
deployed by CodePipeline, and meant to become public. The owner wants it built and maintained by
a fully autonomous software factory: a backlog goes in, verified merged changes come out, and
nobody is in the loop during a run.

Four constraints shape the factory more than anything else:

- **A false "done" is expensive here.** A merged change is deployed to a real AWS account by the
  pipeline. Most of what matters (a GPU fleet that scales from zero, a model that loads, a student
  who can log in) cannot be observed on a pull request.
- **Agents never hold AWS credentials.** Real deployment happens only through CodePipeline after
  a merge, so the factory needs another way to learn how a deployment went.
- **The repository goes public.** Account IDs, ARNs and secrets must be stopped by a
  deterministic gate before anything leaves the machine, not by an agent's judgment after it.
- **The host has no Docker.** It is a WSL2 Linux machine with the Claude Code and Codex CLIs,
  `gh`, `uv` and bubblewrap; `sudo` needs a person.

Four proposals were researched in parallel by a cheaper model on 2026-11-11, each ending in a
concrete design, and a Claude Fable 5.2 instance judged them after checking the claims that
carried weight (listed at the end).

**A. Anthropic-native.** Anthropic ships parts, not a backlog-to-merge product: headless
`claude -p` with schema-validated output, deny rules, a sandbox for Bash, workflow scripts, and
several preview features (scheduled cloud tasks, agent teams, Managed Agents). The proposal's own
conclusion was that the trust boundary has to be a deterministic driver outside the agent that
owns git, GitHub and every gate, with a separate examiner session. That is the architecture of
candidate D, to be written again from nothing, with Claude on both sides. Anthropic's own
engineering posts report that Claude evaluators find real issues and then approve the work.

**B. GitHub-native, cross-vendor.** Issues as the backlog, agents as GitHub Actions jobs, the
examiner's verdict as a required check, GitHub auto-merge. GitHub Agentic Workflows reached
general availability in October 2026, but its merge output refuses default and protected
branches. Everything that makes it a factory (dispatcher, publisher, verdict writer, repair loop,
two GitHub Apps) would have to be built, it cannot be exercised without a remote, and it puts
vendor API keys in jobs on a public repository whose prompts carry issue text.

**C. Best-of-breed open source.** No project publishes unattended-to-merge results on
infrastructure code. The closest is a toy application run by Archon workflows, with a
human-authored perimeter and human-promoted releases. Archon is pre-1.0 (v0.14.0) and was assessed
from its README only; Fabro is small and its local executor is unconfirmed; OpenAI's Symphony is a
v0.0.3 preview for Codex and Linear. The useful part of this proposal is ideas: a perimeter the
builder cannot quietly change, invariants written as tests over the synthesized templates, and
Addy Osmani's rule that a loop gets only as much autonomy as can be cheaply verified.

**D. The incumbent.** The owner's implement loop: a deterministic Python engine (7,598 lines,
standard library only, 478 tests) that drives headless Claude Code and Codex. One vendor plans and
builds, the other examines, probes and certifies the exact commit, and the engine owns the issue
graph, git, GitHub, the verifier and the merges. A run never waits for a person: an issue ends
done, delivered with typed obligations, waiting, or explicitly failed. It merged 54 pull requests
in the cursus repository between 2026-10-04 and 2026-11-05. Its record is not clean: ten engine
faults were filed in those five weeks (all since closed, two of them on the last day it ran), the
redesign of ADR 0012 was needed after 19 parks and $433 of Claude spend for 23 merges, and no
pull request has gone through it since 2026-11-05. As it stands it also fails three of this
repository's requirements:

1. It removes only GitHub credentials from the agents' environment. AWS variables and `~/.aws`
   are untouched, and the verifier runs builder-written tests as the owner's user with the same
   gap.
2. Its only external verdict is the `required` check-run on a pull request's head. It looks at
   nothing that happens after a merge.
3. Its safety invariants are names an examiner may cite. Nothing in it detects an account ID or
   a secret by itself.

## Decision

1. **The spine is the implement loop, vendored.** `tools/implementation-loop/` is copied from the
   cursus repository at `a0dc6df` (engine 1.0.7), with its tests, prompts and scripts
   (`scripts/implement.sh`, `scripts/ship.sh`, `scripts/check-loop-guard.sh`), the `implement`
   and `ship` skills, the git hooks and the PR-only ruleset. `tools/implementation-loop/UPSTREAM.md`
   records the source repository, commit, engine version, licence (MIT, same owner) and every
   local patch by file. The states, roles, gates, budgets and supervisor of ADR 0012 apply here
   unchanged unless a point below says otherwise. It was chosen over the three others for one
   reason above all: it is the only candidate that already implements "never waits for a person"
   with recovery paths that were paid for in real failures, and each rival proposal, read
   closely, describes building it again.

2. **Roles: Fable builds, the other vendor examines.** `explore` stays on a cheap Claude model;
   `plan` and `build` run on `claude-fable-5-2` (`loop.toml`, operator `claude`). Triage,
   examination, request decisions and arbitration stay with Codex on the owner's default Codex
   model. The one controlled study found (arXiv 2607.21656, 116 puzzle tasks, reviewer unable to
   run tests) is asymmetric: Claude reviewing Codex drafts raised the pass rate from 71.6% to
   89.7%, Codex reviewing Claude drafts lowered it from 91.4% to 82.8%, and Claude reviewing
   itself changed nothing. That supports putting the stronger solo model on the builder side. It
   does not argue for removing the second vendor here, because the harm it measured came from a
   reviewer that rewrites a draft it cannot test, and ADR 0012 already took that power away: the
   examiner cannot edit the builder's code, a finding blocks only when the engine reproduces it
   on the exact head and it quotes the issue or names a safety invariant, and the examiner's own
   probes are executed, not believed. A Fable examiner would need an engine change, would remove
   the only decorrelation available, and would rest on the evaluator behaviour Anthropic itself
   describes as lenient. The examiner's catch rate on infrastructure code is unmeasured; a
   seeded-defect pilot is a backlog item (see Consequences).

3. **Every agent call and every check command runs in a bubblewrap sandbox with no AWS
   material.** A new module wraps the two places the engine starts processes that run
   agent-controlled code: the agent CLIs (`live.py`) and the verifier's commands and probes
   (`verify.py`). Inside the sandbox the home directory is an empty tmpfs with an allow-list bound
   back in: the toolchain read-only, the running vendor's own login directory, private package
   caches, the issue worktree and the call's private git directory read-write. `~/.aws`, the `gh`
   login, `~/.ssh` and the other vendor's login do not exist there. The environment loses every
   `AWS_*` variable and gets `AWS_EC2_METADATA_DISABLED=true`. The doctor runs a canary inside
   the sandbox and fails if the canary can see AWS or GitHub credentials, and the engine refuses
   to start an agent without the sandbox. This closes, for file reads, the limit the cursus README
   states ("agents run as your user"), and it also contains `git -C /elsewhere`, since the rest
   of the filesystem is read-only. No Docker and no second OS user are needed.

4. **Public-repository hygiene is a deterministic gate that runs before anything is pushed.**
   `tools/hygiene/` is a standard-library scanner for account IDs, ARNs carrying an account,
   access keys and common tokens, private keys, files that must never be tracked
   (`cdk.context.json`, `cdk.out/`, `.env`), endpoint hostnames that identify an account, e-mail
   addresses outside an allow-list and local home paths. Its allow-list holds documented
   placeholders only and is itself protected. It runs in the hook, in the engine and in CI. The
   full gate list for this repository, in the order a change meets it:

   | Gate | Where it runs | What it stops |
   | --- | --- | --- |
   | Hygiene scan of the staged change | `.githooks/pre-commit` | A person or an interactive agent committing a leak |
   | Hygiene scan of the exact head, as a standing check of the verifier | Engine, before examine and before any push | A loop branch carrying a leak ever reaching GitHub |
   | Issue commands, locked tests and adopted probes; zones; protected paths; tamper and skip accounting | Engine verifier, in the sandbox | A false "done" by the builder |
   | Examination, probes and certificate of the exact head | Examiner, in the sandbox | What tests do not express |
   | `ruff`, `ty`, unit tests, CDK assertion and policy tests, cdk-nag, `cdk synth` without credentials, engine tests, hygiene scan, loop guard, skill-pair identity; aggregated as `required` | GitHub Actions, on every pull request, with no AWS credentials | A head that is not green on a clean machine |
   | PR-only `main`, `required` green on the merged head, no force-push, no deletion, no bypass | GitHub ruleset | Anything landing around the gates |
   | Deployment and post-deployment smoke result as commit statuses on the merge commit | CodePipeline, read by the engine (point 5) | "Merged" being mistaken for "works in AWS" |

   The scan is a verifier check, not a `pre-push` hook or a step of `ship.sh`, because a failing
   push is an engine fault in the loop, while a failing verification goes back to the builder
   with its output.

5. **The engine learns the deployment outcome from commit statuses.** The pipeline (built from
   the backlog, in AWS) posts a commit status on each merge commit: `deploy/<stage>` and, once
   smoke tests exist, `verify/<stage>`. It holds a token that can only write statuses on this
   repository, kept in Secrets Manager, never on the factory's host. Its descriptions and any
   failure summary it posts as a commit comment are sanitized with the hygiene patterns before
   they leave AWS. The engine gains `deploy_status(sha, context)` and a `deploying` phase between
   `merged` and the evidence step: with contexts configured in `loop.toml`, a merge is accepted
   only when they report success; a failure takes the existing broken-`main` path (one
   fix-forward with the failure summary, then a revert pull request); no verdict within the
   timeout makes the issue `waiting`, which the supervisor resumes. The landing claim is held
   through the wait, so one merge maps to one deployment and one verdict; the pipeline therefore
   runs its executions queued, not superseded. The contexts list is empty until the pipeline
   exists, and then nothing waits.

6. **Work only a real AWS account can prove is an obligation, never a pass.** `loop.toml [host]
   lacks` names what the host cannot provide: `aws:account`, `aws:deployed-environment`,
   `gpu:capacity`, `aws:console`. An item that needs one is classified `owner`; the agents' part
   is still built and proven, the issue ends `delivered`, and the obligation is listed with the
   exact command or observation that would discharge it. As the pipeline gains smoke tests, such
   items move from the owner to a status context of point 5. What stays with the owner for good is
   short: bootstrapping the account, authorizing the GitHub connection, quota requests, and
   decisions.

7. **The backlog is drafted as files and published as issues.** The engine reads GitHub issues
   and does not write them from a design. Until the green light no issue may exist, so the backlog
   lives in `backlog/drafts/`: one Markdown file per issue, in the engine's input contract
   (`## Deliverables` and `## Acceptance criteria` checkboxes, a size label, backticked paths as
   zones), with front matter for its epic and its `blocked_by` edges. `tools/backlog/` lints the
   drafts with the engine's own parser and publishes them: issues, sub-issue links, dependency
   links and labels, idempotently through a marker in each body, and only with an explicit
   `--apply`. A planner agent (Fable) turns the system design into the first drafts.
   After publication GitHub Issues are the source of truth, as in the cursus repository; later
   work enters through the same `backlog` skill, or as the engine's own follow-up issues. A draft
   whose acceptance has no machine-checkable part and no named obligation does not pass the lint.

8. **Spend has a dollar ceiling as well as a call ceiling.** Each Claude call carries
   `--max-budget-usd` from its role's setting, and the engine adds a per-issue dollar ceiling
   over the cost it already records, handled like the call ceiling. Fable is priced well above the
   models the recorded runs used, and a call count alone does not bound that. Codex reports
   tokens, not dollars, so its side stays bounded by calls.

9. **Security invariants become tests where they can.** `tests/policy/` will hold assertions
   over the synthesized templates (no public ingress to the fleet, a hard cap on fleet size, no
   wildcard IAM, scale-to-zero configured, admin surfaces not public). The first epic creates
   them from the system design; from then on they are protected paths and locked like any
   examined test. The safety invariants in `loop.toml` gain the names specific to this repository
   (an account identifier committed, a policy broader than the issue requires, a resource exposed
   without the issue asking, a pipeline or ruleset change that bypasses review), for what tests
   cannot express.

10. **Protected paths and safety surface.** A builder changes these only when the issue names
    them or the examiner approves a typed request: the engine, `tools/hygiene/`, `.github/`,
    `.githooks/`, the gate scripts, the ruleset, `tests/policy/`, and the pipeline definition once
    it exists. A fault-repair run may never change the safety surface: the verifier, the gate
    module, the live adapter, the sandbox module, `config.py`, `loop.toml`, the supervisor, the
    hygiene scanner and its allow-list, `scripts/check.sh`, `.github/`, `.githooks/` and the
    ruleset.

11. **Inert until the green light.** The scaffolding is files in the working tree. No commit, no
    push, no ruleset, no issue, no activated hook. The owner's GitHub repository already exists,
    private and empty, and is configured as `origin`, so a missing remote is not what keeps the
    factory off. Three explicit guards are: the engine's doctor fails, and `run` and `supervise`
    refuse to start, while `origin` has no `main`; the backlog publisher refuses `--apply` against
    a repository with no commits and does nothing at all without `--apply`; the workflows cannot
    run until something is pushed. `docs/factory/green-light.md` is the ordered checklist that
    turns it on: scan the tree, first commit, push to the private repository, ruleset, hooks, a
    token for the factory scoped to this repository, publish the backlog, doctor with a smoke
    call, first epic, and only then public visibility after a scan of the whole history.

## Consequences

- The factory is a second product in this repository: about 7,600 lines of engine that produced
  a fault roughly every few days while it was in use, now with local patches in its most
  sensitive files (`live.py`, `verify.py`, `engine.py`). `UPSTREAM.md` keeps the divergence
  visible, the patches are kept in separate modules where possible, and the engine's own tests run
  in `required`. Fixes worth sharing go back to the cursus copy by hand.
- A smooth small issue cost 9 to 21 agent calls and $2 to $6 of Claude in the recorded runs after
  ADR 0012. Fable will cost more per call. The first epic measures it from `agents.jsonl`, and
  the ceilings are set from that measurement, not from an estimate.
- Two paid vendors must both be logged in. A Codex outage makes issues wait; there is no
  single-vendor mode.
- Launching stays an act of the owner (one confirmation per supervision that the runs merge their
  own pull requests). Runs are bounded at 10 engine runs and 24 hours, on the owner's machine.
- What the sandbox does not protect, stated plainly:
  - Network egress is open. An agent can send out whatever it can read: the repository, the issue
    text, and the login of the vendor it is running as, which must be readable for the CLI to
    work.
  - It is a namespace sandbox on a shared kernel, not a virtual machine.
  - The engine and the supervisor run outside it, as the owner, with the `gh` token. They execute
    only their own code, git and `gh`, but git hooks from `.githooks/` run in that context, which
    is why hooks are protected paths. Until the factory has its own repository-scoped token, that
    token reaches every repository of the owner.
  - A merged change is deployed by a pipeline with broad rights in the account. The sandbox says
    nothing about that. The limits there are the policy tests, the protected pipeline path, and
    whatever the system design puts on the AWS side (a permission boundary on the deployment
    role, budgets, a fleet cap).
- Synthesis, assertions and cdk-nag prove a template, not behaviour. Until the pipeline reports
  smoke results, an issue about runtime behaviour ends `delivered`, not `done`, and a
  wrong-but-green change can reach `main`. It is visible in the pull request's Decisions section
  and revertible.
- Follow-ups this decision creates, to be filed in the factory epic: measure the examiner on
  seeded defects in CDK changes before trusting its certificates on IAM and networking; move
  AWS-only items from `owner` to status contexts as smoke tests appear; redact published
  evidence with the hygiene patterns; revisit a scheduler that picks up ready issues without a
  launch.

## Alternatives considered

1. **Write a thin Claude-only driver (design A).** Rejected: it is the incumbent's architecture
   without its recovery paths or its record, and with a same-vendor examiner. Kept from it: the
   dollar cap per call, and the facts about what Claude Code's own guardrails do not guarantee.
2. **Run the factory on GitHub Actions (design B).** Rejected: nothing to reuse, untestable
   before a remote exists, and vendor keys next to issue text on a public repository. Kept from
   it: pull-request checks that hold no AWS credentials, and `CLAUDE.md` as a one-line import of
   `AGENTS.md`.
3. **Adopt Archon or Fabro as the engine (design C).** Rejected: pre-1.0 dependencies read only
   from their READMEs, with the same isolation gap and every workflow still to write. Kept from
   it: invariants as protected tests, and not dispatching work that has no checkable end.
4. **Fable on both sides.** Rejected for now (decision 2). To be reopened if the seeded-defect
   pilot shows the Codex examiner adds nothing on infrastructure code.
5. **A second OS user, or a separate WSL distribution, as the boundary.** Not needed for the
   credential rule once the sandbox exists, and it requires `sudo`. It remains the stronger
   option if the owner wants the engine itself away from their own `gh` login.
6. **A hidden holdout the builder cannot read.** Rejected: the builder has to see a failing
   invariant to fix its change, and locking plus protected paths already stop it from weakening
   one.
7. **Consume the engine as a shared package or a subtree.** Rejected for now: the engine's safety
   surface and its supervisor assume it lives in the repository it governs, and local patches
   would conflict on every subtree pull. A shared package is the right end state if a third
   repository adopts the loop.
8. **Preview platforms as the spine** (Claude scheduled cloud tasks, Managed Agents, agent
   teams, GitHub's partner agents). Rejected: preview status, single-account identities, and, for
   Managed Agents on AWS, an IAM role for the agent.

## Amendments (2026-11-11)

Writing the system design raised points that belong to the factory, not to the AWS design. They
are recorded here as proposed and are part of what the owner approves.

1. **A merge to `main` is deployment rights in the AWS account.** The pipeline deploys every
   merge and updates its own definition first. Decision 10's protected paths and decision 9's
   policy tests are therefore the factory's share of the account's trust boundary, together with
   the bounded deployment role the system design specifies. The pipeline stack and the bootstrap
   policy join the protected paths from the first issue that creates them.
2. **The factory gets its own GitHub identity.** A GitHub App installed on this repository only,
   with contents, pull requests, issues and commit statuses, and without the workflows and
   administration permissions. GitHub itself then refuses a factory change to a workflow file or
   a ruleset, whatever an agent was told by an issue, a comment or a dependency. It also makes
   review possible: an author cannot approve their own pull request, so work pushed with the
   owner's token could never be reviewed by the owner. This replaces the repository-scoped token
   of the green-light checklist; the engine needs a small helper that mints the App's
   installation token for its `gh` calls, which is a factory issue.
3. **Whether the owner approves changes to the safety surface is the owner's decision.** The
   recommendation is yes: a `CODEOWNERS` entry for the protected paths, and nothing else, so
   ordinary backlog work stays fully autonomous and only a change to the gates, the workflows or
   the pipeline's own definition waits for a person. The engine already has the outcome for it:
   such an issue ends `delivered` with an owner obligation. Choosing no keeps the factory fully
   autonomous and makes the examiner and the policy tests the only checks on those paths.

4. **The deployment report is commit statuses only.** Decision 5 spoke of a failure summary
   posted as a commit comment and sanitized with the hygiene patterns. A deny-list cannot be
   trusted to sanitize text that comes from AWS. The reporter therefore writes commit
   statuses and nothing else, each assembled from allow-listed parts (system design, "The trust
   boundary"), and its credential can do nothing more.
5. **Checks that need a container run in CI only.** The factory's host has no container runtime.
   Tests that start a container live under `tests/container/`, outside the local gate, and run in
   a CI job that is part of `required`. `loop.toml [host] lacks` names `host:container-runtime`,
   so an issue can state the dependency; letting the engine discharge such an obligation from
   that job's result is a factory issue.

6. **The agents' configuration directories are protected paths and part of the safety
   surface.** `.claude/`, `.agents/` and `.codex/` hold skills, settings and hooks. A hook or a
   setting that a builder adds there would run later in the owner's own session, outside the
   sandbox and with the owner's credentials. Decision 10's lists gain these three prefixes.
7. **A leak in a branch's history ends the issue.** The verifier scans the tree before a round
   is committed, and the branch's own commits before every push. A finding in the history cannot
   be fixed by a later commit, so the branch is abandoned unpushed, the issue ends `failed` with
   the reason `history`, and a retry starts a new branch. The engine's commits carry the
   repository's git identity, which the doctor checks against the scanner's allow-list: it must
   be a no-reply address before the first run.

## Verified and taken on trust

Checked directly on 2026-11-11 by the deciding instance:

- In the cursus engine at `a0dc6df`: version 1.0.7; 7,598 lines and 478 test functions;
  `live.py` and `verify.py` remove only GitHub variables and `SSH_AUTH_SOCK`; check commands run
  through `bash -c` as the invoking user; the examiner is always the other vendor; `claude_model`
  in `loop.toml` becomes `--model`; the only external verdict read is the `required` check-run of
  a commit; a `ship.sh` failure without a conflict is raised as an engine error.
- On GitHub: ten `loop:engine-bug` issues from 2026-10-11 to 2026-11-05, all closed; no
  loop-authored pull request merged after 2026-11-05; the `pr-only-main` ruleset is active on
  the cursus repository, which a personal account owns, so the landing mechanics do not need an
  organization.
- On the host: `claude` 2.1.322 has `--max-budget-usd`, `--effort`, `--json-schema`, `--settings`
  and `--setting-sources`; `codex exec` 0.167.0 has `--output-schema`, `-s` and no `--full-auto`;
  bubblewrap works without privileges; a headless Claude call with `--model claude-fable-5-2`
  answers from inside a bubblewrap sandbox that hides `~/.aws`, the `gh` login and `~/.ssh`; a
  headless Codex call in its read-only mode runs in the same sandbox and sees an empty `~/.aws`;
  the hidden directories cannot be unmounted from inside, including from a nested user namespace.
  The sandbox tested was the simpler masking form, not the allow-list of decision 3, which the
  scaffolding must prove with the doctor's canary.
- The abstract of arXiv 2607.21656 and its four pass-rate pairs; the release state of Archon
  (v0.14.0), Fabro and Symphony (v0.0.3).

Taken on trust from the research reports: list prices and billing rules of both vendors; the
status of Anthropic's and GitHub's preview features; that GitHub Agentic Workflows cannot merge
into a default branch; the count of 54 loop-authored pull requests and the per-issue cost and call
figures read from the cursus run records; the full inventory of cursus-specific couplings in the engine (spot-checked in `config.py` and the
issue parser only); and the characterization of the open-source frameworks, none of which was
installed or run.
