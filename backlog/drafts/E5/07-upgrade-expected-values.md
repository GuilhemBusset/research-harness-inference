+++
id = "E5-07"
title = "The upgrade pool's expected values on the inference performance dashboard"
labels = ["size:S", "kind:app"]
epic = "E5"
blocked_by = ["E2-68", "E4-12", "E5-03"]
+++

## Context

Each pool's configuration carries its expected values for speed per stream, time to first token and
cache capacity, and the inference performance dashboard shows them beside the live series, one row
per pool, so that a pool serving less than predicted is visible at a glance (sections 4.7, 7.2 and
7.3). The fields come from issue E2-68, each quantity as one low and one high bound, and the
dashboard from issue E4-12. For the upgrade pool section 4.7 gives
60 to 100 tokens per second for one stream alone, 40 to 60 at 8 streams and 25 to 40 at 32, and a
first token under 1 second for a cached turn. It gives its cache capacity only as "several million
tokens", which is not a number, so this issue gives none; spike S6 measures it (issue E5-05). One
bound per quantity cannot carry two stream counts, so the speed bounds span the estimate from 8 to
32 streams: 25 to 60 tokens per second.

## Deliverables

- [ ] `config.example.toml` gives the upgrade pool, in the expected-values field of issue E2-68, an output speed per stream of 25 to 60 tokens per second and a time to first token of 0 to 1 second, and no cache capacity.
- [ ] `tests/unit/dashboards/test_upgrade_row.py` builds the inference performance dashboard from `config.example.toml` and reads the upgrade pool's row.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_upgrade_row.py` passes: the dashboard has a row for the upgrade pool, and its panels for speed per stream and for time to first token carry reference lines at 25 and 60 tokens per second and at 1 second.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/test_config.py` passes with the pools of `config.example.toml`.

## References

- `docs/design/system-design.md`, sections 4.7, 7.2, 7.3
