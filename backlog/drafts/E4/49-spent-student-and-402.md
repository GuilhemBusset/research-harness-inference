+++
id = "E4-49"
title = "Remove the cause budget_reached, the spikes' spent student and spike S2's 402 measurement"
labels = ["size:M", "kind:app"]
epic = 6
blocked_by = ["E4-48"]
+++

## Context

There is no per-student token budget (ADR 0003, decisions 2 and 3), and once the gateway's ledger
takes only a concurrency slot (issue E4-48) nothing in the gateway answers budget_reached. What is
left of that cause is removed here, with what existed only to provoke it:

- **the cause** budget_reached in `services/common/rhi_common/causes.py`, the last cause answered
  402, with the docstring's sentence on why the budget causes are 402;
- **the spent student**: the spikes' secret (`services/common/rhi_common/spike_keys.py`) holds,
  beside the students' keys, the key of one student whose daily budget is 0, which the admin CLI
  puts on the roster as `spike-spent` (`admin/rhi_admin/spikes.py`);
- **spike S2's 402 measurement**: `spikes/s2_client_hold.py` sends each client one request as that
  student and records whether the client retried the 402 and how it showed it, and
  `docs/measurements/s2.md` has those rows. S2's question, whether each client survives the hold,
  stays whole. Spike S3's jobs give the spent student's key to their redaction of keys
  (`spikes/s3_harness_task.py`, `spikes/s3_second_session.py`), and `spikes/common.py` describes it.

**The deployed secret.** `SpikeKeys.from_json` refuses a secret that is not exactly
`{"students": [...], "spent": "..."}`, and the deployed secret `rhi/spike-keys` has that shape. So,
until a later change removes the tolerance once the owner has rewritten the secret, the spike keys
read both the new shape, `{"students": [...]}`, and the old one, whose spent key they ignore; the
admin CLI writes the new shape. The spent student's roster entry and key stay in the tables until
the owner removes the entry with the admin CLI's roster remove, which also revokes its key; its
GitHub ID, 10^18, is one no GitHub account has.

## Deliverables

- [ ] `services/common/rhi_common/spike_keys.py` holds the spikes' students' keys only, writes the secret as an object with students alone, and reads a secret of that shape or of the shape with a spent key, which it ignores; any other shape is refused as before.
- [ ] `admin/rhi_admin/spikes.py` puts no spent student on the roster and writes no spent key, and the help and messages of `admin/rhi_admin/main.py` no longer speak of a student with no budget.
- [ ] `spikes/s2_client_hold.py` sends no request as a spent student and records nothing of a 402, `docs/measurements/s2.md` no longer has the 402's rows or the sentences that introduce them, and `spikes/common.py`, `spikes/s3_harness_task.py` and `spikes/s3_second_session.py` no longer name a spent key.
- [ ] `services/common/rhi_common/causes.py` has no budget_reached and no sentence on 402, and its count of causes follows; `docs/student-guide.md`, `docs/runbooks/secret-rotation.md` and `client/rhi_cli/wait.py` no longer name it.
- [ ] `tests/unit/admin/test_spike_keys.py`, `tests/unit/spikes/test_s2.py`, `tests/unit/spikes/test_common.py` and `tests/unit/gateway/test_errors.py` follow, and cover both shapes of the secret.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin/test_spike_keys.py tests/unit/spikes/test_common.py` passes: the admin's command puts only the students on the roster and writes a secret with no spent key; a secret of the new shape and one of the old shape both read to the students' keys; and a secret with no students, or with a key that is not valid, is refused without repeating it.
- [ ] `uv run --locked pytest tests/unit/spikes tests/unit/gateway/test_errors.py tests/unit/client tests/unit/test_docs.py tests/unit/test_runbooks.py` passes.
- [ ] `grep -rnE "budget_reached|BUDGET_REACHED|spike-spent|SPENT_ALIAS" services admin client spikes docs/student-guide.md docs/runbooks docs/measurements/s2.md` prints nothing.
- [ ] The owner runs the admin CLI's spike-keys command, which rewrites the secret rhi/spike-keys in the new shape, and removes the spent student's entry with roster remove, so that a later change can drop the old shape (needs aws:account).

## References

- `docs/design/system-design.md`, sections 6.2, 12 and 14
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decisions 2, 3 and 8
- `services/common/rhi_common/spike_keys.py`
- `docs/measurements/s2.md`
