+++
id = "E4-47"
title = "Remove what nothing sets or reads: session windows, the pools' GPU hours, the mode budget-latched and its cause"
labels = ["size:M", "kind:app"]
epic = 6
+++

## Context

Phases 0 and 1 were built with a per-student token budget, GPU-hour ceilings and a budget latch;
the design now has none of them, and the code they left is removed once phases 0 and 1 are
deployed, which they are (section 14; ADR 0003, decisions 2 and 3). This issue removes the part
that nothing sets or reads any more. The gateway's reservation and settlement, the cause
budget_reached and the per-student budget fields are later issues (E4-48 to E4-50).

- **session_windows**, a key of the configuration (`src/rhi/config.py`) that must be an empty list
  and that nothing reads: there is no session schedule, the instructor switches sessions on and off
  (section 5.6). `time_zone`, `nightly_stop`, `course_end` and `monthly_budget_usd` stay: the
  nightly scheduled action reads the first two, and the AWS Budgets alerts measure the last.
- **gpu_hours_per_day**, a field of a pool's entry (`services/common/rhi_common/pool_config.py`),
  required of a pool served by vLLM and read by nothing; with it goes the docstring's sentence that
  says the scaler accrues what ran (it never did). A pool's `prices` stay: the price-limit check and
  spike S5 use them.
- **the mode budget-latched** (`services/common/rhi_common/records.py`), which nothing writes: the
  admin CLI sets only disabled and the configured mode. The scaler stops only a disabled pool
  (`services/scaler/rhi_scaler/decide.py`), and the hold's first rule ends a request for a disabled
  pool only (`services/gateway/rhi_gateway/hold.py`).
- **the cause pool_ceiling_reached** (`services/common/rhi_common/causes.py`), answered 402 only for
  a pool in that mode, by the hold and by the status and wake of `services/gateway/rhi_gateway/local.py`.

**Two deployed values the loaders would refuse.** Each loader refuses what it does not know, and
the deployed configuration still holds both removed values: the parameter session_windows under the
configuration's prefix in SSM (the configuration's writer deletes no parameter), and
gpu_hours_per_day in the coursework pool's entry. So, until a later change removes the tolerance
once the owner has rewritten both, the configuration loader still accepts session_windows as an
empty list and reads it into nothing, and the pool configuration's parser still accepts
gpu_hours_per_day as a whole number not below 0 and reads it into nothing. Any other value of either
is refused as before.

## Deliverables

- [ ] `src/rhi/config.py` has no session_windows in its configuration, and a source that holds the key session_windows as an empty list loads as if it did not, while a source that holds any other value for it is refused.
- [ ] `config.example.toml`, `setup/owner-setup.sh` and `docs/runbooks/owner-setup.md` no longer give, write or explain session_windows, and `config.example.toml` no longer gives gpu_hours_per_day.
- [ ] `services/common/rhi_common/pool_config.py` has no gpu_hours_per_day in a pool, accepts the field in an entry as a whole number not below 0 and reads it into nothing, and its docstring says neither that a vLLM pool needs it nor that the scaler accrues what ran.
- [ ] `services/common/rhi_common/records.py` has no mode budget-latched, `services/scaler/rhi_scaler/decide.py` stops the disabled mode alone, `services/gateway/rhi_gateway/hold.py` ends a request for a disabled pool alone in its first rule, and `services/scaler/rhi_scaler/model.py` names neither the latch nor the ceilings.
- [ ] `services/common/rhi_common/causes.py` has no pool_ceiling_reached and its docstring's count of causes follows; `services/gateway/rhi_gateway/local.py`, `services/gateway/rhi_gateway/app.py`, `docs/student-guide.md` and `docs/runbooks/secret-rotation.md` no longer name it.
- [ ] `tests/unit/test_config.py`, `tests/unit/test_config_ssm.py`, `tests/unit/test_owner_setup.py`, `tests/unit/common/test_pool_config.py`, `tests/unit/common/test_records.py`, `tests/unit/scaler/test_decide.py`, `tests/unit/scaler/test_scale_out.py`, `tests/unit/gateway/test_hold.py`, `tests/unit/gateway/test_local.py`, `tests/unit/gateway/test_wake.py`, `tests/unit/gateway/test_errors.py` and `tests/examiner/test_issue69_hold_probe.py` follow, the two tolerances included.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_config.py tests/unit/test_config_ssm.py tests/unit/test_owner_setup.py` passes: a configuration without session_windows loads, one from SSM that holds the parameter session_windows as an empty list loads, one that holds a window in it is refused, and the owner's setup writes no session_windows.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a vLLM pool's entry without gpu_hours_per_day loads, one with gpu_hours_per_day 16 loads to the same pool, and one with a negative or a fractional value is refused.
- [ ] `uv run --locked pytest tests/unit/common tests/unit/scaler tests/unit/gateway tests/examiner tests/unit/test_docs.py tests/unit/test_runbooks.py` passes.
- [ ] `uv run --locked pytest tests/unit/test_copy_weights.py tests/unit/engine/test_fetch_model.py tests/unit/pools/test_vllm_command.py tests/unit/dashboards/test_build.py tests/synth/test_pool_release.py tests/unit/gateway/test_settings.py` passes with their entries unchanged.
- [ ] `grep -rnE "BUDGET_LATCHED|POOL_CEILING_REACHED|budget-latched|pool_ceiling_reached" services admin client src docs/student-guide.md docs/runbooks` prints nothing.
- [ ] The owner rewrites the coursework pool's entry from the new example with the configuration step of `setup/owner-setup.sh`, and deletes the parameter session_windows from the configuration's prefix in SSM, so that a later change can drop the two tolerances (needs aws:account).

## References

- `docs/design/system-design.md`, sections 5.6, 11.3 and 14
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decisions 2 and 3
- `services/common/rhi_common/pool_config.py`
- `src/rhi/config.py`
