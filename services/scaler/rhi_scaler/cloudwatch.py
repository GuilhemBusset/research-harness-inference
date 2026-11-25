"""What the scaler tells CloudWatch, for the alarms that must work when Grafana is down (system
design, section 7.3).

The scaler's own numbers go to Prometheus (`rhi_scaler.metrics`, section 7.2). Four alarms read
what only the scaler knows, so at the end of every completed run it also publishes, in the
namespace `NAMESPACE`:

| Data point | Dimension | Value |
| --- | --- | --- |
| `RunCompleted` | none | 1: a run completed. Its absence for five minutes is the alarm (section 5.1) |
| `LaunchesSuspended` | `Pool` | 1 while the pool's launches are suspended after three abandoned launches, else 0 (sections 5.1 and 5.4) |
| `MinutesInServiceOutsideSessions` | `Pool` | The whole minutes the pool has been in service while its session is not active (`rhi_scaler.outside_sessions`) |
| `InstancesMissing` | `Pool` | The instances the pool's session requests (`PoolDecision.requested`) less those the run saw in service, never below 0, and so 0 for a pool with no active session (section 5.4) |

The data points carry no timestamp: CloudWatch dates them when it receives them, after the run has
applied its decision. The pool's name is the only dimension any of them carries. A CloudWatch metric cannot be deleted
for 15 months, so nothing that names a student ever goes there (section 6.6; evidence E27).
"""

from __future__ import annotations

from typing import Any

from rhi_scaler.model import Decision, Inputs
from rhi_scaler.outside_sessions import minutes

NAMESPACE = "rhi/scaler"
RUN_COMPLETED = "RunCompleted"
LAUNCHES_SUSPENDED = "LaunchesSuspended"
MINUTES_OUTSIDE_SESSIONS = "MinutesInServiceOutsideSessions"
INSTANCES_MISSING = "InstancesMissing"
POOL_DIMENSION = "Pool"


def metric_data(inputs: Inputs, decision: Decision, now: int) -> list[dict[str, Any]]:
    """The data points of a run that completed at the time `now`, having read these inputs, with
    this decision, whose pools' memory holds where their stretch outside sessions stands
    (`outside_sessions.carry`)."""
    in_service = {pool.config.name: len(pool.supply.in_service) for pool in inputs.pools}
    data: list[dict[str, Any]] = [{"MetricName": RUN_COMPLETED, "Value": 1.0, "Unit": "Count"}]
    for pool in decision.pools:
        dimensions = [{"Name": POOL_DIMENSION, "Value": pool.pool}]
        outside = minutes(pool.memory.outside_sessions_since, now)
        missing = max(0, pool.requested - in_service[pool.pool])
        data += [
            {
                "MetricName": LAUNCHES_SUSPENDED,
                "Dimensions": dimensions,
                "Value": 1.0 if pool.suspend_launches else 0.0,
                "Unit": "None",
            },
            {
                "MetricName": MINUTES_OUTSIDE_SESSIONS,
                "Dimensions": dimensions,
                "Value": float(outside),
                "Unit": "None",
            },
            {
                "MetricName": INSTANCES_MISSING,
                "Dimensions": dimensions,
                "Value": float(missing),
                "Unit": "None",
            },
        ]
    return data


def publish(client: Any, inputs: Inputs, decision: Decision, now: int) -> None:
    """Publishes a completed run's data points, in one call, with the CloudWatch client given."""
    client.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data(inputs, decision, now))
