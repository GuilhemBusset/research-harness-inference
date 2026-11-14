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
