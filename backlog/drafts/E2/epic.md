+++
id = "E2"
kind = "epic"
title = "Phase 1: a student key works end to end against the mock pool"
labels = ["kind:infra"]
+++

Phase 1 of the rollout (`docs/design/system-design.md`, section 14): the network, the gateway
(routes, validation, reserve-and-settle, hold), the key issuer and the CLI, the scaler, the mock
pool with its lifecycle, Prometheus, Grafana behind sign-in, state protection, and spikes S9 and
S10. This phase turns on the idle floor of section 11.2 and needs no GPU quota.

Exit criteria, from the design: a student key works end to end against the mock pool; the scaler's
scenarios and the forbidden-route tests pass in the pipeline's verification. Both are obligations on
the verification issue E2-64.

Spike S10 has a part that starts a container, the Grafana sign-in test. It lives under
`tests/container/` and runs in CI only (section 8.3): issue E2-65, after the CI job of issue E0-17.

The pure parts come first and depend on nothing but the source layout: the key format, the error
vocabulary, the records, the scaler's decision function, the body filters and the usage parsers. The
constructs follow, and the pipeline's verification closes the phase.
