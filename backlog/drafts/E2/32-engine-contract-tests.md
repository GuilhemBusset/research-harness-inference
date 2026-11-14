+++
id = "E2-32"
title = "Contract tests that any engine adapter must pass"
labels = ["size:M", "kind:test"]
epic = "E2"
blocked_by = ["E2-82"]
+++

## Context

The gateway, the scaler, the dashboards and the tests depend on the engine contract only (section
4.1). One suite states it, and each adapter is run against it: the mock now, vLLM in phase 2. The
design plans no other engine; SGLang is the candidate if vLLM disappoints, and would be one more
adapter and one more row of the per-engine metric table. Engine adapters differ; the suite does
not.

The contract's metrics row names at least: running and waiting requests; time to first token; time
per output token; input tokens, the cached part of them, and output tokens; cache use. Each engine
names them in its own way, and the per-engine table of issue E2-82 says how; the suite checks the
engine under test serves each name of its row.

Some rows of the contract are not properties an engine can be asked for, and are tested elsewhere.
Reachability is the internal load balancer's allow-list, tested through both hops (issues E2-44 and
E2-63). Identity is reported by the instance's lifecycle agent, in its heartbeat and as a metric,
because the engine does not know its image digest (issue E2-35). The engine settings the contract
fixes for vLLM are asserted by a test of its unit file, in phase 2.

## Deliverables

- [ ] `tests/contract/test_engine_contract.py` has one test per row of the contract of section 4.1 that an engine answers by itself: the four inference routes with streaming and a tool call, health, metrics on localhost, the bearer secret on the four routes, and shutdown.
- [ ] The metrics test finds, on the engine's metrics route, every name the per-engine table gives the engine under test, for every quantity of the contract's metrics row.
- [ ] `tests/contract/conftest.py` runs the suite against the mock engine in process, or against the engine whose address the environment variable RHI_ENGINE_URL gives and whose row of the per-engine table the variable RHI_ENGINE_NAME names; the shutdown test is collected only in process, where the suite controls the engine.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/contract/test_engine_contract.py` passes against the mock engine in process.
- [ ] `tests/contract/test_engine_contract.py::test_the_suite_catches_a_broken_engine` runs the suite against the mock with its secret check turned off, with its usage removed, and with one metric of its row missing, and fails each time.

## References

- `docs/design/system-design.md`, sections 4.1, 4.6
