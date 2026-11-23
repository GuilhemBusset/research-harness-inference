+++
id = "E0-37"
title = "Implement loop: the safety invariants' names in loop.toml alone, and the examiner pilot removed"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = 2
+++

## Context

The names of the safety invariants are kept in two places. `[invariants] names` in
`tools/implementation-loop/loop.toml` is the list the examiner and the examiner bound use, and #579
made four of its names specific. `DEFAULT_INVARIANTS` in
`tools/implementation-loop/implement_loop/gate.py` still holds the old names ("data loss", "a false
done") and is what the engine falls back to when `loop.toml` gives none, so the engine's tests run
with names no run uses.

The examiner pilot, `tools/examiner-pilot/` (684 lines, 10 seeded-defect patches, and a manifest
with a third copy of the names), ran once, on 2026-11-22; the owner decided (#121, closed). Its
patches apply only to one recorded commit, so a new pilot would need a new corpus anyway, and the
pilot can be restored from history. #579's first CI run failed on the pilot's copy of the names.

The owner decided on 2026-11-23: one list, the one in `loop.toml`, and the pilot removed.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/gate.py` holds no list of invariant names: the engine and the engine's tests read the names from `tools/implementation-loop/loop.toml` alone, and an engine whose loop.toml gives none refuses to start with an error that names the invariants table.
- [ ] `tools/examiner-pilot/` is removed, and `pyproject.toml`, `ruff.toml`, `ty.toml`, `README.md` and every test that lists the repository's package roots no longer name it.
- [ ] `docs/adr/0001-software-factory.md` says, where it names the seeded-defect pilot, that the pilot ran once, the owner decided, and the pilot was removed.

## Acceptance criteria

- [ ] `tools/implementation-loop/tests/test_invariant_names.py` holds that the engine's invariant names are those of loop.toml and that an engine whose loop.toml gives none refuses to start.
- [ ] `test ! -e tools/examiner-pilot` exits 0, and `git grep -n -e examiner-pilot -e DEFAULT_INVARIANTS -- pyproject.toml ruff.toml ty.toml README.md tools/implementation-loop` finds nothing.
- [ ] `scripts/check.sh engine-tests` exits 0 and `scripts/check.sh fast` exits 0.

## References

- `tools/implementation-loop/loop.toml`
- `tools/implementation-loop/implement_loop/gate.py`
- `docs/adr/0001-software-factory.md`
