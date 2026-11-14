---
name: backlog
description: Write or revise backlog issue drafts the implement loop can finish (files under backlog/drafts/), lint them and show their order with scripts/backlog.sh. Use when asked to add, draft, split or plan an issue or an epic, or to turn a design into a backlog. Publishing the drafts to GitHub is a separate step, taken only when the owner asks for it.
compatibility: Requires Python 3.12+ and git; publishing also needs an authenticated GitHub CLI (gh)
allowed-tools: Read, Grep, Glob, Bash(scripts/backlog.sh lint*), Bash(scripts/backlog.sh plan*)
---

# backlog — draft issues the implement loop can finish

You are writing work for an automated loop, not for a colleague who can ask what you meant. The
implement loop (`tools/implementation-loop/`, ADR 0001) takes one GitHub issue at a time: a
builder plans and builds every `- [ ]` item of it, an examiner from the other vendor checks the
exact commit, and the engine merges. Nobody answers questions during a run, and **the issue text
is the floor and the ceiling**: what it asks for is built and proven, what it does not ask for is
not built, and an item nobody can prove can never be closed.

So an issue is good when the loop can finish it: small, each item a statement that is true or
false, each acceptance item naming what shows it.

Issues are drafted as files under `backlog/drafts/`, linted, and published later by the owner.
`backlog/README.md` is the reference for the format; the lint enforces all of it. This skill body
is checked in byte-identically at `.agents/skills/backlog/SKILL.md` (Codex) and
`.claude/skills/backlog/SKILL.md` (Claude Code); CI enforces the match.

## 1. Read before you write

- `backlog/README.md`, whole: the layout, the front matter, what the engine reads in a body.
- The part of the design the work comes from (`docs/design/`), and `AGENTS.md`.
- `scripts/backlog.sh plan --zones`: what is already drafted, in which order, on which paths.
- The code the issue will change, if it exists. An issue that names a file that is not there, or
  misses the one that is, costs the loop its first calls.

If the design does not say something the issue needs (a value, a name, a choice between two
options), do not invent it. Leave it out of the draft and report it as an open question, or make
it an item that ends with `(needs human:owner-decision)`.

## 2. Cut the work

- **An epic is one outcome; an issue is one pull request.** After each issue is merged, `main`
  must be green, and what the pipeline deploys from it must still work: the loop merges issues
  one by one.
- **Small.** Prefer `size:S` (one file or one construct with its test; about five items or
  fewer) and `size:M` (a few files; about eight items or fewer). Use `size:L` only for what
  cannot be split. The size sets the issue's ceiling of agent calls and of dollars; an issue
  that reaches it fails. Every item costs calls: ask for nothing the outcome does not need.
- **One kind each:** `kind:infra` (CDK code), `kind:app` (code that runs on the infrastructure),
  `kind:test` (tests and invariants), `kind:docs`, `kind:factory` (the engine, the gates, the
  tooling). Add `loop:infra` to an issue about the factory's own tooling.
- **Few zones.** The paths an issue names in Deliverables are its zones. Two issues are built
  at the same time only if their zones do not overlap, and the files many issues touch
  (`pyproject.toml`, `uv.lock`, `cdk.json`, `README.md`, `AGENTS.md`, `.gitignore`, the
  workflows) are held by one issue at a time. Keep changes to those in few issues.
- **True dependencies only.** `blocked_by` names the issues whose merged result this one needs
  to start. Each edge removes parallel work. Depend on issues, never on an epic.
- **Invariants early.** Where the design states a rule (nothing reachable from the internet, a
  cap on the fleet, no wildcard policy), the issue that writes its test under `tests/policy/`
  comes before, or with, the construct the rule constrains.

## 3. Write the draft

Create `backlog/drafts/<epic>/<nn>-<slug>.md` (and `epic.md` for a new epic):

```markdown
+++
id = "E1-02"
title = "An empty app that synthesizes with no credentials"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-01"]
+++

## Context

Why this exists and what it must fit into. Free text. The builder reads it; the loop does not
prove it.

## Deliverables

- [ ] `app.py` defines one app with one stack, `infra/stack.py`, that holds no resource yet.

## Acceptance criteria

- [ ] `npx cdk synth --lookups=false` exits 0 with every `AWS_*` variable unset.
- [ ] The pipeline's first execution deploys the empty stack (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, section 8.2
```

