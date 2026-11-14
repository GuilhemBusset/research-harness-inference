+++
id = "E0-03"
title = "Mint the factory GitHub App's installation token for the engine's calls"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

Until now the engine calls GitHub with whatever login `gh` has: the owner's own, which reaches every
repository of the owner and cannot be told apart from the owner when a pull request needs review, or
the fine-grained token of `docs/factory/green-light.md`, step 7, which cannot read the check runs of
a private repository. ADR 0001's second amendment gives the factory its own identity: a GitHub App
installed on this repository only, without the workflows and administration permissions, so that
GitHub itself refuses a factory change to a workflow file or a ruleset. The green-light checklist
says this issue replaces its token.

The App's permissions are what the engine's calls use (`implement_loop/github.py`): contents, pull
requests and issues to read and write; checks, commit statuses and actions to read; neither
workflows nor administration. The engine reads commit statuses (the deployment verdict) and never
writes one.

The engine runs outside the sandbox, as the owner, and starts `gh` for the API and git for pushes
and fetches: its workspace module fetches, the ship script pushes and fetches, and the supervisor
pulls `main`. Agents and check commands run in the sandbox, whose environment scrub already drops
every GitHub token variable and clears every git credential helper (`implement_loop/sandbox.py`,
`GITHUB_VARS` and `clean_env`). The App's ID, its installation ID and its private key are on the
factory's host, outside the repository and outside the sandbox. Installation tokens last one hour.

**How git gets a fresh token.** Rather than change each place that pushes or fetches (the workspace
module, the supervisor and the ship script, all on the safety surface), the engine gives git a
credential helper: at the start of a run and of a supervision, `implement_loop/cli.py` adds it to
the git configuration it already passes to every git process of the run through the environment
(`sandbox.engine_git_env`, `GIT_CONFIG_COUNT`), and the helper answers git with a token that has at
least ten minutes left, minting a new one when needed. git asks the helper on every push and fetch,
so a run longer than an hour keeps working. The sandbox's own scrub replaces that configuration for
agents and checks, and the key file is out of their sight anyway.

This issue changes the engine, a protected path (ADR 0001, decision 10); the builder may change the
files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/app_token.py` mints an installation token from the App ID, the installation ID and a private key file, named by the environment variables LOOP_GITHUB_APP_ID, LOOP_GITHUB_APP_INSTALLATION_ID and LOOP_GITHUB_APP_KEY_FILE, with the standard library and the openssl program only.
- [ ] `tools/implementation-loop/implement_loop/github.py` and `tools/implementation-loop/implement_loop/cli.py` give every `gh` process the engine and the supervisor start a token with at least ten minutes left, minting a new one when the one held has less.
- [ ] `tools/implementation-loop/implement_loop/cli.py` sets, for every git process the engine and the supervisor start, a git credential helper for github.com that answers with such a token from `tools/implementation-loop/implement_loop/app_token.py`, so that the pushes and fetches of the workspace module, the supervisor and the ship script get a valid token without a change of their own.
- [ ] The token and the private key are written to no state file, event, log or published text, and reach no agent or check process.
- [ ] Without the three variables the engine keeps the login `gh` already has, and the doctor says which identity it runs as.
- [ ] The doctor fails when the installation holds the workflows or the administration permission, and when it cannot read checks.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_app_token.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_app_token.py` minting a token against a stub of GitHub's API from a key generated in the test, renewing it when less than ten minutes remain, and showing the credential helper answering a git process with the renewed token.
- [ ] `tools/implementation-loop/tests/test_app_token.py::test_the_token_stays_with_gh_and_git` shows the token in the environment of `gh` and git processes only: not in agent or check processes, state files, events or logs.
- [ ] `tools/implementation-loop/tests/test_app_token.py::test_doctor_refuses_a_workflows_permission` fails the doctor for an installation that can write workflows.
- [ ] The owner creates the App with those permissions, installs it on this repository only, and `scripts/implement.sh doctor` reports the App's identity (needs human:owner-decision).

## References

- `docs/adr/0001-software-factory.md`, amendment 2
- `docs/design/system-design.md`, sections 8.4, 8.6
- `docs/factory/green-light.md`
