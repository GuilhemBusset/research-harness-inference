+++
id = "E2-68"
title = "A pool's prices per instance type, the on-demand price limit, and its expected values"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-04"]
+++

## Context

A pool may list fallback instance types of the same GPU, the next sizes of its family; they cost
more, so the configuration then gives a price for each type and the scaler accrues what actually ran
(sections 5.4 and 7.2). Configuration refuses `wake = "on-demand"` for a pool above an hourly price,
$5 by default (sections 11.3 and 17). A fallback type is a price the pool can run at, so every listed
type is held to the limit.

Each pool's configuration carries its expected values for speed per stream, time to first token and
cache capacity, which the inference dashboard draws beside the measured series (sections 4.7 and
7.3; issues E4-12 and E4-29). Section 4.7 gives them as ranges: for the coursework pool, 15 to 22
tokens per second per stream at eight agent streams, 0.3 to 1.5 seconds to the first token of a turn
whose transcript is cached, and 1.1 to 1.8 million tokens of cache. A pool without an estimate (the
mock) leaves the field out; a pool may give some quantities and not others (the upgrade pool gives no
cache capacity, issue E5-07).

The shape:

```toml
[pools.coursework]
prices = { "g7e.2xlarge" = 3.36 }
expected = { stream_tokens_per_second = [15, 22], time_to_first_token_seconds = [0.3, 1.5], cache_tokens = [1100000, 1800000] }
```

`prices` is required of every pool: the deployed pools table is still empty when this issue lands,
and the first pool reaches SSM with issue E2-71 (issue E2-04 says why that matters). The limit itself
is a top-level key read by `src/rhi/config.py` in issue E2-69; this module takes it as an argument,
5 dollars an hour when none is given.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives each pool prices, a price in dollars an hour for each of its instance types, the first listed type being the one it runs and the others its fallbacks, and refuses, naming the pool and the field, a listed type without a price, a price for a type the pool does not list, and a price below zero.
- [ ] It refuses a pool with wake on-demand when any of its instance types is priced above the on-demand price limit it is given, 5 dollars an hour when it is given none, and accepts the same pool with wake scheduled-only.
- [ ] A pool may give expected, whose keys stream_tokens_per_second, time_to_first_token_seconds and cache_tokens are each optional and each a low and a high bound; a low bound of zero is accepted, and a bound below zero, a high bound of zero, a low bound above its high bound, or another key is refused.
- [ ] `tests/unit/common/test_pool_config.py` covers the prices, the limit and the expected values.

## Acceptance criteria

- [ ] `tests/unit/common/test_pool_config.py::test_price_limit` shows, with no limit given, the coursework entry accepted as on-demand at 3.36 dollars an hour, refused as on-demand and accepted as scheduled-only at 16.57, and refused as on-demand when a fallback type is priced at 6.
- [ ] `tests/unit/common/test_pool_config.py::test_expected_values` shows the coursework pool's expected values of section 4.7 parsed as 15 to 22, 0.3 to 1.5 and 1,100,000 to 1,800,000, a pool without them accepted, a time to first token of 0 to 1 accepted, and a low bound above its high bound refused.
- [ ] `tests/unit/common/test_pool_config.py::test_prices` shows a type without a price and a price for an unlisted type each refused with the pool's name and the field prices.

## References

- `docs/design/system-design.md`, sections 4.7, 5.4, 7.2, 7.3, 11.3, 17
