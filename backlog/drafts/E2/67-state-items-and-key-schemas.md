+++
id = "E2-67"
title = "The key, roster and usage items, and every table's key schema, defined once"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-03"]
+++

## Context

Five components write the same student records: the issuer mints keys (issue E2-50), the admin CLI
keeps the roster and issues keys directly (issue E2-56), the gateway reads a key on every request and
reserves and settles budget (issues E2-14 and E2-15), and the export, erasure and retention commands
read all of them (issues E4-19 and E4-35). The tables themselves are created by issue E2-49, and once
they exist their logical IDs are frozen by a policy test, so their keys cannot change later without
replacing a table. Each component would otherwise pick its own attribute names. This issue fixes them
once, in the shared package, with no AWS call.

The tables (DynamoDB on demand, section 2) and their keys:

| Table | Partition key | Sort key | Secondary index | What an item is |
| --- | --- | --- | --- | --- |
| keys | `key_hash` (string) | none | `by_student`: `github_id` (number), sorted by `issued_at` (number) | One API key |
| roster | `github_id` (number) | none | `by_alias`: `alias` (string) | One student |
| usage | `github_id` (number) | `day` (string, the UTC date as YYYY-MM-DD) | none | One student's day |
| pool-state | `pool` (string) | `record` (string) | none | The records of issue E2-03, and the scaler's memory |
| heartbeats | `pool` (string) | `source` (string) | none | The heartbeats of issue E2-03 |

Every table's expiry attribute is `expires_at`, whole seconds since the epoch. Section 6.6 sets
personal data to expire at course end plus 90 days; issue E4-35 makes every writer set it.

The items:

- **A key**: `key_hash`, the SHA-256 of the key in lower-case hexadecimal (section 6.4; the key itself
  is never stored); `github_id`; `alias`; `installation`, the identifier the CLI sends at login;
  `issued_at`; `expires_at`, the course end, after which the key is refused whatever the table's
  expiry has done; `revoked_at`, present only once the key is revoked; and the student's
  `daily_token_budget` and `concurrency_limit`, copied from the roster entry when the key is issued,
  so that the gateway reads one item per request (section 6.2, step 2). The index `by_student` finds a
  student's keys: the previous key's issue time a login shows, the key of an installation that a new
  login replaces, and every key that `rhi revoke --all` or the admin revokes.
- **A roster entry**: `github_id`, which handles cannot change (section 6.4); `alias`, the only name
  metrics and logs use (section 6.6); `daily_token_budget` and `concurrency_limit`; `added_at`;
  `expires_at`. The index `by_alias` finds an entry by its alias, as the canary command and the
  export and erasure commands do.
- **A day of usage**: `github_id`; `day`; `alias`; `tokens`, the tokens reserved and charged that
  day; `leases`, a map from a request's ID to its lease (`tokens` reserved, `estimate` of its input,
  `expires`); `expires_at`. Section 6.2, step 3 asks for one conditional write to reserve against
  the daily budget and take a concurrency slot, so both live in this one item. A lease stays in the
  item of the day its request started: for at most the longest request after midnight UTC, a student
  can hold up to twice their concurrency limit. Phase 2 adds the affinity cookie and the last
  rebalance number applied to this item (section 5.5), each absent until written.
- **The pool-state table's memory item**: the record `memory` holds the scaler's memory of earlier
  runs, whose attributes are the scaler's own (issue E2-05), except `launch_suspended`, the mark of
  issue E2-08 that the admin CLI clears (issue E4-03).

## Deliverables

- [ ] `services/common/rhi_common/tables.py` names the five tables of Context and gives, for each, its partition key and its sort key with their types, its expiry attribute expires_at, its secondary indexes with their keys, and, for the pool-state table, the three records mode, note and memory and the memory's attribute launch_suspended.
- [ ] `services/common/rhi_common/students.py` defines the key, the roster entry and the day of usage with the attributes of Context, each converting to and from a DynamoDB item.
- [ ] A key item without key_hash, github_id, alias, issued_at, expires_at, daily_token_budget or concurrency_limit is refused when read, and no item of this module writes an attribute outside the lists of Context: none holds a key in clear, a GitHub token, a request or a response.
- [ ] `tests/unit/common/test_students.py` covers the three items, and `tests/unit/common/test_tables.py` checks every item of that module and of the records of issue E2-03 against its table's keys.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_students.py` passes: each item survives a round trip through its item form, a key item without daily_token_budget is refused, and a day of usage with two leases reads back both with their tokens, estimates and expiries.
- [ ] `uv run --locked pytest tests/unit/common/test_tables.py` passes: every item of both modules carries its table's partition key and sort key, the key item carries the keys of the index by_student, and the roster item those of the index by_alias.
- [ ] `tests/unit/common/test_students.py::test_attributes_are_the_listed_ones` fails when an item of the module writes an attribute outside the lists of this issue's Context.

## References

- `docs/design/system-design.md`, sections 2, 5.5, 6.2, 6.4, 6.6, 8.5
