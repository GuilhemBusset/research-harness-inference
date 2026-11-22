"""The gateway's entry point: the application built with its real parts, and the server that runs it.

`python -m rhi_gateway.main` is what the gateway's image runs (`services/gateway/Dockerfile`). It
reads the settings from the environment (`rhi_gateway.settings`), builds the application and
serves it:

- **on port 8080**, on every address of the task: the port the public load balancer's target
  group uses (the design names none);
- **the metrics on `127.0.0.1`, port 9091**, for the collector of the same task and nobody else
  (`rhi_gateway.metrics`).

A setting that is refused raises before anything listens, and so does a secret that cannot be
read: the task stops, and the error names a variable or a pool and never a value. So does a pool's
request profile that names an entry the gateway does not have (`rhi_gateway.profile`): that error
names the pool and the entry.

**The parts** (`build`). The keys table is read through `DynamoKeyStore`, the usage table is the
`DynamoDBLedger`'s, given the course end its rows expire by, the pool-state table and the target groups are the `Readiness` reader's, the
scaler function is the `Waker`'s, and the heartbeats table is the `Heartbeat`'s. The body filters
are the three formats', with the body limit of the settings and, on each forwarded route, every
pool's request profile, which is built first; the proxy has the upstream address,
the pools' secrets and the longest request; the hold has the keep-alive interval; the refusals
have the three `Retry-After`. With no pool, every one of these is built the same, over no pool.

**The secrets.** A pool's bearer secret is in no variable and no template: the settings hold its
ARN, and `read_secrets` reads the value through the Secrets Manager API, once, at start, with the
task's role. The values stay in the proxy.

**AWS.** The clients are the AWS SDK's (`aws_clients`), made with what the task gives the SDK: the
credentials of the task's role and the region of the task. Nothing here names either.

**The task's ID** names this task's heartbeat rows. `task_id` reads it from the task metadata
endpoint ECS gives the container (`ECS_CONTAINER_METADATA_URI_V4`): the last part of the task's
ARN, the ID the collector labels this task's series with. Without the endpoint, or when it does
not answer with one, the ID is random: the rows of two tasks must never be one row.

**SIGTERM** (`serve`). uvicorn stops accepting connections, lets every request it has accepted
run to its end, with no deadline of its own (a held stream may last minutes; the longest request
is the proxy's limit), and then ends the application's lifespan, where the heartbeat writes its
last rows, with both counts at zero (`rhi_gateway.app.build_app`). uvicorn writes no access log:
the gateway's own record of each request is the log (`rhi_gateway.telemetry`).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette

from rhi_gateway import metrics as metrics_endpoint
from rhi_gateway import profile, routes
from rhi_gateway.app import Filter, Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter, Rewrite
from rhi_gateway.budget import DynamoDBLedger
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.heartbeat import Heartbeat
from rhi_gateway.keystore import DynamoKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.metrics import Metrics
from rhi_gateway.proxy import Proxy, upstream_client
from rhi_gateway.readiness import Readiness
from rhi_gateway.settings import Settings, read_settings
from rhi_gateway.waker import Waker

# Every address of the task: the load balancer reaches the container on the task's own address, and
# the task's security group decides who reaches the port.
HOST = "0.0.0.0"
PORT = 8080

# Where ECS gives a container its task's metadata, and how long the gateway waits for it at start.
METADATA_VARIABLE = "ECS_CONTAINER_METADATA_URI_V4"
METADATA_SECONDS = 2.0
# A task's ID as ECS writes it, at the end of the task's ARN.
_TASK_ID = re.compile(r"[0-9A-Za-z-]{1,64}", re.ASCII)

_LOG = logging.getLogger(__name__)


class StartError(RuntimeError):
    """The gateway cannot start. The message names a pool and the kind of failure, never a value or AWS's text."""


@dataclass(frozen=True)
class Clients:
    """The clients of the AWS SDK the gateway's parts are given.

    `dynamodb` is `boto3.client("dynamodb")`, the ledger's; `tables` is
    `boto3.resource("dynamodb")`, which gives the keys, pool-state and heartbeats tables;
    `secretsmanager`, `elbv2` and `lambda_client` are the clients of those services.
    """

    dynamodb: Any
    tables: Any
    secretsmanager: Any
    elbv2: Any
    lambda_client: Any


def aws_clients() -> Clients:
    """The clients, with the credentials and the region the task gives the SDK."""
    # Imported here: a test that builds the gateway over its own clients loads no session.
    import boto3

    return Clients(
        dynamodb=boto3.client("dynamodb"),
        tables=boto3.resource("dynamodb"),
        secretsmanager=boto3.client("secretsmanager"),
        elbv2=boto3.client("elbv2"),
        lambda_client=boto3.client("lambda"),
    )


def read_secrets(settings: Settings, secretsmanager: Any) -> dict[str, str]:
    """Each pool's bearer secret, read from the ARN the settings hold for it. No pool, no read."""
    secrets: dict[str, str] = {}
    for pool, arn in settings.pool_secrets.items():
        try:
            value = secretsmanager.get_secret_value(SecretId=arn).get("SecretString")
        except Exception as error:  # noqa: BLE001 - whatever the SDK raised, its text is AWS's and is not repeated
            raise StartError(
                f"the secret of pool {pool!r} cannot be read: the AWS SDK raised {type(error).__name__}"
            ) from None
        if not isinstance(value, str) or not value:
            raise StartError(f"the secret of pool {pool!r} holds no text")
        secrets[pool] = value
    return secrets


