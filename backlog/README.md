# Backlog

**The backlog is this repository's GitHub issues**: one tracking issue per epic, its work as
sub-issues, linked by their dependencies. The issue on GitHub is the source of truth; the implement
loop reads it and writes its evidence there.

The implement loop reads GitHub issues; it does not write them from a design. So new work is
drafted here first, as files, one per issue, in the shape the loop's engine parses, then linted and
published by `tools/backlog/` ([ADR 0001](../docs/adr/0001-software-factory.md), decision 7). Once
published, a draft is removed by a later change, so `backlog/drafts/` is empty between two
publications.

```
scripts/backlog.sh lint               # every draft against the engine's input contract
scripts/backlog.sh plan [--zones]     # the order and waves the dependencies give
scripts/backlog.sh publish            # dry run: what would be created on GitHub; nothing is sent
scripts/backlog.sh publish --apply    # create it; a second run creates nothing
```

Exit status: 0 fine, 1 the drafts have problems (one per line, `path:line: message`), 2 the tool
could not do its job. `plan` and `publish` run the lint first and do nothing until it is clean.

To write drafts, use the `backlog` skill (`/backlog` in Claude Code, `$backlog` in Codex). This
file is the reference for the format.

## Lifecycle

1. **Draft.** A file under `backlog/drafts/`, written from the design or from a need.
2. **Lint.** `scripts/backlog.sh lint` until it prints no problem, then `plan` to read the order.
3. **Commit.** Drafts are reviewed and merged like any change.
4. **Publish.** The owner runs `publish`, reads the dry run, then runs `publish --apply`.
5. **After publication the issue on GitHub is the truth.** The publisher never edits, closes or
   deletes an issue. To change published work, edit the issue. The draft is history: a later
   housekeeping change removes it. An id is never used again.

Work that comes later enters the same way. A new draft joins a published epic, or waits for a
published issue, by naming its GitHub number (see `epic` and `blocked_by` below).

## Layout

```
backlog/drafts/
  E0/
    epic.md
    01-deployment-status.md
  E1/
    epic.md
    01-toolchain-pins.md
    02-empty-app.md
```

- One folder per epic, named `E<number>`: `E0`, `E1`, up to `E999`, no leading zero.
- `epic.md` is the epic. Its id is the folder name: `E1`.
- `<nn>-<slug>.md` is an issue: two digits, unique in the folder, then lower-case words joined
  by hyphens. Its id is `<folder>-<nn>`: `E1-02`.
- Nothing else goes under `backlog/drafts/`.

Every file starts with TOML front matter between two `+++` lines, followed by the body in
Markdown.

## Front matter

An issue:

| Key | Value |
| --- | --- |
| `id` | The id the path gives, as a string: `"E1-02"`. |
| `title` | The issue title: one line, at most 256 characters, used by no other draft. |
| `labels` | Exactly one size, exactly one kind, and `loop:infra` when it applies (see Labels). |
| `epic` | The epic of its folder, `"E1"`. When the epic is already on GitHub and the folder has no `epic.md`: its issue number, as an integer (`epic = 12`). |
| `blocked_by` | Optional list of what must be delivered first: ids of other issue drafts (`"E1-01"`), and numbers of issues already on GitHub (integers). Never an epic, never the draft itself, no cycle. |

An epic (`epic.md`):

| Key | Value |
| --- | --- |
| `id` | The folder name: `"E1"`. |
| `kind` | `"epic"`. |
| `title` | As for an issue. |
| `labels` | Optional: at most one kind. No size, no `loop:infra`. |

Any other key is an error. An epic needs at least one issue draft in its folder.

### Labels

| Label | Meaning |
| --- | --- |
| `size:S`, `size:M`, `size:L` | How much work the issue is. The engine sets the issue's ceiling of agent calls from it. Prefer S and M; split an L if it can be split. |
| `kind:infra` | AWS infrastructure, as CDK code. |
| `kind:app` | Code that runs on the infrastructure. |
| `kind:test` | Tests and invariants. |
| `kind:docs` | Documentation. |
| `kind:factory` | The software factory itself: the engine, the gates, this tooling. |
| `loop:infra` | Factory tooling: the engine gives the issue its larger call ceiling. It does so by itself when a zone is under `tools/`, `scripts/`, `.github/` or `setup/`. |

No other label can be drafted. The engine creates and moves its own `loop:*` labels.

## Body of an issue

The engine reads the body line by line. What follows is what it reads, and so what the lint
enforces.

```
## Context               optional, free text; any other `## ` section is free text too

## Deliverables          required, once
- [ ] one item per line

## Acceptance criteria   required, once
- [ ] one item per line

