+++
id = "E2-76"
title = "Gateway: the entry point, its settings from the environment, and its image"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-28", "E2-78", "E2-79"]
+++

## Context

The gateway's issues build its parts behind interfaces and test them with doubles. Something must
build the application with the real parts (the DynamoDB keystore and ledger, the readiness reader
and the waker, the heartbeat, the metrics) and start a server, and the deployment (issue E2-30) must
know exactly what that needs. This issue is that entry point, the reader of its settings, and the
image that runs it.

**Settings come from the environment, read once at start.** Every variable is named here, so that
the deployment fills them in and its test parses the task's environment with this reader:

| Variable | Holds | Default |
| --- | --- | --- |
| RHI_POOLS | The configuration's pools table as the stage loaded it, as JSON, parsed with the pool configuration of issue E2-04; it may be empty | required |
| RHI_DEFAULT_POOL | The default pool's name | absent when the configuration has no default pool |
| RHI_UPSTREAM_URL | The address of the internal load balancer's listener | required |
| RHI_TARGET_GROUPS | Each pool's target group, as JSON from pool name to ARN; empty with no pool | required |
| RHI_POOL_SECRETS | Each pool's secret, as JSON from pool name to the secret's ARN; empty with no pool | required |
| RHI_KEYS_TABLE, RHI_USAGE_TABLE, RHI_HEARTBEAT_TABLE, RHI_POOL_STATE_TABLE | The tables' names | required |
| RHI_SCALER_FUNCTION | The scaler function's name | required |
| RHI_BODY_LIMIT_BYTES | The largest body the filter reads | 8388608 |
| RHI_LONGEST_REQUEST_SECONDS | The longest request | 1800 |
| RHI_KEEPALIVE_SECONDS | The keep-alive interval of a hold | 10 |
| RHI_RETRY_AFTER_WARMING, RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE, RHI_RETRY_AFTER_CONCURRENCY_LIMIT | Retry-After of the three causes that carry one | 30, 60, 5 |

**The gateway starts with no pool.** The owner's configuration in SSM has an empty pools table until
the owner adds the mock pool (issue E2-71), and the loop does not wait for that: a gateway that
refused to start would keep its service from reaching a steady state, and every later deployment
would fail at that stack. So an empty pools table, with empty target groups and secrets, is valid,
and RHI_DEFAULT_POOL is optional, as the configuration's default_pool is (issue E2-69). With no
default pool, a model name that starts with claude- maps to no pool and is refused like any other
name of no pool, with the cause request_refused (issue E2-16).

The defaults are section 17's. Each numeric setting is also an optional key of the configuration,
at the same default, so that the owner can change it in SSM and no merge needs a value the deployed
configuration lacks: `request_body_limit_bytes`, `longest_request_seconds`, `keepalive_seconds`,
`retry_after_warming_seconds`, `retry_after_capacity_unavailable_seconds` and
`retry_after_concurrency_limit_seconds`. The task count `gateway_tasks`, two by default (sections 2
and 11.2), is a key of the same kind, which only the deployment reads. Issue E2-30 writes each key's
value into the matching variable.

The secret values are read at start through the Secrets Manager API, from the ARNs, with the task's
role; they are in no environment and no template. The task's ID, which names the gateway's heartbeat
row (issue E2-28), comes from the task metadata endpoint ECS gives the container, and is a random
identifier when there is none. The design names no port: the gateway listens on 8080, the port the
public load balancer's target group uses, and serves its metrics on localhost port 9091 (issue
E2-79). On SIGTERM it stops taking connections, finishes the requests in flight and writes its
heartbeat row of zeros (issue E2-28).

The server is uvicorn, pinned in the gateway group of issue E1-16. The image's build context is the
one `src/rhi/packaging.py` gives a Python service (issue E1-16): at its root the shared package, the
gateway's package, its Dockerfile, and the gateway group's requirements exported from `uv.lock` with
their hashes. Starting the image is tested in CI's container job (issue E2-86); this issue
needs no container.

## Deliverables

- [ ] `services/gateway/rhi_gateway/settings.py` reads the variables RHI_POOLS, RHI_UPSTREAM_URL, RHI_TARGET_GROUPS, RHI_POOL_SECRETS, RHI_KEYS_TABLE, RHI_USAGE_TABLE, RHI_HEARTBEAT_TABLE, RHI_POOL_STATE_TABLE and RHI_SCALER_FUNCTION, each required, and RHI_DEFAULT_POOL, optional; it parses the pools with the pool configuration of issue E2-04, accepts an empty pools table with empty target groups and secrets, and refuses a default pool that names no pool of the table.
- [ ] It reads RHI_BODY_LIMIT_BYTES, RHI_LONGEST_REQUEST_SECONDS, RHI_KEEPALIVE_SECONDS, RHI_RETRY_AFTER_WARMING, RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE and RHI_RETRY_AFTER_CONCURRENCY_LIMIT, which are 8388608, 1800, 10, 30, 60 and 5 when unset; a required variable that is missing, or any malformed one, stops the gateway with an error that names the variable and never its value.
- [ ] `src/rhi/config.py` gains seven optional keys: gateway_tasks 2, request_body_limit_bytes 8388608, longest_request_seconds 1800, keepalive_seconds 10, retry_after_warming_seconds 30, retry_after_capacity_unavailable_seconds 60 and retry_after_concurrency_limit_seconds 5; `config.example.toml` gives each at its default, and `tests/unit/test_config.py` holds them.
- [ ] `services/gateway/rhi_gateway/main.py` builds the application from the settings with the DynamoDB keystore and ledger, the readiness reader and waker of issue E2-73, the heartbeat and the metrics, reads each pool's secret at start, and serves the application on port 8080 and the metrics on 127.0.0.1, port 9091.
- [ ] On SIGTERM the entry point stops taking connections, finishes the requests in flight and has the heartbeat write its row of zeros.
- [ ] `services/gateway/Dockerfile` builds the gateway's image from a base image pinned by digest, in the build context of issue E1-16: it installs the context's requirements with their hashes checked, copies the shared package and the gateway's, and runs the entry point.
- [ ] `tests/unit/gateway/test_settings.py` covers the reader, and `tests/unit/gateway/test_main.py` the entry point and the Dockerfile, with DynamoDB and Secrets Manager replaced by moto.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_settings.py` passes: a full environment gives the settings, an environment with an empty pools table, empty target groups and secrets and no RHI_DEFAULT_POOL gives settings with no pool and no default pool, each numeric variable left unset gives its default, each required variable left out raises an error that names it, and a secret's value placed in a variable never appears in an error.
- [ ] `uv run --locked pytest tests/unit/gateway/test_main.py` passes: built from an environment whose tables and secrets moto holds, the application answers `HEAD /api/hello` with 200 and `GET /v1/models` without a key with 401 and the cause invalid_key; built from one with no pool and no default pool, it starts and answers `HEAD /api/hello` with 200; and `tests/unit/gateway/test_main.py::test_the_base_image_is_pinned` fails when the Dockerfile names a base image without a digest.
- [ ] `uv run --locked pytest tests/unit/test_config.py` passes with the seven keys at their defaults when a source leaves them out, and `config.example.toml` giving each.

## References

- `docs/design/system-design.md`, sections 2, 5.3, 6.2, 8.5, 11.2, 17
