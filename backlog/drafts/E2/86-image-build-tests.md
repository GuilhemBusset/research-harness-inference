+++
id = "E2-86"
title = "Build the gateway's and the mock engine's images in CI, and run each once"
labels = ["size:S", "kind:test"]
epic = "E2"
blocked_by = ["E0-17", "E2-76", "E2-82"]
+++

## Context

The gateway's image (issue E2-76) and the mock engine's (issue E2-82) are otherwise first built by
the pipeline's asset step, after the merge: a Dockerfile that does not build, or an image that lacks
a library its code imports, would only show as a failed deployment. A test that starts a container
lives under `tests/container/` and runs in CI's job of its own, part of the required check; the
local gate does not run it, because the coding agents' host has no container runtime (section 8.3;
ADR 0001, amendment 5). The step is issue E0-28, and the CI job that runs it is issue E0-17. What an agent can show here is that
the tests are collected; that they pass is shown by the CI job.

Each image is built from its Dockerfile with the build context issue E1-16 fixes, for the runner's
own architecture: the mock engine's base image is published for arm64 and x86_64 (issue E2-82). The
gateway's image needs AWS and its settings to serve, so its test imports the entry point inside the
image, which loads every library the gateway uses, and stops there. The mock engine needs nothing
outside itself but its secret in RHI_POOL_SECRET (issue E2-31), so its test starts it with one.

## Deliverables

- [ ] `tests/container/test_gateway_build.py` builds the gateway's image from its Dockerfile and build context, and runs it once to import the gateway's entry point, which succeeds.
- [ ] `tests/container/test_mock_engine_build.py` builds the mock engine's image for the runner's architecture, starts it with a short start delay and a secret in RHI_POOL_SECRET, and finds its health route answering 200 after the delay and an inference route refusing a request without the secret.
- [ ] Both tests stop and remove what they started, whatever their result.

## Acceptance criteria

- [ ] `uv run --locked pytest --collect-only -q tests/container/test_gateway_build.py tests/container/test_mock_engine_build.py` exits 0 and lists the tests' cases, on a machine with no container runtime.
- [ ] `scripts/check.sh container-tests` passes in the CI job container-tests on the head that is merged, with no test of `tests/container/test_gateway_build.py` or `tests/container/test_mock_engine_build.py` skipped (needs host:container-runtime).

## References

- `docs/design/system-design.md`, section 8.3
- `docs/adr/0001-software-factory.md`, amendment 5
