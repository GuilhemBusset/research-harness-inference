+++
id = "E2-56"
title = "The admin CLI: the roster, direct keys, revocation and the canary, with the admin's own credentials"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-49", "E2-51", "E2-67"]
+++

## Context

The roster is keyed by numeric GitHub ID, and the admin manages it with a CLI that uses the admin's
own AWS credentials; there is no public admin API (section 6.4). GitHub allows 50 code confirmations
an hour per app, so an admin command that issues a key directly is the fallback. Per-student series
are labelled with a roster alias, never the GitHub ID (section 6.6), so every roster entry has one.

A student's daily token budget defaults to 100 million tokens, input plus output, cached input
included, and their concurrent requests to 4 (sections 11.3 and 17). The budget is large because an
agent re-sends its transcript on every request; it is a fairness guard between students, and the
GPU-hour ceilings are the cost cap. These two defaults are the admin CLI's own: only it reads them,
when it writes a roster entry, so they are not keys of the stage's configuration.

**Budgets travel with the keys.** The roster and key items are issue E2-67's. The issuer copies the
roster entry's budget and limit into each key it mints (issue E2-50), so that the gateway answers a
request with one read of the keys table (issue E2-14). A change of a student's budget or limit is
therefore written to the roster entry and to each of the student's active key items, and the gateway
applies it within its 60-second cache. A key the admin issues directly is the same item.

The pipeline's verification runs with a canary roster entry (section 8.2, step 6; issue E2-64). The
canary command keeps the canary's key in one Secrets Manager secret named rhi/canary-key, which the
verification step reads; the command creates that secret if it does not exist, so that the owner
can run it before the verification is merged.

The tables' logical IDs are pinned by issue E2-49, so the CLI can find each table in the deployed
stack by its logical ID, with the admin's credentials, and the repository holds no table name. The
pool modes and the export and erasure of a student's data are other commands of the same CLI
(issues E4-03 and E4-19). The tests run against tables and a secret that moto holds; keys are
generated at run time, because a literal key in a test file fails the hygiene scan (issue E0-16).

## Deliverables

- [ ] `admin/rhi_admin/roster.py` adds, lists and removes roster entries: the numeric GitHub ID, an alias, and the daily token budget and the concurrency limit, which are 100 million tokens and 4 unless the admin gives others, a budget of 0 included (the spent-budget canary student of the smoke tests); removing an entry revokes its keys.
- [ ] Changing an entry's budget or limit writes the new values to the roster item and to every active key item of the student.
- [ ] `admin/rhi_admin/keys.py` issues a key for a roster entry directly, as the key item of issue E2-67 with the entry's alias, budget and limit, printing the key once, and revokes a student's keys.
- [ ] A command creates the canary roster entry under a fixed alias, issues its key, and writes the key into the secret rhi/canary-key, creating that secret if it does not exist, without printing the key; running it again replaces the key.
- [ ] `admin/rhi_admin/main.py` is the command line; it finds each table in the deployed stack by the table's logical ID, with the caller's credentials, and holds no account, region or table name.
- [ ] `tests/unit/admin/test_roster.py` covers the commands against tables and a secret that moto holds, with every key generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin/test_roster.py` passes: an entry is added, listed and removed, an entry added without numbers has a budget of 100 million tokens and a limit of 4, a directly issued key verifies and is stored as its SHA-256 only, and removing an entry leaves it no active key.
- [ ] `tests/unit/admin/test_roster.py::test_the_canary_key_is_never_printed` runs the canary command twice and finds one secret holding the second key, and neither key in the output.
- [ ] `tests/unit/admin/test_roster.py::test_a_budget_change_reaches_the_keys` changes an entry's budget and limit and finds the new values in each of the student's active key items, and none in a revoked one.
- [ ] The roster is entered through the admin CLI, which is the owner's ninth one-time step (needs aws:account).
- [ ] The owner runs the canary command once before the pipeline's verification of issue E2-64 is merged (needs aws:account).

## References

- `docs/design/system-design.md`, sections 6.4, 6.6, 8.2, 8.6, 11.3, 17
- `docs/design/evidence.md`, E17