**Deliverables** say what exists once the issue is merged: files and behaviour. **Acceptance
criteria** say how that is shown. For every item of either:

- One line, one full sentence, one thing that is true or false. The examiner holds the work to
  an item by quoting its words, so write the words you mean: exact paths, exact names, exact
  numbers. Not "should", "properly", "as needed", "etc.".
- Say what must hold, not how to code it, unless the design fixes the how.
- In Deliverables, put paths in backticks and nothing else that looks like one: every such
  token becomes a zone. Name at least one path.
- Under these two headings write only items. Explanations go in Context.
- A value that comes from the deployment environment is named as such, never invented. No
  account ID, ARN, hostname of a deployed endpoint, e-mail address or home path, anywhere.

### An acceptance item names its check, or the capability it needs

Agents hold no AWS credentials and never run `aws` or `cdk deploy`. What they can prove:

| What | How an item names it |
| --- | --- |
| The app synthesizes with no account | `` `npx cdk synth --lookups=false` exits 0 `` |
| A property of the synthesized template: a resource, a setting, a count, an absence | a test: `` `tests/unit/test_fleet.py` fails when the group's maximum exceeds the cap `` |
| A security invariant | a test under `` `tests/policy/` `` |
| Behaviour of code that runs on the infrastructure | a unit test with fakes, or a replayed capture |
| Lint, types, lock file, repository gates | `` `scripts/check.sh all` exits 0 ``, `` `uv sync --locked` `` |
| A file or a text is there and says something | a test, or `` `grep -q ... path` `` |

The item carries that command or test path in backticks. Prefer a test that stays in the
repository to a one-off command: it keeps holding after the merge.

What only the real account can show is never claimed from a template. Write it as an item of
its own that ends with `(needs <capability>)`:

| Capability | When |
| --- | --- |
| `aws:deployed-environment` | It can only be seen on what the pipeline deployed: the endpoint answers, the fleet scales from zero, a student logs in, a dashboard shows data. |
| `aws:account` | It takes credentials for the account: a bootstrap, a quota request, a lookup. |
| `aws:console` | It is done or seen in the AWS console. |
| `gpu:capacity` | It takes a real GPU instance: the model loads, a throughput figure. |
| `human:owner-decision` | The owner has to decide something. |
| `host:container-runtime` | It starts a container. Such a test lives under `tests/container/` and runs in a CI job of its own, never in the local gate. |

Say in the item what the owner would run or look at. The loop still builds and proves the rest
of the issue, merges it, and lists the item as the owner's obligation. Split a statement that
is partly provable into two items: the template property with its test, and the observation
with its capability.

What only a reader can judge (a text is clear, a diagram is right) is a deliverable, proven by
the examiner's review. It is not an acceptance item.

## 4. Lint, then read the plan

```
scripts/backlog.sh lint
scripts/backlog.sh plan --zones
```

Run the lint until it prints no problem. Each line is `path:line: message` and says what to
change. Fix the draft, never the lint, and never the hygiene scanner's allow-list. Then read
the plan: the order, the waves, and the zones the engine will take for each issue. A zone you
did not mean is a backticked token to reword; two issues of one wave on the same file are a
dependency you forgot or a split to reconsider.

Report what you drafted: the ids and titles, the waves, and every open question you left out
of the drafts.

## 5. Publishing is the owner's step

`scripts/backlog.sh publish` prints what would be created on GitHub and sends nothing. Run it
only if asked to show that. `scripts/backlog.sh publish --apply` creates the issues, their
sub-issue and dependency links and the labels.

- Never run `publish --apply` unless the owner asked for it in this conversation, after seeing
  the dry run. Nothing may be published before the green light of `docs/factory/green-light.md`.
- Never create, edit or close an issue another way (`gh issue create`, the API) to get around
  the tool.
- After publication the issue on GitHub is the truth. Do not edit a published draft to change
  the work; the publisher never updates an issue. New work is a new draft with a new id; it
  joins a published epic, or waits for a published issue, by its GitHub number
  (`epic = 12`, `blocked_by = [57]`).
