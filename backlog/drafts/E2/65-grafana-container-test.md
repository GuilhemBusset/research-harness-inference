+++
id = "E2-65"
title = "Start the Grafana image and assert that it refuses everything but the load balancer's identity"
labels = ["size:S", "kind:test"]
epic = "E2"
blocked_by = ["E0-17", "E2-59"]
+++

## Context

Grafana trusts the identity header the load balancer sets, and has its login form and its default
admin account disabled. The design does not take that on the word of a configuration file: a CI
test starts the pinned image and asserts that a request without the header is refused and that the
default account does not exist (section 7.4). It is the CI half of spike S10; the configuration
assertions that need no container are issue E2-59, and the deployed check is on issue E2-60. Grafana
is served under the path prefix /grafana/ (issue E2-59).

A test that starts a container lives under `tests/container/` and runs in CI's job of its own, part
of the required check; the local gate does not run it, because the coding agents' host has no
container runtime (section 8.3; ADR 0001, amendment 5). The step is issue E0-28, and the CI job that runs it is issue E0-17. What
an agent can show here is that the test is collected; that it passes is shown by the CI job.

## Deliverables

- [ ] `tests/container/test_grafana_sign_in.py` builds the image of the Grafana Dockerfile of issue E2-59, starts it, and waits until it answers.
- [ ] It asserts that a request without the identity header is refused and reaches no dashboard and no data source, and that a request with the header is served as that user.
- [ ] It asserts that the default admin account does not exist: its documented name and password are refused on the login route and on the HTTP API.
- [ ] The test stops and removes the container it started, whatever its result.

## Acceptance criteria

- [ ] `uv run --locked pytest --collect-only -q tests/container/test_grafana_sign_in.py` exits 0 and lists the test's cases, on a machine with no container runtime.
- [ ] `uv run --locked pytest tests/unit/test_ci_workflow.py` still passes: the default test run collects nothing under `tests/container/`.
- [ ] `scripts/check.sh container-tests` passes in the CI job container-tests on the head that is merged, with no test of `tests/container/test_grafana_sign_in.py` skipped (needs host:container-runtime).

## References

- `docs/design/system-design.md`, sections 7.4, 8.3, 12
- `docs/adr/0001-software-factory.md`, amendment 5
- `docs/design/evidence.md`, E24
