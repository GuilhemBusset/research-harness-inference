+++
id = "E1-16"
title = "Service dependencies and packaging"
labels = ["size:M", "kind:infra"]
epic = "E1"
blocked_by = ["E1-01", "E1-02"]
+++

## Context

The services import third-party libraries, and the pipeline deploys them as Lambda functions (the
scaler, the key issuer, the commit-status reporter) and as container images (the gateway, the mock
engine). Two things must be settled once, before any service is written, or each service's issue
would settle them its own way:

- **Which libraries, pinned where.** One uv project holds everything (section 8.1), so the libraries
  are pinned in dependency groups of the root project, one group per service, at exact versions in
  `uv.lock`. A group lists every library its service imports, through `rhi_common` included. The dev
  group includes every service group, so that the tests and ty see them all. The CDK libraries stay
  in the project's own dependencies (issue E1-01), so no service's group carries them.
- **How a function or an image gets `rhi_common` and its libraries.** CDK's usual Python bundling
  runs Docker at synthesis, and neither the agents' host nor `scripts/check.sh synth` has a container
  runtime (section 8.3). A function deployed with no bundling fails on its first import.

**The libraries, and why each.** Each at an exact version: the newest release at least seven days
old when this issue is built (the cooldown of section 9), all resolved together in one lock.

| Group | Libraries | Why |
| --- | --- | --- |
| gateway | Starlette, httpx, uvicorn, prometheus-client, the AWS SDK | Starlette and httpx are the design's (section 6.2). uvicorn is the ASGI server Starlette documents, used without its optional native extras, which this load does not need. prometheus-client serves the gateway's metrics on localhost for the collector beside it (section 7.1) |
| mock-engine | Starlette, uvicorn, prometheus-client | The mock serves the engine contract over HTTP and its metrics in Prometheus format (section 4.1) |
| scaler | the AWS SDK, protobuf, cramjam | The scaler writes its metrics to the Prometheus workspace by remote write (section 7.1): a protobuf message compressed with snappy's block format and signed with SigV4, which the AWS SDK's botocore signs. cramjam gives snappy as prebuilt wheels with no system library (python-snappy is now a thin wrapper around it). The two message types of remote write can be declared from a descriptor at import, so neither protoc nor generated code is needed |
| reporter | the AWS SDK, PyJWT, cryptography | A GitHub App signs an RS256 token with its private key (section 8.4); PyJWT does it with cryptography, which has native code and publishes wheels for both architectures |
| issuer | the AWS SDK | The issuer's tables (section 6.4); its calls to GitHub need nothing beyond the standard library |
| lifecycle | the AWS SDK, prometheus-client | The agent writes heartbeats, answers lifecycle hooks, reads the engine's metrics in Prometheus's text format and serves its own (section 4.3) |
| admin | the AWS SDK | The admin CLI works with the admin's own credentials (section 6.4) |
| cli | keyring | `rhi login` keeps the key in the operating system's keychain (section 6.4). Students install the CLI with `uv tool install` from its own `client/pyproject.toml` (issue E2-53), which declares the same requirement; this group lets the tests and ty see it |
| dev (added to) | moto, with its DynamoDB extra | An in-process DynamoDB that evaluates condition expressions, which botocore's Stubber cannot, for the gateway's key lookup and reservations (issues E2-14, E2-15) |

The AWS SDK is the one issue E1-17 pins for the app; one lock holds one version. A function carries
the locked SDK rather than the copy its runtime provides, so that the version the tests ran is the
version that runs. A library this issue does not pin is added by the issue that first needs it,
which names `pyproject.toml` and `uv.lock` in its Deliverables.

