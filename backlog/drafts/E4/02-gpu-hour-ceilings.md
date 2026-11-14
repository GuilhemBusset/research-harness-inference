+++
id = "E4-02"
title = "Scaler: refuse to wake a pool past its daily or monthly GPU-hour ceiling"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E3-10", "E4-01"]
+++

## Context

The GPU-hour ceilings are the real total cap (section 11.3). Token budgets alone do not bound GPU
time: one small request every 19 minutes would keep a coursework instance up all month for about
$2,400. The scaler refuses to wake a pool past its daily or monthly ceiling, in-band, and scales in
as its instances fall idle (section 5.1, the stop rule). A pre-warm is a wake: a pool at a ceiling is
not pre-warmed either (issue E4-05).

The ceilings' values are the owner's decision 6. The defaults for the coursework pool are 16
GPU-hours a day and 150 a month (section 17). Sixteen cover two instances through the four-hour
first session, which uses 10.7 (section 11.3). A GPU-hour is one instance for an hour, whatever its
number of GPUs, accrued by issue E4-01.

**The monthly ceiling is a pool field with a default, never a required one.** The configuration in
SSM already holds the owner's pools, written before this issue, and the loader refuses a pool that
lacks a required field: a required field would fail the pipeline's synth step on the first merge
after this one. So the field gpu_hours_per_month is optional. A pool whose engine is not mock and
that leaves it out gets 150, the value of section 17. A mock pool that leaves it out has no monthly
ceiling, as it has no daily one unless it gives gpu_hours_per_day (issue E2-04): the mock pool is
woken by the pipeline's verification on every deployment, and a ceiling there would turn a busy
week of merges into failed verifications. The value reaches the scaler with the rest of the pool's
configuration (issue E2-12); nothing else reads it.

A request for a pool at its ceiling is answered 402 with the cause `pool_ceiling_reached` (section
6.2, step 8). The gateway learns it from the note the scaler writes for each pool on every run
(issues E2-03 and E2-10); the gateway's side is issue E4-32.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gains gpu_hours_per_month, a pool's monthly GPU-hour ceiling: optional; 150 when a pool whose engine is not mock leaves it out; no monthly ceiling when a mock pool leaves it out; `tests/unit/common/test_pool_config.py` covers the three cases.
- [ ] `config.example.toml` gives the coursework pool the ceilings of section 17: gpu_hours_per_day 16 and gpu_hours_per_month 150.
- [ ] `services/scaler/rhi_scaler/decide.py` wakes no pool whose accrued GPU-hours of the day or of the month have reached its ceiling, lets its instances go as they fall idle, and gives the reason ceiling-reached; a pool without a ceiling is never stopped by one.
- [ ] `services/common/rhi_common/records.py` adds to the scaler's note for a pool whether it is at a GPU-hour ceiling, a note stored without it reading as not at a ceiling, and `services/scaler/rhi_scaler/aws_io.py` writes it on every run.
- [ ] `tests/unit/scaler/test_ceilings.py` covers both ceilings and the note.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_ceilings.py` passes: at 16 accrued hours in a day a pool with gpu_hours_per_day 16 is not woken by held requests, a busy instance is kept until it is idle, the note says the pool is at its ceiling, and the next day wakes it again.
- [ ] `tests/unit/scaler/test_ceilings.py::test_the_month_holds_across_days` shows a pool at 150 accrued hours in the month not woken on a fresh day, and woken again on the first of the next month.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/test_config.py` passes: the example's coursework pool loads with 16 and 150, the same pool without gpu_hours_per_month loads with 150, and the mock pool loads with no ceiling.
- [ ] The owner sets each pool's daily and monthly GPU-hour ceilings, part of decision 6 of the design, and writes them to the configuration in SSM (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 5.1, 6.2, 11.3, 15, 17
