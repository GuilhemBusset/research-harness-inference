+++
id = "E4-23"
title = "Runbooks: a leaked key, a bad deployment, secret and token rotation"
labels = ["size:M", "kind:docs"]
epic = "E4"
blocked_by = ["E2-43", "E2-56", "E4-22"]
+++

## Context

Three of the nine runbooks of section 9. Each runbook is tested once.

- **A leaked key** is likely over a term: it is worth one student's daily token budget and GPU time
  bounded by the pool's ceiling (section 13). Keys start with `rhi_` and carry a checksum, so
  scanners recognise one in a homework branch; a revoked key stops working within 60 seconds, the
  gateway's lookup cache (section 6.4). What the key spent shows per alias on the students dashboard.
- **A bad deployment** shows as the commit statuses on the merge commit and the failure report that
  accompanies them (section 8.4). It is fixed forward or reverted through the same pipeline (section
  8.5); a change to a pool's release refreshes its instances (section 4.3).
- **Secrets and tokens:** the pools' bearer secrets (section 4.3), the three GitHub Apps' credentials
  stored in Secrets Manager (section 8.6), and the canary's key (section 8.2).

Each step is one of the three kinds issue E4-22 defines, and `tests/unit/test_runbooks.py` checks
them the same way. Here: revoking a student's keys and replacing the canary's key are admin commands
(issues E2-56 and E4-03); reading the commit statuses of a merge commit and reverting it are gh and
git steps; rotating a pool's bearer secret and a GitHub App's credential are steps in the AWS and
GitHub consoles, or aws commands, written out in full with placeholders, never with a real name or
ID. The leaked-key runbook shows no key, not even an invented one: the hygiene scanner reports
anything shaped like a key (issue E0-16); it names the prefix `rhi_` alone.

## Deliverables

- [ ] `docs/runbooks/leaked-key.md`: how a leak is noticed, the command that revokes a student's keys, the 60 seconds a revoked key may still work, how to read what the key spent, and what to tell the student.
- [ ] `docs/runbooks/bad-deployment.md`: how to read the commit statuses and the failure report, when to fix forward and when to revert, and what a revert does to instances already refreshed.
- [ ] `docs/runbooks/secret-rotation.md`: for each secret, how to rotate it and what restarts: a pool's bearer secret, each GitHub App's credential, the canary's key.
- [ ] Each runbook has the sections when, check, act and verify, writes each step as one of the three kinds of issue E4-22, and is listed in `tests/unit/test_runbooks.py`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_runbooks.py` passes for the three runbooks: each has the four sections, each rhi and admin command it shows parses, and each script it names exists.
- [ ] Each of the three runbooks is walked through once on the deployed system, and its date is recorded at its foot (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 6.4, 8.2, 8.4, 8.5, 8.6, 9, 13
