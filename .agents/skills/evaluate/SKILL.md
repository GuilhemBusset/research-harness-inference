---
name: evaluate
description: Submit an agent-session evaluation (a screen or a qualification of an already-deployed pool) to the operator's running driver, and show its experiment issue and status; plan or compare saved bundles offline. Invoke explicitly with /evaluate <command> (Claude Code) or $evaluate <command> (Codex). Do not invoke implicitly.
compatibility: Requires git, uv, and the operator's driver (scripts/evaluate.sh driver) already running for this user on this host
disable-model-invocation: true
allowed-tools: Bash, Read
---

# evaluate — submit an evaluation run to the operator's driver

You are a **client** of the operator's driver. You hold no AWS credentials and no GitHub token, and
you never look for any. The operator started `scripts/evaluate.sh driver` in a terminal of their own,
with their scoped AWS session and the experiment issue writer's token. The driver runs each request
you submit through the one controller (`tools/evaluate/controller.py`). You only send typed
requests to its owner-only socket and report what it answers. The procedure, its prerequisites
and its recovery steps are in `docs/runbooks/agent-session-evaluation.md`. The design is
`docs/design/agent-session-evaluation.md`, "How the operator runs it".

This skill body is checked in byte-identically at `.agents/skills/evaluate/SKILL.md` (Codex) and
`.claude/skills/evaluate/SKILL.md` (Claude Code), and `scripts/check.sh skills` enforces the match.
Implicit invocation is disabled in both harnesses: a run books GPU hosts and writes a public issue.

Run every command from the repository's root, exactly as shown, and nothing else that reaches AWS
or GitHub. Never run `scripts/evaluate.sh driver`, `run`, `publish` or `compare --issue` yourself:
those need the driver's credentials. If the operator asks for one of them, give them the command
from the runbook to run in their own terminal.

## The experiment's repository

The driver takes the repository from the request, not from its own environment, so every `submit`
carries it. Use, in this order:

1. the `--repository owner/name` the operator gave, if any;
2. `RHI_EVALUATE_REPOSITORY`, if it is set in your shell;
3. this checkout's `origin`:

```sh
git remote get-url origin | sed -E 's#\.git$##; s#^.*[:/]([^/]+/[^/]+)$#\1#'
```

Pass it as `--repository "$repository"`, which is a repository name and never a credential.

Admission and the issue's creation are two steps. Admission only reads. It answers
`refused: not-admitted`, with no run ID, when the driver's token cannot read the repository or the
repository has no issues. It also refuses a selected issue that is missing or records another
workload, or hosts or a `--for` the pool cannot hold. Nothing is created then. The issue is created
after admission, by the admitted run, before any booking. The `issue` field shows a URL only once
GitHub has confirmed the issue. A run that ends without that confirmation stops before booking:
`submit` has already returned a run ID, and `inspect --submission <submission-id>` then shows no
`issue`, `running: false` and `exit: 2`. These fields do not say why, and you never guess a cause:

- `cancel_requested: true`: the run was cancelled before its issue was confirmed.
- `publication: pending`: a write awaits GitHub's confirmation. GitHub may have created the issue
  and lost its answer, or refused the write: the driver cannot tell them apart, and neither can
  you. `scripts/evaluate.sh publish --resume <run-id>` delivers it, finds an issue GitHub did
  create by the run's marker, and never opens a second one.
- with neither, nothing awaits delivery and no issue was confirmed. The run's own record is
  `scripts/evaluate.sh status <run-id>`, in the operator's terminal.

## The commands

The argument is one of these forms. A run's fields are those of `scripts/evaluate.sh run`:
`--profile`, `--pool`, `--students`, `--instances`, `--for` or `--session existing`, `--issue new`
or `--issue <number>`, and optionally `--suite`, `--question`, `--target`, `--input-seed`,
`--schedule-seed` and `--rate`.

- `run <fields>` submits the run:

  ```sh
  scripts/evaluate.sh submit --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new --repository "$repository"
  ```

  It prints `submission: <id>` before it sends anything. Keep that ID. If the answer is lost
  (`unanswered`), run the same command again with `--submission <submission-id>`, which returns the
  same run and starts nothing new. Do not submit again without `--submission`.
- `status <submission-id>` shows the run:

  ```sh
  scripts/evaluate.sh inspect --submission <submission-id>
  ```

- `cancel <submission-id>` asks the driver to stop the run. It stops admissions and releases the
  booking the run made:

  ```sh
  scripts/evaluate.sh cancel --socket --submission <submission-id>
  ```

- `plan <fields>`, `report <bundle>` and `compare <reference-bundle> <candidate-bundle>` are
  offline. Run them as `scripts/evaluate.sh plan …`, `report …` and `compare …`, and show their
  output:

  ```sh
  scripts/evaluate.sh plan --profile qualify --students 8 --instances 1 --for 6h
  scripts/evaluate.sh compare .cache/eval/runs/<reference-run-id>/bundle .cache/eval/runs/<candidate-run-id>/bundle
  ```

## After a submission

1. Show what the driver answered, one field a line: `submission`, `run_id`, `step`, `issue`,
   `ending`, `exit`, `publication`, `cancel_requested`, `running`.
2. Until `issue:` appears or `running` is `false`, run `inspect --submission <submission-id>` again
   about every 30 seconds, for at most 10 minutes. The issue is created and confirmed before any
   host is booked, and the preflight may first build the trial image. If `running` is `false` with
   no `issue`, the run ended before an experiment issue was confirmed, and no host was booked.
   Report the fields of step 1 as the driver gave them, `cancel_requested` included, and say no
   more about the cause than they do (see "The experiment's repository"). Then:
   - if `publication` is `pending`, a write awaits confirmation. Tell the operator to run
     `scripts/evaluate.sh publish --resume <run-id>` in their own terminal. It needs no GPU, finds
     the issue by the run's marker if GitHub created it (or creates it if GitHub never received
     the request), and never opens a second issue. After that, `inspect` shows the issue. If it
     still reports the publication pending, the operator checks the driver token's access to the
     repository's issues and runs it again;
   - otherwise nothing awaits delivery. If `cancel_requested` is `true`, the run was cancelled;
     if not, point the operator to `scripts/evaluate.sh status <run-id>` in their terminal.
3. Report to the operator:
   - the experiment issue's URL. It is where the run's terminal comment will be;
   - the run ID and the submission ID;
   - the step reached, and the publication;
   - how to follow the run: `/evaluate status <submission-id>` (or `$evaluate`), and
     `scripts/evaluate.sh status <run-id>` from their terminal.

   A run takes hours. You may end the chat at this point: the run goes on in the driver.
4. Once `exit` is shown, report its meaning. `0` means the profile completed and met its floors.
   `1` is a measured miss. `2` means invalid, incomplete or inconclusive evidence, a failure or a
   refusal. If `publication` is `pending`, the operator delivers it with
   `scripts/evaluate.sh publish --resume <run-id>`, which needs no GPU.

## Hard rules

- Never pass, print or ask for a token, a key or an AWS credential. A request that carries one is
  refused, and nothing in it reaches a service.
- Never edit the experiment issue, its comments or anything under `.cache/eval/`. They are the
  driver's.
- A refusal (`refused: <reason>`) is the answer: report it as it is and stop. Do not retry with
  other fields unless the operator asks for it.
- Never paste a run's private log, its booking times or anything from AWS into the chat, a file or
  GitHub. Report only the fields above.
