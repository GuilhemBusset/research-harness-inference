# Green light: turning the factory on

The factory is scaffolded as files in the working tree and is inert: no commit, nothing pushed,
no ruleset, no issue, no active hook ([ADR 0001](../adr/0001-software-factory.md), decision 11).
This is the ordered checklist that turns it on. **The owner runs it, by hand, in this order. No
agent runs any step of it.**

Each step ends with what must be true before the next one. The local commands were run while the
factory was scaffolded. The GitHub-side commands could not be, because nothing may be pushed or
changed on GitHub before this checklist: where GitHub's behaviour depends on the account's plan
or on a token's kind, the step says how to check instead of assuming.

Everything pushed in step 3 is private until step 12. Treat it as public anyway: step 12 cannot
take anything back.

**One dependency to decide before starting.** On a free personal plan, GitHub enforces no ruleset
on a private repository and offers it no secret scanning. Between step 3 and step 12 the
repository is private, so unless the plan is upgraded, the PR-only rule on `main` rests on the
git hooks and on the implement loop's own gates alone, and the hygiene scanner and gitleaks are
the only scanners. Step 4 has a probe that shows which case this account is in. Either upgrade
the plan, or accept that state knowingly until the repository is public.

## Before you start

- The system design under `docs/design/` is reviewed, and the backlog drafts under
  `backlog/drafts/` are written from it and pass `scripts/backlog.sh lint`.
- On this machine: `git`, `uv`, `jq`, `gh` logged in as the owner (`gh auth status`), the Claude
  Code and Codex CLIs both logged in, and bubblewrap (`bwrap --version`).
- The tree is still inert:

  ```sh
  git log -1                        # fatal: ... does not have any commits yet
  git config --get core.hooksPath   # prints nothing
  git remote -v                     # nothing, or `origin` (see step 3)
  ```

## 1. Check the whole tree, then read the hygiene report

No pattern in a public repository can name your real domain, your address or a student: only you
can. Write those values, one per line, in a file outside the repository. From then on every scan
on this machine, including the hooks, also looks for them
([`tools/hygiene/README.md`](../../tools/hygiene/README.md), "Values only the owner knows").

```sh
mkdir -p ~/.config/research-harness-inference
$EDITOR ~/.config/research-harness-inference/hygiene-values    # the domain, the admin address, ...
chmod 600 ~/.config/research-harness-inference/hygiene-values
```

Then:

```sh
scripts/check.sh all
python3 tools/hygiene/scan.py --show-allowed
git status --short --ignored
```

- `scripts/check.sh all` ends with `all steps passed`.
- The scanner reports no finding, and its summary line ends with `local values checked`. Read
  every `allowed inline:` line and every entry of `[exclude]` in `tools/hygiene/hygiene.toml`:
  each one is a value somebody decided was public.
- `git status` lists what will be committed (`??`) and what stays out (`!!`). The scanner cannot
  recognise a revealing name it was never told about: read the list with that in mind.

**Done when** all three are clean and you have read the list.

## 2. First commit on `main`

A commit publishes its author's name and e-mail address for good. Use your GitHub no-reply
address, in this repository only:

```sh
gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'   # prints the address
git config user.name "<the name to publish>"
git config user.email "<the address printed above>"
```

On GitHub, under Settings, Emails, tick **Keep my email addresses private** and **Block command
line pushes that expose my email**. Merges made on GitHub (the implement loop merges through the
API) then carry the no-reply address too.

```sh
git add -A
python3 tools/hygiene/scan.py --staged
git commit -m "Scaffold the software factory (ADR 0001)"
python3 tools/hygiene/scan.py --history
```

The hooks are not active yet, so the commit on `main` goes through (the `pre-commit` hook would
allow the first commit of a repository anyway). `--history` reads every committed file, the
commit message and the author and committer addresses.

**Done when** `git log --format='%an <%ae>' -1` shows the no-reply address and both scans exit 0.

## 3. The GitHub repository, private, and the first push of `main`

When the factory was scaffolded, this clone already had an `origin`: an empty, private GitHub
repository created by the owner. Check that this is still what it is before anything is pushed
to it:

```sh
git remote get-url origin        # https://github.com/<owner>/<repository>.git
gh repo view --json nameWithOwner,visibility,isEmpty --jq '[.nameWithOwner, .visibility, .isEmpty] | @tsv'
```

Expected: your repository, `PRIVATE`, `true`. In a copy of this tree that has no `origin`, create
the repository instead, under the name of this directory:

```sh
name="$(basename "$PWD")"
gh repo create "$name" --private --source . --remote origin
git remote set-url origin "https://github.com/$(gh api user --jq .login)/$name.git"
```

Then push:

```sh
git push -u origin main
repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
```

The remote is HTTPS on purpose: from step 7 on, git must push with the factory's token, which is
scoped to this repository. An SSH key reaches every repository of its owner. The later steps use
`$repo`; in a new shell, set it again with the same `gh repo view` command.

Keep workflow tokens read-only by default, whatever a workflow file says:

```sh
gh api -X PUT "repos/$repo/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

The push starts the first CI run this repository has ever had:

```sh
gh run watch "$(gh run list --workflow ci.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Expect to fix something here: the workflows could not be run before this step (the job most
likely to need attention is `engine-tests`, which prepares bubblewrap on the runner). Until step
4, `main` still accepts direct pushes, so fix forward on `main` and push again.

**Done when** the CI run on `main` is green, job `required` included.

## 4. Apply the ruleset; turn on secret scanning

```sh
bash setup/apply-ruleset.sh
gh api "repos/$repo/rulesets" --jq '.[] | [.name, .enforcement] | @tsv'   # pr-only-main  active
```

Check that the rule binds, by pushing a commit that must be refused. This creates an empty commit
object without touching any branch, and tries to put it on `main`:

```sh
probe="$(git commit-tree 'HEAD^{tree}' -p HEAD -m 'probe: the ruleset must refuse this')"
git push origin "$probe:refs/heads/main"
```

Expected: `remote rejected`, with `GH013: Repository rule violations`. If the push is accepted,
the ruleset exists but GitHub does not enforce it: on a personal account without a paid plan,
rulesets are enforced on public repositories only. Undo the probe with
`git push --force origin main` (possible precisely because nothing is enforced), then decide:
upgrade the account, or accept that until step 12 only the hooks, the engine's own gates and the
`pr-only.yml` audit stand between a mistake and `main`. Do not continue without deciding.

```sh
gh repo edit "$repo" --enable-secret-scanning --enable-secret-scanning-push-protection
gh api "repos/$repo" --jq '.security_and_analysis
  | {secret_scanning: .secret_scanning.status, push_protection: .secret_scanning_push_protection.status}'
```

On a personal account GitHub offers secret scanning and push protection on public repositories
only. If the command is refused, or the statuses stay `disabled`, the hygiene scanner and
gitleaks in CI are the only scanners while the repository is private; step 12 turns these on
again once it is public.

**Done when** the probe push is refused (or you have decided otherwise, knowingly).

## 5. Activate the hooks

```sh
bash setup/git-hooks.sh
git config --get core.hooksPath          # .githooks
git commit --allow-empty -m "probe"      # refused: direct commits to 'main' are not allowed
```

**Done when** the probe commit is refused.

## 6. A first pull request, end to end