## References            optional
- `path/in/the/repository.md`, section 4
```

**Deliverables and Acceptance criteria are the ledger.** Each `- [ ] ` line is one item the loop
must prove before the issue is done. Under these two headings:

- Every non-blank line is one item, on one line: `- [ ] text`. Nothing else: no prose, no
  sub-list, no wrapped line. The engine would ignore it, so it would never be proven.
- An item is not ticked and carries no evidence link: the engine adds both.
- An item has at least three words and is not repeated in the issue. The examiner holds the
  work to an item by quoting it.

**Deliverables name the issue's zones.** In Deliverables, the engine takes every backticked
token that looks like a path (it contains a `/`, or ends like a file name) for a path the issue
changes. It runs two issues at the same time only when their zones do not overlap, and an issue
that names no path is written alone. So:

- Name at least one path, relative to the repository root: `` `infra/fleet.py` ``,
  `` `tests/policy/` ``.
- In Deliverables, keep backticks for paths. A token such as `m5.large`, a network range or a
  media type would be taken for a path, and the lint refuses it: write it without backticks.
  `scripts/backlog.sh plan --zones` shows the zones the engine will take.
- Some paths are protected: the engine, `tools/hygiene/`, `.github/`, `.githooks/`,
  `tests/policy/` and the gate scripts. A builder changes them only when the issue names them
  or the examiner approves it (ADR 0001, decision 10). Name one only when the issue is about it.

**An acceptance item names its check, or the capability it needs.** Nothing in between:

- *Its check:* the item carries, in backticks, the command or the test that shows it.
  - A command is one of `uv`, `uvx`, `python`, `python3`, `pytest`, `ruff`, `ty`, `cdk`, `npx`,
    `npm`, `node`, `bash`, `sh`, `git`, `grep`, `rg`, `diff`, `cmp`, `test`, `jq` followed by
    its arguments (`NAME=value` may come first), or a shell script of the repository
    (`` `scripts/check.sh all` ``).
  - A test is a path under a `tests/` folder, or a file named `test_*.py` or `*_test.py`, with
    or without `::name`: `` `tests/policy/test_ingress.py::test_no_open_rule` ``.
- *The capability it needs:* the item ends with `(needs <capability>)`, outside backticks, with
  one of the capabilities `tools/implementation-loop/loop.toml` lists under `[host] lacks`:

  | Capability | Use it when the item can only be shown |
  | --- | --- |
  | `aws:account` | with credentials for the account: a bootstrap, a quota, a lookup. |
  | `aws:deployed-environment` | on what the pipeline deployed: an endpoint that answers, a fleet that scales, a login. |
  | `aws:console` | or done in the AWS console. |
  | `gpu:capacity` | on a real GPU instance. |
  | `human:owner-decision` | by a decision of the owner. |
  | `host:container-runtime` | by starting a container. Such a test lives under `tests/container/` and runs in a CI job of its own, never in the local gate. |

  The loop builds and proves the rest of the issue, merges it, and hands this item to the owner
  as an obligation. One capability per item; a deliverable may carry one too.
- A command only an account can answer (`aws ...`, `cdk deploy`, `bootstrap`, `destroy`, `diff`,
  `watch`, `import`, `rollback`, `gc`, `drift`) is never a check: agents hold no AWS
  credentials. An item that rests on one must end with `(needs <capability>)`.

What only a reviewer can judge (the quality of a text, a design choice) is a deliverable, which
the examiner may prove by review. It is not an acceptance item.

**References.** Under `## References`, each backticked path on a line without a Markdown link
must exist in the repository now, or be named in the Deliverables of an issue this one is
blocked by, directly or through others. Cite the design section the issue implements.

**Everywhere in the body:**

- No relative Markdown link (`[text](docs/x.md)`): it leads nowhere from an issue. Name the path
  in backticks, or give a full URL.
- No `<details>` block, and no `<!-- backlog-id: ... -->` line: the publisher adds that marker.
- Optionally, outside the two ledger sections, one line `` Suggested branch: `type/short-description` ``
  (lower case, used by no other draft, not under `loop/`). Without it the engine names the
  branch `loop/issue-<number>`.
- Nothing that identifies the AWS account: the hygiene scanner reads every draft, and again
  every title and body as they are sent.

## Body of an epic

Free text: what the epic is for, what it leaves out, the design section behind it. No
`## Deliverables`, no `## Acceptance criteria`: an epic is a tracking issue, the engine works
its sub-issues and never reads its checkboxes. The rules of "Everywhere in the body" apply.

## Example

These two files are kept under `tools/backlog/tests/fixtures/good/`, where the tests lint them.

`backlog/drafts/E1/epic.md`:

<!-- example: E1/epic.md -->
````markdown
+++
id = "E1"
kind = "epic"
title = "Walking skeleton: an app that synthesizes with no credentials"
labels = ["kind:infra"]
+++

The smallest CDK application the rest of the backlog can grow from: pinned tools, one empty
stack, and the first invariant over the synthesized template. Nothing here is deployed by an
agent (`docs/design/system-design.md`, section 8).

Done when every sub-issue is closed.
````

`backlog/drafts/E1/02-empty-app.md`:

