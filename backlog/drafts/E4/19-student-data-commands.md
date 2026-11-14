+++
id = "E4-19"
title = "The admin CLI exports and erases one student's data, and says what only retention removes"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-30", "E2-56", "E2-57"]
+++

## Context

The admin CLI can export and erase one student's data (section 6.6). What exists per student: the
roster entry, the keys' hashes and times, the usage rows (which also hold the student's affinity
cookies, section 5.5), the gateway's log records under the student's alias (issue E2-29), and the
series in Prometheus under that alias. Source addresses in load balancer and WAF logs cannot be
tied to a student by the system and leave with their retention (issues E4-18 and E4-37).

Managed Prometheus offers no call that deletes series, and CloudWatch Logs none that deletes one
record. What an erase can do there is cut the link: once the roster entry is gone, the alias names
nobody, and the series and log records under it expire with their retention, 90 days after they
were written. The command says so rather than claiming more.

The admin uses their own AWS credentials (section 6.4). The CLI finds each table, the gateway's log
group and the workspace in the deployed stacks by their logical IDs, as it finds the tables (issue
E2-56); the repository holds no name of a deployed resource.

The tests hold no student key: a key a test needs is generated at run time by the key module of issue
E2-01, because the hygiene scanner reports a literal key anywhere in the tree (issue E0-16).

## Deliverables

- [ ] `admin/rhi_admin/privacy.py` adds a command that exports, for one roster entry, as one JSON document: the entry, each key's issue, expiry and revocation times, the usage rows, the gateway's log records under the alias, and the values of the series labelled with the alias in the workspace.
- [ ] It adds a command that erases the same student: it revokes and deletes the keys, deletes the usage rows and the roster entry, and prints what it deleted and what only retention removes, naming the workspace's series and the gateway's, load balancers' and WAF's logs.
- [ ] `admin/rhi_admin/main.py` offers both commands, and `tests/unit/admin/test_privacy.py` covers them against stubbed clients.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin/test_privacy.py` passes: the export holds the entry, the usage rows, the log records and the series values of the stubs and no key and no hash; after an erase no table holds a row of the student and no key of the student verifies; the erase's output names the workspace and the log groups as removed by retention only.

## References

- `docs/design/system-design.md`, sections 5.5, 6.4, 6.6
