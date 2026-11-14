+++
id = "E0-02"
title = "Make the pipeline stack and the bootstrap policies protected paths"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

A merge to `main` is deployment rights in the account. ADR 0001's first amendment therefore adds the
pipeline stack and the bootstrap policy to the protected paths, from the first issue that creates
them: a builder changes them only when the issue names them or the examiner approves a typed
request. The engine keeps that list in `PROTECTED_PREFIXES` of its configuration module; the plan
and decide prompts and `AGENTS.md` repeat it for the agents and for people. Issue E1-08 creates the
bootstrap policies under `setup/bootstrap/`, and issue E1-09 the pipeline stack under
`src/rhi/pipeline/`. Neither folder exists yet, which does not matter: a prefix protects whatever
lands under it. Neither joins the safety surface; the ADR does not ask for it.

This issue changes `tools/implementation-loop/implement_loop/config.py` and two prompts of the
engine, protected paths, `config.py` on the safety surface (ADR 0001, decision 10). The builder may
change them because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/config.py` adds the folders src/rhi/pipeline/ and setup/bootstrap/ to the protected prefixes, and to nothing else.
- [ ] `tools/implementation-loop/prompts/plan.md` and `tools/implementation-loop/prompts/decide.md` name the pipeline stack and the bootstrap policies where they list the protected paths.
- [ ] `AGENTS.md` names the two folders where it lists the protected paths, in place of "the pipeline definition, once it exists".
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_protected_zones.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_protected_zones.py` showing that a file under each of the two folders is protected, that a plan zone there is withheld unless the issue names it, and that neither folder is on the safety surface.

## References

- `docs/adr/0001-software-factory.md`, decision 10 and amendment 1
- `docs/design/system-design.md`, section 8.4
- `AGENTS.md`, "Protected paths and the safety surface"
