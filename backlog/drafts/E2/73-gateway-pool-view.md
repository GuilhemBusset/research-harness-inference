+++
id = "E2-73"
title = "Gateway: what each task knows of each pool, read every 5 seconds, and the wakes it asks for"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-03", "E2-04"]
+++

## Context

The gateway learns that a pool is ready from the target group's health, which the internal load
balancer establishes by checking each engine directly. Each gateway task reads it through the AWS
API every 5 seconds for every pool, whether or not it is holding a request, and decides from that
cached value whether a request is forwarded or held. It never probes through the load balancer,
which forwards the four inference routes and nothing else (section 5.3).

The hold and the status route need more than health: the pool's mode, which the scaler and the
gateway both read from one record per pool (section 11.3), and the scaler's note on the pool,
whether it has asked for capacity and when a launch last found none (issue E2-03). The gateway reads
both on the same cycle as the health, so that one request never costs a read.

The module that asks the scaler for a wake also keeps the explicit wakes that are pending. A wake a
student asked for with `POST /wake` counts as demand for 10 minutes or until the pool is ready,
whichever comes first (sections 5.1 and 5.3). The hold (issue E2-26), the local routes (issue E2-27)
and the demand report (issue E2-28) all read this module, so that none of them waits for another.

The modules are given each pool's target group, the pool-state table and the scaler function's name;
issue E2-76 reads them from the gateway's environment. They call AWS with the SDK that issue E1-16
pins for the services; the tests replace it with doubles.

## Deliverables

- [ ] `services/gateway/rhi_gateway/readiness.py` reads the health of every pool's target group through the AWS API every 5 seconds, whether or not a request is held, and keeps the last value: a pool is ready when at least one target is healthy, and not ready when the read fails.
- [ ] On the same cycle it reads each pool's state record and the scaler's note of issue E2-03 and keeps the last values read; a failed read keeps the values of the previous one.
- [ ] Callers read only the kept values: no request causes a read of the AWS API or of the tables, and the gateway sends no request of its own through the internal load balancer.
- [ ] `services/gateway/rhi_gateway/waker.py` asks the scaler to run for a pool, without waiting for its answer.
- [ ] The same module keeps the explicit wakes that are pending: one recorded for a pool stays pending until that pool is ready or 10 minutes have passed, whichever comes first, and the module says, per pool, how many are pending and when the last one ended with its pool ready.
- [ ] Test doubles stand in for the AWS API, the tables and the scaler; `tests/unit/gateway/test_readiness.py` covers both modules.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_readiness.py` passes: a pool with one healthy target reads ready, one with none reads not ready, and a failed health read reads not ready.
- [ ] `tests/unit/gateway/test_readiness.py::test_read_every_5_seconds_for_every_pool` counts one read of the health, of the state record and of the note per pool every 5 seconds with no caller at all, and the same number while a hundred callers ask for the kept values.
- [ ] `tests/unit/gateway/test_readiness.py::test_an_explicit_wake_is_pending_for_10_minutes_or_until_ready` records an explicit wake for a cold pool and finds it ended at the moment the target group turns healthy, and, for a pool that never turns healthy, ended at 10 minutes.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 11.3