def task_id(environ: Mapping[str, str], *, transport: httpx.BaseTransport | None = None) -> str:
    """This task's ID from the task metadata endpoint, or a random identifier when there is none.

    `transport` replaces the network, for tests.
    """
    endpoint = environ.get(METADATA_VARIABLE)
    if endpoint:
        try:
            with httpx.Client(transport=transport, timeout=METADATA_SECONDS, trust_env=False) as client:
                answer = client.get(endpoint.rstrip("/") + "/task")
            arn = answer.json().get("TaskARN") if answer.is_success else None
            found = arn.rsplit("/", 1)[-1] if isinstance(arn, str) else ""
            if _TASK_ID.fullmatch(found):
                return found
            _LOG.warning("the task metadata gave no task ID: the heartbeat rows are under a random one")
        except Exception as error:  # noqa: BLE001 - whatever the read raised, the gateway starts, under a random ID
            _LOG.warning(
                "the task metadata was not read (%s): the heartbeat rows are under a random ID", type(error).__name__
            )
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Gateway:
    """The gateway as built: the application, and the parts whoever serves or tests it reaches."""

    app: Starlette
    metrics: Metrics
    readiness: Readiness
    forwarding: Forwarding


def build(
    settings: Settings,
    aws: Clients,
    *,
    task: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Gateway:
    """The gateway of these settings, over these clients. It reads each pool's secret and nothing else.

    Every pool's request profile is built first: one that names an entry the gateway does not have
    raises `rhi_gateway.profile.ProfileError`, which names the pool and the entry, before a secret
    is read. `task` names the heartbeat rows, a random identifier when none is given. `transport`
    replaces the network between the gateway and the engines, for tests.
    """
    profiles = profile.build(settings.pools)
    secrets = read_secrets(settings, aws.secretsmanager)
    pools, default_pool, maximum = settings.pools, settings.default_pool, settings.body_limit_bytes

    keys = DynamoKeyStore(aws.tables.Table(settings.keys_table))
    authenticator = Authenticator(keys)
    ledger = DynamoDBLedger(aws.dynamodb, settings.usage_table, course_end=settings.course_end)
    readiness = Readiness(settings.target_groups, elbv2=aws.elbv2, table=aws.tables.Table(settings.pool_state_table))
    waker = Waker(readiness, lambda_client=aws.lambda_client, function_name=settings.scaler_function)
    heartbeat = Heartbeat(
        aws.tables.Table(settings.heartbeat_table),
        task or uuid.uuid4().hex,
        [pool.name for pool in pools],
        waker=waker,
    )
    gateway_metrics = Metrics(ledger, keys)

    def rewrites(route: routes.Route) -> dict[str, Rewrite]:
        """What a route's filter runs in its per-pool step: the pools' profiles on that route."""
        return profile.rewrites(profiles, route)

    filters: dict[routes.Route, Filter] = {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, pools, default_pool, rewrites(routes.MESSAGES), maximum),
        routes.COUNT_TOKENS: BodyFilter(
            messages.COUNT_TOKENS, pools, default_pool, rewrites(routes.COUNT_TOKENS), maximum
        ),
        routes.CHAT_COMPLETIONS: ChatBodyFilter(pools, default_pool, rewrites(routes.CHAT_COMPLETIONS), maximum),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, pools, default_pool, rewrites(routes.RESPONSES), maximum),
    }
    forwarding = Forwarding(
        authenticator=authenticator,
        ledger=ledger,
        filters=filters,
        proxy=Proxy(
            upstream_client(transport),
            settings.upstream_url,
            secrets,
            longest_request_seconds=settings.longest_request_seconds,
        ),
        holding=Holding(views=readiness.views, wake=waker.ask, keepalive_seconds=settings.keepalive_seconds),
        heartbeat=heartbeat,
        retry_after=settings.retry_after,
    )
    local = LocalRoutes(
        authenticator=authenticator,
        pools=pools,
        views=readiness.views,
        default_pool=default_pool,
        waker=waker,
        ledger=ledger,
    )
    app = build_app(
        forwarding_handlers(forwarding),
        local=local,
        heartbeat=heartbeat,
        metrics=gateway_metrics,
        readiness=readiness,
    )
    return Gateway(app=app, metrics=gateway_metrics, readiness=readiness, forwarding=forwarding)


def serve(gateway: Gateway, *, metrics_port: int = metrics_endpoint.PORT, **where: Any) -> None:
    """Serve the metrics on the loopback address, and the application where `where` says, until SIGTERM.

    `where` is uvicorn's `host` and `port`, or `uds`. `metrics_port` is for the tests, which take a
    free port: the gateway gives none.
    """
    gateway.metrics.serve(port=metrics_port)
    uvicorn.run(
        gateway.app,
        **where,
        # The readiness reads and the heartbeat run in the lifespan: without it there is no gateway.
        lifespan="on",
        # No deadline cuts a request in flight.
        timeout_graceful_shutdown=None,
        access_log=False,
    )


def main() -> None:
    """Serve the gateway on port 8080.

    A refused setting, a request profile that names an entry the gateway does not have or an unread
    secret raises before anything listens.
    """
    settings = read_settings(os.environ)
    serve(build(settings, aws_clients(), task=task_id(os.environ)), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
