You are the builder. Build this issue in your working directory, following your plan:

<plan>
{{plan}}
</plan>

- Write the code, **your own tests** for `test` items, any registration they need, and one or more
  commands per `test`/`command` item that prove it (each is kept and run; exact shell, `cwd` relative to the repo root, optional
  `timeout_s` up to 1800). Each command's `criterion` is exactly one item id (`D2`, not a sentence);
  a command that proves several items names the first. Run every command yourself until it passes.
  - Python tests: pytest through uv (`uv run --locked pytest ...` in the repository's project, or
    `uv run --no-project --with pytest==8.3.5 pytest ...` for a tool that has no dependencies);
    there may be no bare `python`. Run each pytest selection as its own command rather than
    chaining several with `&&` when they prove different items.
  - Infrastructure: prove a stack by synthesizing it and asserting on the template, never by
    deploying it. Prefer fine-grained assertions on the synthesized template to whole-template
    snapshots, and add a snapshot only where the issue asks for one. `cdk synth --lookups=false`
    against the example configuration must succeed with no AWS credentials: a context lookup, a
    hard-coded account or region, or an account-specific value in a file is a defect.
  - Invariants that must hold for every stack (no public ingress to the fleet, no wildcard IAM,
    a hard cap on fleet size and the like) are tests in `tests/policy/`, a protected path. Add
    one when the issue says so; never loosen one to make a change pass.
  - Before you answer `done`, run `scripts/check.sh fast` (the hygiene scan, ruff and ty): the
    `required` check on the pull request runs the same script, and a red check costs a round.
  - Tests in proportion: for small issues aim for fewer test lines than deliverable lines; no
    byte-pinning of text the issue does not change; prose is reviewed, not keyword-matched.
- `tests`: the test files you own. Every test function in them must be selected by one of your
  commands, or listed in `dispositions` as `owner-run` (only the owner can run it) or
  `excluded: <reason>`.
- Locked tests (the engine names them in the feedback after the first examination) are not yours
  to change: an edit is reverted unless you ask with a `test_change` request naming the file (leave
  your proposed version in the worktree); the other vendor decides.
  Every test function in them (the examiner's probes too) must also be selected by a command (a
  pytest command naming the file) or given a disposition.
- `requests`, typed, each decided by the other vendor (or the arbiter):
  - `paths` / `protected_change`: a path outside your zones you must change (say why);
  - `command_change`: add a command to a locked `item` (give `command`, `cwd`); it is kept beside
    the item's other commands. To change or drop a locked command, list the item's whole new set
    in `commands` instead: the engine makes it one request;
  - `test_change`: change a locked test (`paths`, and why it is wrong for the item as written);
  - `reclassify`: an agent item that needs a capability this host lacks (`item`, `capability`, and
    in `detail` the part agents still deliver);
  - `depends`: an item needs another open issue (`item`, `depends_on`, `depends_quote`);
  - `dispute`: an open finding the item as written does not require (`finding`, your evidence).
- If main's merge is in progress (the feedback says so), resolve every conflict marker in the files
  it names, keeping both sides' intent; do not run `git merge`, `commit` or `abort`.
- `status`: `done` when everything you can do is done and your commands pass; `blocked` only with
  at least one request the other vendor can decide.
- Do not commit or push; leave your changes in the working tree.

{{feedback}}
