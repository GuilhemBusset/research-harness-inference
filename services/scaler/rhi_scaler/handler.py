"""The scaler's function: one run, from its inputs to its calls (system design, section 5.1).

The gateway calls it when a request needs a cold pool, a one-minute schedule calls it, and so does
the admin CLI. Whoever calls, a run does the same thing, and the event is never read:

1. read the settings (`rhi_scaler.settings`) and the inputs (`rhi_scaler.aws_io.read`);
2. decide (`rhi_scaler.decide`), from the inputs alone, and note in each pool's memory how long it
   has been in service outside a session (`rhi_scaler.outside_sessions`), which only reports;
3. apply: the Auto Scaling calls that change something, then each pool's note and memory record;
4. log one record per pool with its decision and its reason, send the fleet's metrics with the
   session gauge, from the switch the run read (`rhi_scaler.metrics`), and publish to CloudWatch
   what the alarms read (`rhi_scaler.cloudwatch`).

A run that fails before its writes leaves the pool as it was; the next run reads the world again.
A run that fails publishes nothing to CloudWatch: the missing `RunCompleted` is what the alarm
reads. A metrics push that fails does not fail the run: the decision is already applied, and the
log holds it; a switch that could not be read sends no session sample. A run whose reading of the
pools fails sends the session gauge alone, if it can read the switch, and then fails: the gauge's
last reading stays the one in force until the next, and an off left unsent would let the session
seem on. Two failures do fail the
run, after its decision is applied, which stands:

- the session switch could not be read: what demand calls for is applied all the same, a pool seen
  with no instance in service ends its stretch outside sessions while every other stretch stays as
  it was, and nothing is published;
- CloudWatch refused the publication: the run did not deliver what the alarms read.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import boto3

from rhi_common.records import session_is_on
from rhi_scaler import aws_io, cloudwatch, outside_sessions
from rhi_scaler.decide import decide
from rhi_scaler.metrics import DecisionCounts, RemoteWriter, fleet_series, session_series
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


def send_session_alone(
    clients: aws_io.Clients, settings: Settings, writer: RemoteWriter, counts: DecisionCounts, milliseconds: int
) -> None:
    """The session gauge on its own, for a run that could not read the pools: nothing if the switch
    cannot be read either, or the push fails."""
    try:
        session_on = session_is_on(aws_io.read_session(clients, settings.tables))
        writer.push([session_series(session_on, counts.stamp(milliseconds))])
    except Exception as error:  # noqa: BLE001 - the run fails on its own error, which is the one to report
        LOGGER.warning(json.dumps({"event": "session-gauge-not-sent", "error": type(error).__name__}))


def run_cycle(
    settings: Settings,
    clients: aws_io.Clients,
    writer: RemoteWriter,
    now: int,
    counts: DecisionCounts,
    milliseconds: int | None = None,
) -> Decision:
    """One cycle at the time `now`: read, decide, apply and store the memory, log, send the metrics,
    publish to CloudWatch.

    `now` is the decision clock, in whole seconds; `milliseconds`, the run's time for the
    metrics' samples (by default, `now`'s second).
    """
    try:
        run = aws_io.read(clients, settings.tables, settings.pools, now)
    except Exception:
        send_session_alone(clients, settings, writer, counts, now * 1000 if milliseconds is None else milliseconds)
        raise
    readable = run.session_error is None
    decision = outside_sessions.carry(run.inputs, decide(run.inputs), run.session, read=readable)
    aws_io.apply(clients, settings.tables, run, decision)
    log_decisions(run, decision)
    session_on = session_is_on(run.session) if readable else None
    try:
        writer.push(fleet_series(run.inputs, decision, counts, milliseconds, session_on=session_on))
    except Exception as error:  # noqa: BLE001 - the decision is applied; a lost push only leaves a gap
        LOGGER.warning(json.dumps({"event": "metrics-not-sent", "error": type(error).__name__}))
    if run.session_error is not None:
        LOGGER.error(json.dumps({"event": "session-switch-not-read", "error": type(run.session_error).__name__}))
        raise run.session_error
    try:
        cloudwatch.publish(clients.cloudwatch, decision, now)
    except Exception as error:
        LOGGER.error(json.dumps({"event": "cloudwatch-not-published", "error": type(error).__name__}))
        raise
    return decision


def connect(settings: Settings) -> tuple[aws_io.Clients, RemoteWriter]:
    """The AWS clients and the metrics writer, with the function's own credentials."""
    session = boto3.Session(region_name=settings.region)
    clients = aws_io.Clients(
        autoscaling=session.client("autoscaling"),
        elbv2=session.client("elbv2"),
        dynamodb=session.client("dynamodb"),
        cloudwatch=session.client("cloudwatch"),
    )
    return clients, RemoteWriter(settings.workspace_url, settings.region, session.get_credentials())


def handler(event: object, context: object, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The Lambda entry point. The caller's event and context change nothing in the run."""
    del event, context
    settings = read_settings(os.environ if environ is None else environ)
    clients, writer = connect(settings)
    moment = time.time()
    decision = run_cycle(settings, clients, writer, int(moment), COUNTS, int(moment * 1000))
    return {"pools": [{"pool": pool.pool, "desired": pool.desired, "reason": pool.reason.value} for pool in decision.pools]}
