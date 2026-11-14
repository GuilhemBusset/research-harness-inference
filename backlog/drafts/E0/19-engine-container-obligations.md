+++
id = "E0-19"
title = "Engine: discharge a container-runtime obligation from the CI job's result"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

The factory's host has no container runtime. `loop.toml [host] lacks` names `host:container-runtime`,
so an acceptance item that rests on a test under `tests/container/` becomes an owner obligation and
its issue ends `delivered` (ADR 0001, decision 6 and amendment 5). But nothing is left for the owner
to do: the CI job `container-tests` (issue E0-17) runs those tests on the very head that lands, and
it is part of the required check. Amendment 5 leaves to the backlog letting the engine discharge
such an obligation from that job's result.

The capability is already in `loop.toml`, whose comment says that the engine does not read the
job's result yet. That reading is all this issue adds; it adds no capability.

The engine already reads the check run named `required` on a commit; this issue has it read one
more, by name, for the obligations a configured CI job provides.

This issue changes the engine and `loop.toml`, protected paths on the safety surface (ADR 0001,
decision 10); the builder may change the files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/loop.toml` maps, in its table host, the capability host:container-runtime, which it already lists, to the name of the CI job that provides it, container-tests; its comment no longer says the result is not read; and `tools/implementation-loop/implement_loop/config.py` reads that mapping.
- [ ] `tools/implementation-loop/implement_loop/engine.py` discharges, when an issue lands, an obligation whose capability has such a mapping and whose item names a test under the folder tests/container/, if the check run of that name succeeded on the head that was merged: the item is recorded as proven with the check run's address as its evidence, and is not listed for the owner.
- [ ] When that check run is absent, was skipped, or did not succeed, the obligation stays the owner's, unchanged.
- [ ] An obligation for any other capability, and one whose item names no test under that folder, is handled as before.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_container_obligations.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_container_obligations.py` covering a succeeded check run, an absent one and a skipped one on the fake GitHub, and with no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_container_obligations.py::test_other_obligations_are_untouched` shows that an item that needs aws:deployed-environment stays the owner's whatever the check runs say.
- [ ] `tools/implementation-loop/tests/test_container_obligations.py::test_an_issue_with_nothing_left_is_done` shows that an issue whose only obligation was discharged ends done, not delivered.

## References

- `docs/adr/0001-software-factory.md`, decision 6 and amendment 5
- `docs/design/system-design.md`, section 8.3