<!-- example: E1/02-empty-app.md -->
````markdown
+++
id = "E1-02"
title = "An empty app that synthesizes with no credentials"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-01"]
+++

## Context

Every later issue adds a construct to this app and a test over its template. The app must
synthesize on a machine that has never seen an AWS account: no lookups, no environment
variables, no context file.

Suggested branch: `feat/empty-app`

## Deliverables

- [ ] `app.py` defines one app with one stack, `infra/stack.py`, that holds no resource yet.
- [ ] `tests/unit/test_synth.py` synthesizes the app in a temporary directory and reads the template back.

## Acceptance criteria

- [ ] `npx cdk synth --quiet` exits 0 with every `AWS_*` variable unset and no `cdk.context.json` in the tree.
- [ ] `uv run pytest tests/unit/test_synth.py` passes and writes nothing outside its temporary directory.
- [ ] The pipeline's first execution deploys the empty stack to the account (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, section 8.2
- `cdk.json`, delivered by the toolchain issue
````

`cdk.json` does not exist yet: the reference holds because `E1-01`, which this issue is blocked
by, names it in its Deliverables.

## What the lint needs

The lint reads each body with the engine's own parser
(`tools/implementation-loop/implement_loop/issue_parser.py`, functions `sections()` and
`ledger()`), takes the capabilities from `tools/implementation-loop/loop.toml`, and runs the
hygiene scanner (`tools/hygiene/scan.py`) over every draft. If one of the three is missing it
fails; it never passes by default. If the engine's parser ever reads a body differently from
the lint, the lint stops and says so: `tools/backlog/backlog.py` must then follow the engine.

## Publishing

`scripts/backlog.sh publish` is a dry run. It calls neither `gh` nor the network and prints what
`--apply` would create. Only the exact flag `--apply` publishes: no abbreviation, no environment
variable, no configuration file.

`scripts/backlog.sh publish --apply` refuses, before any call, unless all of this holds:

- the lint is clean, and the hygiene scan of every title and body, as they would be sent, is
  clean;
- the repository has an `origin` remote on github.com, and the command runs at its top;
- the repository has at least one commit, and `backlog/drafts/` has no uncommitted change: what
  is published is what the history holds.

Then it asks GitHub for the repository and for the head of its default branch, and refuses if
nothing has been pushed there yet. A remote that exists is not the owner's green light: the
first push is, and [`docs/factory/green-light.md`](../docs/factory/green-light.md) puts it
before the publication. On that refusal no label, issue or link has been read, let alone
written. It also refuses a repository that answers under another name or has issues disabled.

Then it reads the labels and all the issues, and refuses, before any write, if: two issues
carry the same marker; an issue without a marker has the title of a draft; a label exists in
another letter case (`Size:S`); a number named by a draft is unknown or is a pull request.

Then it creates, in this order, only what is missing:

1. the nine labels above;
2. the epics, then the issues in dependency order, so issue numbers follow the order of work.
   Each body is the draft's body under one added first line, `<!-- backlog-id: E1-02 -->`;
3. each issue as a sub-issue of its epic;
4. each `blocked_by` link.

**Idempotent.** An issue whose body carries a draft's marker is that draft, whatever became of
its title, text or state: it is never created again and never edited. Links that already exist
are left alone. A second `--apply` therefore creates nothing, and an `--apply` that failed
halfway is completed by running it again. Nothing is retried within a run. Run one at a time:
two at once could create an issue twice.

The calls, through `gh api --hostname github.com`, with REST API version `2022-11-28` (the one
the engine reads with), one second apart for the writes:

| Purpose | Call |
| --- | --- |
| The repository | `GET /repos/{owner}/{repo}` |
| Has anything been pushed | `GET /repos/{owner}/{repo}/branches/{default_branch}`: a 404 on an empty repository |
| Labels | `GET /repos/{owner}/{repo}/labels`, `POST /repos/{owner}/{repo}/labels` |
| Issues | `GET /repos/{owner}/{repo}/issues?state=all`, `POST /repos/{owner}/{repo}/issues` |
| A numbered issue a draft names | `GET /repos/{owner}/{repo}/issues/{number}` |
| Sub-issues | `GET` and `POST /repos/{owner}/{repo}/issues/{epic}/sub_issues`, body `{"sub_issue_id": <id of the child>}` |
| Dependencies | `GET` and `POST /repos/{owner}/{repo}/issues/{number}/dependencies/blocked_by`, body `{"issue_id": <id of the blocker>}` |

Both links take the `id` of an issue, not its number
([sub-issues](https://docs.github.com/en/rest/issues/sub-issues),
[issue dependencies](https://docs.github.com/en/rest/issues/issue-dependencies)). The engine
reads the same two listings back (`list_sub_issues` and `list_blocked_by` in
`tools/implementation-loop/implement_loop/github.py`), and the tests run the engine's own
planner on what the publisher created.

The token `gh` uses needs read and write access to the repository's issues.
