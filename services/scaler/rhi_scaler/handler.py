"""The scaler's function: one run, from its inputs to its calls (system design, section 5.1).

The gateway calls it when a request needs a cold pool, a one-minute schedule calls it, and so does
the admin CLI. Whoever calls, a run does the same thing, and the event is never read:

1. read the settings (`rhi_scaler.settings`) and the inputs (`rhi_scaler.aws_io.read`);
2. decide (`rhi_scaler.decide`), from the inputs alone;
3. apply: the Auto Scaling calls that change something, then each pool's note and memory record;
4. log one record per pool with its decision and its reason, and send the fleet's metrics
   (`rhi_scaler.metrics`).

A run that fails before its writes leaves the pool as it was; the next run reads the world again.
A metrics push that fails does not fail the run: the decision is already applied, and the log
holds it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import boto3

from rhi_scaler import aws_io
from rhi_scaler.decide import decide
from rhi_scaler.metrics import DecisionCounts, RemoteWriter, fleet_series
from rhi_scaler.model import Decision
from rhi_scaler.settings import Settings, read_settings

LOGGER = logging.getLogger("rhi_scaler")
LOGGER.setLevel(logging.INFO)

# The decisions of this process, for the decisions counter.
COUNTS = DecisionCounts()


def log_decisions(run: aws_io.Run, decision: Decision) -> None:
    """One record per pool, made of the decision and the supply read, and of nothing else."""
    supplies = {pool.config.name: pool.supply for pool in run.inputs.pools}
    for pool in decision.pools:
        supply = supplies[pool.pool]
        record = {
            "event": "decision",
            "pool": pool.pool,
            "reason": pool.reason.value,
            "desired_before": supply.desired,
            "desired": pool.desired,
            "in_service": len(supply.in_service),
            "starting": len(supply.starting),
            "warm": len(supply.warm),
            "launches_suspended": pool.suspend_launches,
        }
        LOGGER.info(json.dumps(record, sort_keys=True))


def run_cycle(
    settings: Settings, clients: aws_io.Clients, writer: RemoteWriter, now: int, counts: DecisionCounts
) -> Decision:
    """One cycle at the time `now`: read, decide, apply and store the memory, log, send the metrics."""
    run = aws_io.read(clients, settings.tables, settings.pools, now)
    decision = decide(run.inputs)
    aws_io.apply(clients, settings.tables, run, decision)
    log_decisions(run, decision)
    try:
        writer.push(fleet_series(run.inputs, decision, counts))
    except Exception as error:  # noqa: BLE001 - the decision is applied; a lost push only leaves a gap
        LOGGER.warning(json.dumps({"event": "metrics-not-sent", "error": type(error).__name__}))
    return decision


def connect(settings: Settings) -> tuple[aws_io.Clients, RemoteWriter]:
    """The AWS clients and the metrics writer, with the function's own credentials."""
    session = boto3.Session(region_name=settings.region)
    clients = aws_io.Clients(
        autoscaling=session.client("autoscaling"),
        elbv2=session.client("elbv2"),
        dynamodb=session.client("dynamodb"),
    )
    return clients, RemoteWriter(settings.workspace_url, settings.region, session.get_credentials())


def handler(event: object, context: object, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The Lambda entry point. The caller's event and context change nothing in the run."""
    del event, context
    settings = read_settings(os.environ if environ is None else environ)
    clients, writer = connect(settings)
    decision = run_cycle(settings, clients, writer, int(time.time()), COUNTS)
    return {"pools": [{"pool": pool.pool, "desired": pool.desired, "reason": pool.reason.value} for pool in decision.pools]}
