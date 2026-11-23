+++
id = "E0-36"
title = "Implement loop: a clean update from main keeps the certificate of the head it merged"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = 2
+++

## Context

When an issue is in `land` and `main` has moved since its certificate, the engine merges `main`
into the certified head, and the new head goes through verification and a new examination (the
"focused delta examination" of the land step in
`tools/implementation-loop/implement_loop/engine.py`) before it may land.

The retrospective of 2026-11-23 measured it: with three roots landing at once, 20 heads were
updated in `land`, which took 20 examinations, 17% of the period's Codex tokens and 62 minutes of
examiner time; one issue was updated five times. Each examination was shown `main`'s change,
already certified in its own issue. 19 of the 20 certified; the other found a defect in the
issue's own code that its first examination had missed. The engine already lands a head the owner
lands without bringing it up to date at all.

The owner decided on 2026-11-23 that a clean update keeps the certificate. What stays: the merged
head is verified again by the verifier, on the exact head, and the `required` check of CI runs on
it before the merge. What goes: the new examination. A merge with a conflict is the builder's work
as today, and the head that resolves it is examined as today. Landing an owner's head is
unchanged.

## Deliverables

- [ ] In `tools/implementation-loop/implement_loop/engine.py`, a certified head that the land step brings up to date with main without a conflict is verified again and, when that verification passes, carries the certificate of the head it merged and lands with no new examination.
- [ ] The engine journals each carried certificate as one event that names the issue, the certified head, the merged head and the commit of main it merged, and the pull request's evidence comment names the head the examiner certified.
- [ ] A merged head whose verification fails goes back to the builder as a failed round and does not land, and a head whose update from main conflicts goes to the builder and its resolved head is examined, as before.
- [ ] `AGENTS.md` (Enforcement), `docs/factory/operating.md`, `tools/implementation-loop/README.md` and an amendment of `docs/adr/0001-software-factory.md` say that the certificate carries across a clean update from main, the merged head still verified and checked by CI.

## Acceptance criteria

- [ ] `tools/implementation-loop/tests/test_carried_certificate.py` lands an issue whose certified head is brought up to date with main without a conflict: the merged head is verified, it lands, and the examiner is not called again.
- [ ] `tools/implementation-loop/tests/test_carried_certificate.py` holds that a merged head whose verification fails goes back to the builder and does not land.
- [ ] `tools/implementation-loop/tests/test_carried_certificate.py` holds that a head whose update from main conflicts goes to the builder and that its resolved head is examined before it lands.
- [ ] `scripts/check.sh engine-tests` exits 0.

## References

- `tools/implementation-loop/implement_loop/engine.py`
- `docs/adr/0001-software-factory.md`
- `docs/factory/operating.md`
