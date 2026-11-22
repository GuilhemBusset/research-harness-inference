+++
id = "E4-50"
title = "Remove the per-student budget fields from the keys, the roster, the usage rows, the issuer and the admin CLI"
labels = ["size:M", "kind:app"]
epic = 6
blocked_by = ["E4-48", "E4-49"]
+++

## Context

There is no per-student token budget (ADR 0003, decisions 2 and 3; section 14). Once the gateway's
ledger takes only a concurrency slot (issue E4-48) and the spikes' spent student is gone (issue
E4-49), nothing reads the budget's fields; they are still written:

- a key's and a roster entry's daily_token_budget (`services/common/rhi_common/students.py`), which
  the admin CLI sets (`admin/rhi_admin/roster.py`, its default `DEFAULT_DAILY_TOKEN_BUDGET`, the
  option --budget and the budget column of `admin/rhi_admin/main.py`), and which the issuer and the
  admin copy onto each key they issue (`services/issuer/rhi_issuer/mint.py`, `admin/rhi_admin/keys.py`);
  the gateway's key record carries it (`services/gateway/rhi_gateway/keystore.py`);
- a usage day's tokens, and a lease's tokens and estimate, which the ledger writes as 0
  (`services/gateway/rhi_gateway/budget.py`).

They are removed here. A key's and an entry's concurrency_limit, the leases, their expiry, the
affinity cookies and the rebalance numbers stay, with the usage item one per student and UTC day.

**The rollout.** Items written before this change keep their attributes until they expire: every
reader of this release ignores them. The release before reads items without them (issue E4-48), and
this issue starts only once that release is merged; the access stack, where the issuer runs,
deploys before the gateway's stack, so for a moment the issuer of this release writes keys that the
gateway of the release before reads, and the gateway tasks of both releases share the usage rows.
The withdrawal's condition, which today requires a day's tokens to be 0, must hold as well for a day
written without tokens.

## Deliverables

- [ ] `services/common/rhi_common/students.py` gives a key and a roster entry no daily_token_budget, a day no tokens and a lease no tokens or estimate, writes none of them, reads an item that still holds one as if it did not, and its docstring's table follows.
- [ ] `services/issuer/rhi_issuer/mint.py` and `admin/rhi_admin/keys.py` issue a key without a budget, and the key record of `services/gateway/rhi_gateway/keystore.py` holds none.
- [ ] `services/gateway/rhi_gateway/budget.py` writes no tokens on a day and no tokens or estimate on a lease, its student has no daily budget, and its withdrawal deletes a day written without tokens on the same terms as one written with tokens 0.
- [ ] `admin/rhi_admin/roster.py` has no default budget and sets none, `admin/rhi_admin/main.py` has no option --budget and prints no budget, and `docs/runbooks/owner-setup.md` shows the roster commands without it.
- [ ] `tests/unit/common/test_students.py`, `tests/unit/common/test_tables.py`, `tests/unit/admin/test_roster.py`, `tests/unit/issuer/test_redeem.py`, `tests/unit/issuer/test_revoke.py`, `tests/unit/issuer/test_rules.py`, `tests/unit/gateway/test_keystore.py`, `tests/unit/gateway/test_auth.py` and `tests/unit/gateway/test_budget_dynamodb.py` cover the change, and every other test, fixture and harness that gives a key, an entry or a lease a budget, `tests/replay/harness.py` and `tests/smoke/conftest.py` among them, follows.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_students.py tests/unit/common/test_tables.py` passes: no item written holds daily_token_budget, tokens or estimate, and a key, an entry, a day and a lease written by the release before, with them, read back with their other attributes.
- [ ] `uv run --locked pytest tests/unit/gateway/test_budget_dynamodb.py` passes: a reservation, a settlement and a withdrawal each work on a day written by the release before, with tokens and leases holding tokens and an estimate, and on a day written without them.
- [ ] `uv run --locked pytest tests/unit tests/replay tests/smoke/test_smoke.py` passes.
- [ ] `grep -rn "daily_token_budget" admin client services/issuer services/gateway docs/runbooks` prints nothing.
- [ ] During and after the deployment of this change, a student logs in with rhi login and is served, and the pipeline's verification passes (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.2, 6.4 and 14
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decisions 2 and 3
- `services/common/rhi_common/students.py`
- `admin/rhi_admin/roster.py`