One small, real change proves the whole path: hooks, `ship`, CI, the ruleset, the merge and the
audit. The change is ready-made: `README.md` ("Status") and `AGENTS.md` ("Status: scaffolding,
inert") still say that nothing is committed. Make them say the repository is live.

```sh
git switch -c docs/factory-is-live
# edit README.md and AGENTS.md
git commit -am "Docs: the repository is live"
scripts/ship.sh
gh pr create --base main --fill
gh pr checks --watch
gh pr merge --merge --delete-branch
git switch main && git pull --ff-only
gh run list --workflow pr-only.yml --limit 1
```

**Done when** `required` was green on the pull request, the merge went through, and the PR-only
audit on `main` is green.

## 7. A token for the factory, limited to this repository

Until this step the engine would act with your own `gh` login, which reaches every repository you
own. The system design ("The trust boundary") gives the factory an identity of its own, without
the workflows and administration permissions, so that GitHub itself refuses an agent's change to
a workflow or a ruleset. The form of that identity `gh` can use today is a fine-grained personal
access token. The design's final form is a GitHub App, an issue of the factory epic: once that
issue lands, the App replaces this token, and the token is revoked.

Create the token (GitHub, Settings, Developer settings, Fine-grained tokens): resource owner
yourself, **Only select repositories** with this repository alone, an expiry of 90 days or less,
and these repository permissions:

| Permission | Access | The engine uses it to |
| --- | --- | --- |
| Contents | Read and write | Push its branches, merge its pull requests |
| Pull requests | Read and write | Open pull requests, post evidence |
| Issues | Read and write | Read the issue graph, tick checkboxes, file follow-ups; publish the backlog (step 8) |
| Commit statuses | Read-only | Read `deploy/<stage>` and `verify/<stage>` on a merge commit |
| Actions | Read-only | Read the log of a failed check |
| Metadata | Read-only | Granted automatically |

Leave everything else at "No access", **Workflows** and **Administration** in particular: without
them GitHub refuses any push that changes `.github/workflows/` and any change to the ruleset, so
the factory cannot change its own gates. An issue that names a workflow file then has to be
landed by you. The credential that *writes* commit statuses is a different one and belongs to the
pipeline (step 11).

Store it in a `gh` configuration of its own, and run the factory from shells that point at it:

```sh
export GH_CONFIG_DIR="$HOME/.config/gh-factory"
gh auth login --hostname github.com --git-protocol https --with-token   # paste the token, Enter, Ctrl-D
gh auth setup-git
gh auth status
```

Then check what this shell can do:

```sh
gh api "repos/$repo" --jq .permissions                                   # push: true, admin: false
gh api "repos/$repo/commits/$(git rev-parse origin/main)/check-runs?check_name=required" \
  --jq '.check_runs[0].conclusion'                                       # success
```

The second command matters. The engine learns that `required` is green from that endpoint, and
GitHub's list of fine-grained token permissions has none for check runs. If it answers `403`
("Resource not accessible by personal access token"), this token cannot land anything while the
repository is private; GitHub serves check runs of public repositories without a permission. In
that case run the factory under your own `gh` login until step 12 (unset `GH_CONFIG_DIR`),
knowing what ADR 0001 says about that login's reach, and bring the GitHub App forward in the
backlog: an App installed on this repository only can be granted read access to checks.

**Done when** both commands answer as shown, or you have chosen the fallback knowingly.

## 8. Publish the backlog

```sh
scripts/backlog.sh lint
scripts/backlog.sh plan
scripts/backlog.sh publish            # dry run: prints what it would create, sends nothing
scripts/backlog.sh publish --apply
scripts/backlog.sh publish --apply    # creates nothing the second time
gh issue list --limit 200
```

From here on the GitHub issues are the source of truth, and the drafts are history.

**Done when** the second `--apply` creates nothing and the epics show their sub-issues.

## 9. Doctor, with a smoke call

```sh
scripts/implement.sh doctor --smoke
```

It makes one tiny real call to each vendor (cents). Every line must be `PASS` or `WARN`. The
`sandbox` line must be `PASS`: its canary ran inside the sandbox and could see neither `~/.aws`,
nor the `gh` login, nor `~/.ssh`, nor any `AWS_*` variable.

**Done when** there is no `FAIL`.

## 10. The first epic

```sh
scripts/implement.sh plan <epic>
scripts/implement.sh supervise <epic> --operator claude --yes --detach
scripts/implement.sh status <epic>
```

or `/implement <epic>` from Claude Code. `--yes` is the one confirmation a supervision needs: the
runs merge their own pull requests. See [`operating.md`](operating.md) for watching, stopping and
reading outcomes.

When it has settled, read what each issue cost (`status`, and `agents.jsonl` in the run
directory) and set the call and dollar ceilings in `tools/implementation-loop/loop.toml` from
those numbers. That file is part of the safety surface: change it yourself, through a pull
request.

**Done when** the first epic has settled and the ceilings come from measurement.

## 11. The AWS side, by hand, once

From a shell no agent runs in, with your own AWS credentials: the steps under "What the owner
does once" in the [system design](../design/system-design.md). In short: request the GPU quotas;
register the domain; create the GitHub Apps (student login, status reporter, the factory's
identity) and store their secrets in Secrets Manager; write the configuration to SSM; turn on
billing alerts; `cdk bootstrap` with the project's execution policy and permissions boundary,
then `cdk deploy` the pipeline stack once; authorize the GitHub connection for this repository
only; enter the roster.

The design is the reference for that list, and the exact commands arrive with the backlog: the
pipeline epic delivers the script that walks through these steps and writes nothing
account-specific to the working tree. They do not exist yet. Two rules hold whatever the
commands turn out to be:

- Nothing they print (the account ID, ARNs, host names) is ever pasted into a file, an issue, a
  pull request or a commit message.
- The status reporter's credential can write commit statuses on this repository and nothing
  else, lives in Secrets Manager, and never exists on the factory's machine.

Once the pipeline reports on merge commits, tell the engine which statuses to wait for. List what
the pipeline posts, then set them in `tools/implementation-loop/loop.toml`, through a pull
request of your own:

```sh
gh api "repos/$repo/commits/$(git rev-parse origin/main)/statuses" --jq '.[].context' | sort -u
```

```toml
[deploy]
contexts = ["deploy/<stage>"]
```

If `loop.toml` has no `[deploy]` table, the engine does not read deployment statuses yet: that is
the deployment-status issue of the factory epic.

**Done when** a merge is accepted only after its `deploy/<stage>` status reports success.

## 12. Make the repository public

Making it public publishes every file version on every branch, every pull request (merged or
not), every issue and comment, every commit's author address, and the log of every workflow run.
Scan all of it first.

```sh
git fetch --prune origin '+refs/heads/*:refs/remotes/origin/*' '+refs/pull/*/head:refs/remotes/pull/*'
python3 tools/hygiene/scan.py --history
git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u      # no-reply addresses only
```

Secrets in the full history, with the gitleaks version CI pins:

```sh
tmp="$(mktemp -d)"
curl -fsSL -o "$tmp/gitleaks.tar.gz" \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
echo "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  $tmp/gitleaks.tar.gz" | sha256sum --check --strict -
tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks
"$tmp/gitleaks" git --no-banner --redact --verbose --log-opts=--all .
```

What is not in git: issue and pull request text, and every kind of comment.

```sh
gh api --paginate "repos/$repo/issues?state=all&per_page=100" \
  --jq '.[] | "#\(.number) \(.title)\n\(.body // "")"' > "$tmp/issues-and-pulls.txt"
gh api --paginate "repos/$repo/issues/comments?per_page=100" --jq '.[].body' > "$tmp/comments.txt"
gh api --paginate "repos/$repo/pulls/comments?per_page=100" --jq '.[].body' > "$tmp/review-comments.txt"
gh api --paginate "repos/$repo/comments?per_page=100" --jq '.[].body' > "$tmp/commit-comments.txt"
python3 tools/hygiene/scan.py --paths "$tmp"/*.txt
rm -rf "$tmp"
```

Workflow logs become readable by everyone. If you would rather not publish the old ones:

```sh
gh api --paginate "repos/$repo/actions/runs?per_page=100" --jq '.workflow_runs[].id' \
  | xargs -I{} gh api -X DELETE "repos/$repo/actions/runs/{}/logs"
```

**If any scan reports a finding, stop.** A value in the history cannot be removed by a new
commit, and the ruleset forbids rewriting `main`. Decide what the value is worth (rotate a
credential in any case) before going further.

Then:

```sh
gh repo edit "$repo" --visibility public --accept-visibility-change-consequences
gh repo edit "$repo" --enable-secret-scanning --enable-secret-scanning-push-protection
gh api -X PUT "repos/$repo/private-vulnerability-reporting"
gh api -X PUT "repos/$repo/actions/permissions/fork-pr-contributor-approval" \
  -f approval_policy=all_external_contributors
```

The last command makes every pull request from someone outside the repository wait for your
approval before its workflows run. Then repeat the probe push of step 4 (it must be refused now,
whatever the plan), and the two checks of step 7 if the factory was running under your own login.

**Done when** the repository is public, the probe push is refused, secret scanning and push
protection report `enabled`, and the next nightly CI run is green.
