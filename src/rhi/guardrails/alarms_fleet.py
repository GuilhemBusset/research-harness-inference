"""The fleet's alarms, which work when Grafana is down: CloudWatch alarms that act on the alerts
topic (system design, section 7.3).

Three guard money from outside the scaler, so that a broken scaler cannot cost money (sections 5.1
and 5.4). The first two read Auto Scaling's own group metrics (`rhi.pools.group` turns them on),
never what the scaler publishes, in the namespace `AWS/AutoScaling`, by the group's name, each
statistic the most of its one-minute period:

- **Wanted with nothing in service**, per pool: the group's `GroupDesiredCapacity` above 0 and its
  `GroupInServiceInstances` at 0, in each of 10 one-minute periods.
- **Running with nothing in service**, per pool: the group's `GroupTotalInstances` above 0 (the
  group keeps no stopped instance, so every instance it counts runs) and its
  `GroupInServiceInstances` at 0, in each of 20 one-minute periods.
- **The scaler has completed no run** for 5 one-minute periods: no `RunCompleted` data point, which
  the scaler publishes after each completed run (`rhi_scaler.cloudwatch`). Missing data is what it
  alarms on, so it breaches.

Each of the three is a metric math expression, or one metric, that is 1 in a period where its
condition holds, and alarms when it is 1 in every period of its window. They evaluate a wall-clock
window of whole minutes: the alarm reads exactly its 10, 20 or 5 periods, never an older data
point that CloudWatch's default sliding window would reach back for when one is missing. A period
the fleet's expression has no data for does not breach, so the condition must be seen in every
period; a period with no completed run does.

Two more come from the scaler's own rules, each per pool, from what the scaler publishes in the
namespace `rhi/scaler` with the dimension `Pool`, over one one-minute period:

- **Launches suspended**: `LaunchesSuspended` at 1, after three abandoned launches (section 5.1).
- **In service while no session is on**: `MinutesInServiceOutsideSessions` above
  outside_sessions_alarm_hours, 1 by default, in minutes (sections 11.3 and 17).

These two keep their state while the scaler publishes nothing: the scaler's own alarm says so.

Every alarm acts on the alerts topic (`rhi.guardrails.alerts`, whose policy lets CloudWatch publish
to it). The only dimensions their metrics carry are a group's name and a pool's name: nothing that
identifies a student (section 6.6).

The construct creates alarms only, which hold no role and no IAM action.

**What it reads, and how.** The groups are the pools stack's, which deploys before the guardrails
stack: each alarm names its group through the pools stack's outputs, never an export
(`rhi.guardrails.stack`), so that the pools stack may remove a group, as parking does, while an
alarm still names it. A parked stage holds none of these alarms (sections 5.6 and 11.2): there is
no group, no scaler and no gateway to watch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_cloudwatch as cloudwatch
from constructs import Construct

from rhi_scaler.cloudwatch import LAUNCHES_SUSPENDED, MINUTES_OUTSIDE_SESSIONS, POOL_DIMENSION, RUN_COMPLETED
from rhi_scaler.cloudwatch import NAMESPACE as SCALER_NAMESPACE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aws_cdk import aws_sns as sns

    from rhi.config import Config
    from rhi.pools.group import PoolGroup

AUTO_SCALING_NAMESPACE = "AWS/AutoScaling"
GROUP_DIMENSION = "AutoScalingGroupName"
PERIOD_SECONDS = 60

# The periods, of one minute, each condition must hold in.
WANTED_PERIODS = 10
RUNNING_PERIODS = 20
SCALER_PERIODS = 5

# Each period of the window on its own minute: exactly the alarm's periods, in UTC.
WALL_CLOCK = cloudwatch.CfnAlarm.EvaluationWindowProperty(
    wall_clock_window=cloudwatch.CfnAlarm.WallClockWindowProperty(timezone="UTC")
)


def _metric(
    query_id: str, namespace: str, name: str, dimensions: dict[str, str], statistic: str
) -> cloudwatch.CfnAlarm.MetricDataQueryProperty:
    return cloudwatch.CfnAlarm.MetricDataQueryProperty(
        id=query_id,
        return_data=False,
        metric_stat=cloudwatch.CfnAlarm.MetricStatProperty(
            metric=cloudwatch.CfnAlarm.MetricProperty(
                namespace=namespace,
                metric_name=name,
                dimensions=[cloudwatch.CfnAlarm.DimensionProperty(name=k, value=v) for k, v in dimensions.items()],
            ),
            period=PERIOD_SECONDS,
            stat=statistic,
        ),
    )


class AlarmsFleet(Construct):
    """The scaler's alarm, as `scaler`, and each pool's four, by the pool's name, in `wanted`,
    `running`, `launches_suspended` and `outside_sessions`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        groups: Mapping[str, PoolGroup],
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, construct_id)
        actions = [topic.topic_arn]

        def fleet_alarm(
            alarm_id: str,
            description: str,
            periods: int,
            metrics: list[cloudwatch.CfnAlarm.MetricDataQueryProperty],
            expression: str,
        ) -> cloudwatch.CfnAlarm:
            return cloudwatch.CfnAlarm(
                self,
                alarm_id,
                alarm_description=description,
                metrics=[
                    *metrics,
                    cloudwatch.CfnAlarm.MetricDataQueryProperty(id="condition", expression=expression, return_data=True),
                ],
                comparison_operator="GreaterThanOrEqualToThreshold",
                threshold=1,
                evaluation_periods=periods,
                datapoints_to_alarm=periods,
                treat_missing_data="notBreaching",
                evaluation_window=WALL_CLOCK,
                alarm_actions=actions,
            )

        def scaler_alarm(
            alarm_id: str, description: str, pool: str, name: str, operator: str, threshold: float
        ) -> cloudwatch.CfnAlarm:
            return cloudwatch.CfnAlarm(
                self,
                alarm_id,
                alarm_description=description,
                namespace=SCALER_NAMESPACE,
                metric_name=name,
                dimensions=[cloudwatch.CfnAlarm.DimensionProperty(name=POOL_DIMENSION, value=pool)],
                statistic="Maximum",
                period=PERIOD_SECONDS,
                evaluation_periods=1,
                comparison_operator=operator,
                threshold=threshold,
                treat_missing_data="ignore",
                alarm_actions=actions,
            )

        self.scaler = cloudwatch.CfnAlarm(
            self,
            "ScalerRunCompleted",
            alarm_description="The scaler has completed no run for 5 minutes.",
            namespace=SCALER_NAMESPACE,
            metric_name=RUN_COMPLETED,
            statistic="Sum",
            period=PERIOD_SECONDS,
            evaluation_periods=SCALER_PERIODS,
            datapoints_to_alarm=SCALER_PERIODS,
            comparison_operator="LessThanThreshold",
            threshold=1,
            treat_missing_data="breaching",
            evaluation_window=WALL_CLOCK,
            alarm_actions=actions,
        )
        self.wanted: dict[str, cloudwatch.CfnAlarm] = {}
        self.running: dict[str, cloudwatch.CfnAlarm] = {}
        self.launches_suspended: dict[str, cloudwatch.CfnAlarm] = {}
        self.outside_sessions: dict[str, cloudwatch.CfnAlarm] = {}
        for pool, pool_group in groups.items():
            group = {GROUP_DIMENSION: pool_group.group.ref}
            in_service = _metric("in_service", AUTO_SCALING_NAMESPACE, "GroupInServiceInstances", group, "Maximum")
            self.wanted[pool] = fleet_alarm(
                f"Wanted-{pool}",
                f"The pool {pool} has been wanted for 10 minutes with nothing in service.",
                WANTED_PERIODS,
                [_metric("desired", AUTO_SCALING_NAMESPACE, "GroupDesiredCapacity", group, "Maximum"), in_service],
                "IF(desired > 0 AND in_service == 0, 1, 0)",
            )
            self.running[pool] = fleet_alarm(
                f"Running-{pool}",
                f"The group of the pool {pool} has had instances running for 20 minutes with none in service.",
                RUNNING_PERIODS,
                [_metric("total", AUTO_SCALING_NAMESPACE, "GroupTotalInstances", group, "Maximum"), in_service],
                "IF(total > 0 AND in_service == 0, 1, 0)",
            )
            self.launches_suspended[pool] = scaler_alarm(
                f"LaunchesSuspended-{pool}",
                f"The scaler has suspended the launches of the pool {pool} after three abandoned launches.",
                pool,
                LAUNCHES_SUSPENDED,
                "GreaterThanOrEqualToThreshold",
                1,
            )
            hours = config.outside_sessions_alarm_hours
            self.outside_sessions[pool] = scaler_alarm(
                f"OutsideSessions-{pool}",
                f"The pool {pool} has been in service for more than {hours * 60} minutes while no session is on.",
                pool,
                MINUTES_OUTSIDE_SESSIONS,
                "GreaterThanThreshold",
                hours * 60,
            )
