+++
id = "E6-21"
title = "Configuration and shared records: retire what served scaling by load"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-13", "E6-16", "E6-19", "E6-20", "E6-22", "E6-27"]
+++

## Context

By now nothing reads the configuration fields and the shared records that served scaling by load:
the wake, the hold, the idle time, the warm pool, the pool modes, the session switch, the gateway's
demand heartbeat, the owner entries served outside a session. This issue removes the records and
retires the fields (system design, sections 4.2, 5.6 and 17).

**Retired, not removed.** The deployed configuration still holds these fields, and the loader fails
closed on what it does not know. A retired field is accepted, held to its type as before, and read
into nothing: the typed configuration no longer has it. Both loaders already do this for one field
each (`RETIRED_FIELDS` in `rhi_common.pool_config`, `RETIRED_KEYS` in `rhi.config`). The tolerance
is removed by a later issue, once the owner has taken the fields out of the deployed configuration.

| Where | Retired |
| --- | --- |
| A pool's entry | `wake`, `warm_instances`, `hold_seconds`, `hold_seconds_non_streaming`, `idle_minutes`, `start_delay_seconds` |
| The configuration's keys | `on_demand_price_limit`, `keepalive_seconds` |

With `wake` and `on_demand_price_limit` goes the rule that refused a pool priced above the limit
unless it woke only on schedule: no request wakes a pool, and only the instructor books one. With
`start_delay_seconds` goes the mock engine's start delay, which only spike S2 used.

**Removed from the shared records:** the pool mode and its record, the session switch and its item,
the gateway's heartbeat, and the rule that told the owner's own entries apart. Items of those kinds
left in the tables are read by nothing and deleted by nothing; heartbeat rows expire by themselves.
The deployment hold loses its read of the switch and waits on sessions alone.

After this issue the deployed system behaves as before it. The mock pool's release changes, since
the mock loses a flag.

## Deliverables

- [ ] `services/common/rhi_common/records.py`, `services/common/rhi_common/tables.py` and `services/common/rhi_common/students.py` no longer define the pool mode and its record, the session switch and its key, the gateway's heartbeat, or the rule for the owner's own entries.
- [ ] `scripts/pipeline/deploy_hold.py` reads no switch: it waits while any pool's session is active, and passes otherwise.
- [ ] `services/common/rhi_common/pool_config.py` holds the six fields of a pool's entry that Context lists as retired: an entry that gives them still loads, the typed pool has none of them, and no rule compares a pool's prices with a limit, there or in the callers that gave one: `services/gateway/rhi_gateway/settings.py`, `services/scaler/rhi_scaler/settings.py` and `admin/rhi_admin/pools.py`.
- [ ] `src/rhi/config.py` holds the two keys that Context lists as retired, and `config.example.toml` gives none of the eight, in a key, in a pool's entry or in a comment.
- [ ] `services/engine/mock/rhi_mock_engine/app.py` has no start delay, and `src/rhi/pools/release.py` gives the mock engine no flag for one.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common tests/unit/test_config.py tests/unit/test_deploy_hold.py tests/unit/engine` passes: an entry and a configuration that still give every retired field load, and one that gives a retired field a value of another type is refused by the field's name.
- [ ] `git grep -n -e hold_seconds -e idle_minutes -e warm_instances -e start_delay -e keepalive_seconds -e on_demand_price_limit -e SessionSwitch -e GatewayHeartbeat -e is_owner_entry -- services src admin client spikes scripts dashboards ":!services/common/rhi_common/pool_config.py" ":!src/rhi/config.py"` prints nothing.
- [ ] The owner removes the eight retired fields of Context, and `nightly_stop`, from the deployed configuration, so that a later issue can refuse them (needs aws:account).

## References

- `docs/design/system-design.md`, sections 4.2, 5.6 and 17
- `services/common/rhi_common/pool_config.py`
- `services/common/rhi_common/records.py`
- `src/rhi/config.py`
- `config.example.toml`