**A function's code.** `src/rhi/packaging.py` builds a bundle for a named service and an
architecture: at its root, the folders `rhi_common/` and the service's package (`rhi_scaler/`, for
instance), and the service group's libraries at their versions in `uv.lock`, installed by uv as
wheels only, for the Lambda runtime's Python and for Linux on the function's architecture (the
manylinux 2.28 platform of arm64 or x86_64; Lambda's Amazon Linux 2023 has a newer glibc). Wheels
only means nothing is compiled on the host, so the host's own architecture does not matter. The
runtime is a constant of the module: Python 3.13, the version of `.python-version`. Which
architecture a function runs on is chosen by the issue that deploys it. The bundle holds those two
packages and nothing else of the repository: a function that needs a list kept elsewhere in it,
such as the smoke test IDs or the lifecycle stages the reporter allows (issues E0-21 and E0-26),
gets it at synthesis, as an environment variable its construct sets.

The bundle is built through CDK's local bundling, so that CDK never starts a container, and so that
CDK skips it for a stack that the context aws:cdk:bundling-stacks excludes. The test fixtures of
issues E1-04 and E1-07 exclude every stack, so no test builds a bundle or needs the network.
`scripts/check.sh synth` and the pipeline's synth step bundle for real, downloading wheels from the
package index the way `uv sync` already does. The same layout could carry the lifecycle agent to an
instance (issue E2-42 decides); it is not part of this issue.

**An image's build context.** A Python service's image is built by the pipeline's asset step
(section 8.2, step 4), from a build context that holds, at its root, `rhi_common/`, the service's
package, the service's Dockerfile and a requirements file exported from `uv.lock` for the service's
group, with hashes, and nothing else of the repository. The Dockerfiles of issues E2-76 and E2-82
install that file with its hashes checked and copy the two packages. A change elsewhere in the
repository, or to another group in the lock, then leaves the image's asset hash as it was, so that
it starts no needless instance refresh (issue E2-42).

## Deliverables

- [ ] `pyproject.toml` and `uv.lock` pin, each at one exact version, in a dependency group named after the service that imports it, the libraries of the table in Context: groups gateway, mock-engine, scaler, reporter, issuer, lifecycle, admin and cli; and moto with its DynamoDB extra in the dev group.
- [ ] The dev group includes every service group, so that `uv sync --locked` gives the tests and ty every library, and `setup/README.md` lists in its installation inventory each group, what it holds, and that a function's bundle and an image install it from the lock.
- [ ] `src/rhi/packaging.py` gives a function's code for a named service and an architecture, arm64 or x86_64: a bundle holding the rhi_common package, the service's package and the libraries of the service's group at their locked versions, installed by uv as wheels only for the Lambda runtime's Python and Linux on that architecture, through CDK's local bundling so that no container is started and a stack excluded from bundling builds nothing.
- [ ] `src/rhi/packaging.py` gives a Python service's image a build context that holds, at its root, the rhi_common package, the service's package and Dockerfile, and a requirements file exported from `uv.lock` for the service's group with its hashes, and nothing else of the repository.
- [ ] `tests/synth/test_packaging.py` covers both, with no docker executable on the path and the step that installs wheels replaced by a stand-in that records its arguments.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_packaging.py` passes with no docker executable on the path: a function for arm64 and one for x86_64 each get a bundle that holds the two packages and nothing else of the repository, and the install step receives that architecture's platform, wheels only, and exactly the group's locked versions.
- [ ] `tests/synth/test_packaging.py::test_the_image_context_holds_the_service_only` builds the gateway's context, with a Dockerfile the test provides, and finds exactly the four entries stated, requirements equal to the gateway group's locked versions with their hashes, and an image asset hash that a change to a file outside them leaves unchanged.
- [ ] `tests/synth/test_packaging.py::test_skipped_bundling_installs_nothing` synthesizes a stack holding a function under a context that excludes every stack from bundling, and the install step is never called.
- [ ] `tests/synth/test_packaging.py::test_every_group_is_pinned` fails when a requirement of a service group or of the dev group is not pinned to one exact version, and imports every library of every service group.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 6.2, 6.4, 7.1, 8.1, 8.2, 8.3, 9
- `AGENTS.md`, section "Toolchain"
- `.python-version`
