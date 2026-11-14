+++
id = "E4-01"
title = "Scaler: accrue the GPU time every pool uses, priced by the instance type that ran"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-12", "E2-68"]
+++

## Context

The scaler accrues every pool's GPU-seconds on every run, for every running instance of the group,
including one being prepared for the warm pool (section 5.1, the spend input). The GPU-hour ceilings
of section 11.3 read that accrual, and the cost panels are computed from it: accrued instance-seconds
times the configured hourly price, labelled an estimate (section 7.2).

A GPU-hour here is one instance running for an hour, whatever its number of GPUs (section 11.3): an
instance of the upgrade pool's four-GPU type running for an hour is one GPU-hour, not four.

A pool may list fallback instance types with the same GPU, the next sizes of its family. They cost
more, so the configuration gives an hourly price for each type, fallbacks included, in the pool's
`prices` field (issue E2-68), and the scaler accrues what actually ran (section 5.4). The
coursework pool lists one type, `g7e.2xlarge`, at $3.36 an hour (section 4.2); the mock pool runs on
`t4g.small` at $0.02.

"Today" and "this month" are read in the schedule's time zone, a key of the configuration since
issue E1-03 and the one the nightly stop of the pools' groups uses (section 17), so that an evening
session is not split across two days. The deployed function receives every pool's configuration as
the stage loaded it, prices included, in its settings (issues E2-11 and E2-12), but not yet the time
zone: this issue adds it to the settings, so that the accrual uses the configured zone in the account
and not only in the tests. Settings without it read as UTC, so that an older construct's settings
still read (issue E2-11).

## Deliverables

- [ ] `services/scaler/rhi_scaler/aws_io.py` reads, for every running instance of a pool's group in any lifecycle state, warm pool preparation included, its instance type.
- [ ] `services/scaler/rhi_scaler/spend.py` adds, on every run and for each instance running now, the seconds since the previous run to the pool's GPU-seconds of the day and of the month, and the same seconds times that instance type's hourly price from the pool's prices to the pool's estimated dollars of the day and of the month, and keeps them in the scaler's memory of earlier runs.
- [ ] A run that comes late accrues the whole gap; an instance counts once whatever its number of GPUs; the day's totals restart at midnight in the configured time zone and the month's on the first of the month; `tests/unit/scaler/test_spend.py` covers this on a clock the test controls.
- [ ] `services/scaler/rhi_scaler/metrics.py` sends, per pool, the GPU-hours of the day and of the month and their estimated dollars, with no student label.
- [ ] `src/rhi/pools/scaler.py` adds the schedule's time zone of the configuration to the function's settings, and `services/scaler/rhi_scaler/settings.py` reads it, UTC when the settings give none; `tests/synth/test_scaler.py` and `tests/unit/scaler/test_settings.py` check both.
- [ ] Every cdk-nag finding `src/rhi/pools/scaler.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_scaler.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_spend.py` passes: two instances over three one-minute runs accrue 360 seconds, a run five minutes late accrues the five minutes, an instance being prepared for the warm pool accrues like one in service, and the day's total restarts at midnight in the configured time zone while the month's does not.
- [ ] `tests/unit/scaler/test_spend.py::test_priced_by_the_type_that_ran` shows an hour of a fallback type priced at that type's price and not the first type's, and an hour of an instance with four GPUs accrued as one GPU-hour.
- [ ] `uv run --locked pytest tests/synth/test_scaler.py tests/unit/scaler/test_settings.py` passes: the scaler function's settings carry the example configuration's time zone, and settings without one read as UTC.

## References

- `docs/design/system-design.md`, sections 4.2, 5.1, 5.4, 7.2, 11.3, 17
