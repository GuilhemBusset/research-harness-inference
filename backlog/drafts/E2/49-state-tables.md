+++
id = "E2-49"
title = "The DynamoDB tables, with their key schemas, protected against deletion and against a rename"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-67", "E2-88"]
+++

## Context

State is in DynamoDB on demand: keys, roster, usage, and pool state with its heartbeats (section 2).
There is one environment, so every table has point-in-time recovery, deletion protection and a
retain policy (section 8.5), and the stateful-resource invariant of issue E2-88 records its logical
ID. Once recorded, a table's stack and logical ID cannot change without replacing it, so both are
fixed here:

| Table | Stack, module | Partition key | Sort key | Index |
| --- | --- | --- | --- | --- |
| keys | access, `src/rhi/access/stack.py` | `key_hash` (string) | none | `by_student`: `github_id` (number), `issued_at` (number) |
| roster | access | `github_id` (number) | none | `by_alias`: `alias` (string) |
| usage | access | `github_id` (number) | `day` (string) | none |
| pool-state | pools, `src/rhi/pools/stack.py` | `pool` (string) | `record` (string) | none |
| heartbeats | pools | `pool` (string) | `source` (string) | none |

Every table's expiry attribute is `expires_at`. The keys, the indexes and the expiry come from
`services/common/rhi_common/tables.py` (issue E2-67), which the services read too, so that the tables
and their writers cannot disagree.

The pool tables live in the pools stack, beside the groups and the scaler that use them, and this
issue creates that stack; the internal load balancer, the groups and the rest of the pools join it
(issue E2-44 first). The student tables live in the access stack, which the issuer and the admin's
sign-in join (issues E2-52 and E2-85). Parking a deployment leaves both stacks and every table in
place (issue E4-17). The pools stack references other stacks (the workspace, the network); no stack
it references may reference it back.

## Deliverables

- [ ] `src/rhi/access/tables.py` creates the keys, roster and usage tables in the access stack that `src/rhi/access/stack.py` defines, and `src/rhi/pools/tables.py` the pool-state and heartbeat tables in the pools stack that `src/rhi/pools/stack.py` defines; `src/rhi/stage.py` adds both stacks to the stage.
- [ ] Each table takes its partition key, its sort key, its expiry attribute expires_at and its secondary indexes from the shared schema of issue E2-67, as the table in Context gives them, and is billed on demand.
- [ ] Every table has point-in-time recovery, deletion protection and a retain policy on deletion and on replacement, and `tests/policy/stateful_logical_ids.json` records the five tables.
- [ ] Every cdk-nag finding `src/rhi/access/tables.py` or `src/rhi/pools/tables.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/access_tables.toml` or `tests/policy/acknowledged/pools_tables.toml`.
- [ ] Every cdk-nag finding `src/rhi/access/stack.py`, `src/rhi/pools/stack.py` or `src/rhi/stage.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/access_stack.toml`, `tests/policy/acknowledged/pools_stack.toml` or `tests/policy/acknowledged/stage.toml`.
- [ ] `tests/synth/test_tables.py` checks each table's stack, keys, indexes and expiry against the shared schema.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_tables.py tests/policy/test_stateful_resources.py` passes on the app as it stands: five tables, the keys, roster and usage tables in the access stack and the other two in the pools stack, each with the keys and indexes of Context, its expiry on expires_at and its three protections.
- [ ] `tests/synth/test_tables.py::test_the_schema_is_the_shared_one` fails when a table's key or index differs from the shared schema of issue E2-67.
- [ ] `scripts/check.sh synth` exits 0 with the access and pools stacks in the stage.

## References

- `docs/design/system-design.md`, sections 2, 8.4, 8.5
