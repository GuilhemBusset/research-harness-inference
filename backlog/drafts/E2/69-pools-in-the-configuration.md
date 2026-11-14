+++
id = "E2-69"
title = "The configuration validates its pools, names a default pool, and its example holds the mock pool"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-17", "E2-68"]
+++

## Context

Issue E1-03's loader refuses any pool entry until pools have a shape, and issue E1-17 reads the pools
table from SSM as one parameter per entry; issues E2-04 and E2-68 parse a pool. This issue replaces
the loader's refusal with that parsing, and adds the three top-level keys the pools need:

- `allowed_instance_types`: section 11.3 lists "a list of allowed instance types" among the hard
  caps. Left out, it is the types the configured pools list, so that it never needs a value the
  deployed configuration lacks; an owner who writes it restricts what a later pool may use.
- `on_demand_price_limit`: $5 an hour by default (section 17).
- `default_pool`: the gateway maps `claude-*` model names to the default pool, which the runbook can
  switch (sections 5.4 and 6.2, step 4). Left out, it is the only configured pool when there is
  exactly one, and there is none when the pools table is empty; with two pools or more it must be
  given. The deployed configuration's pools table is empty until the owner adds the mock pool (issue
  E2-71), so none of the three keys needs a value there before then.

How the values reach what runs: the stage reads them at synthesis; the scaler receives the pools
with its settings (issue E2-12); the gateway receives them and the default pool with its own (issues
E2-30 and E2-76); an instance receives what it needs in its pool's release parameter (issue E2-42).

The mock pool is phase 1's only pool (section 4.2): the mock engine on `t4g.small` at $0.02 an
hour, woken on demand. Its largest output is not a number the design gives. Claude Code asks for up
to 32,000 output tokens on its main turns (evidence E15), and the body filter clamps to the pool's
largest output, so 32,000 leaves Claude Code's requests as they are sent; issue E3-10 gives the
coursework pool the same. Its context length is the coursework pool's, 262,144 tokens, so that the
mock exercises the same limits. It gives no expected values.

The example's mock pool is also the entry the owner writes to SSM (issue E2-71). Once it is there,
every later pool field must have a default (issue E2-04): a test keeps a copy of the entry as this
issue writes it and parses it on every run, so that a later field without a default fails in CI and
not in the pipeline.

## Deliverables

- [ ] `src/rhi/config.py` accepts pool entries in place of refusing them, validates them with the pool parser of issues E2-04 and E2-68 when it loads, and reads three optional keys: allowed_instance_types, by default the instance types the configured pools list; on_demand_price_limit, 5 dollars an hour by default; and default_pool, by default the only configured pool when there is one, none when there is none.
- [ ] It refuses, naming the key: an instance type of a pool outside allowed_instance_types, a default_pool that names no configured pool, and two or more pools with no default_pool.
- [ ] `config.example.toml` gives allowed_instance_types t4g.small, an on_demand_price_limit of 5, the default pool mock, and the pool named mock: the mock engine on t4g.small priced at 0.02 dollars an hour, wake on-demand, max_instances 2, warm_instances 1, target_concurrency 8, a context length of 262,144 tokens and a largest output of 32,000 tokens, with no expected values and no measured start.
- [ ] `tests/unit/test_config.py` covers the three keys and their defaults, its test_only_the_owners_keys_are_required accepts a valid pool entry and still refuses a session window, and `tests/unit/common/test_pool_config.py` keeps a copy of the mock pool's entry as `config.example.toml` gives it here.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_config.py` passes with the pools of `config.example.toml` validated, a configuration whose only pool is mock and that gives none of the three keys loading with the default pool mock, the allowed types t4g.small and a limit of 5, and a default pool that names no configured pool refused.
- [ ] `tests/unit/test_config.py::test_an_empty_pools_table_needs_no_new_key` loads a configuration with an empty pools table and none of the three keys.
- [ ] `tests/unit/common/test_pool_config.py::test_the_first_deployed_pool_still_parses` parses the kept copy of the mock pool's entry and fails when a later pool field without a default makes it invalid.

## References

- `docs/design/system-design.md`, sections 4.2, 5.4, 6.2, 11.3, 17
- `docs/design/evidence.md`, E15
