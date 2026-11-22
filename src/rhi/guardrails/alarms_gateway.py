"""The gateway's error-rate alarm, which works when Grafana is down: a CloudWatch alarm that acts on
the alerts topic (system design, sections 7.3 and 17).

**What counts as the gateway's own fault.** Every error the gateway means to give names a cause of
section 6.2, and each 5xx cause names a fault elsewhere. A fault of the gateway itself is logged
with the cause `none` and answered 500 (`rhi_gateway.telemetry`, `NO_CAUSE` and `FAULT_STATUS`).
A metric filter on the gateway's log group counts the records whose `status` is 500 or more and
whose `cause` is `none`, as the metric `OWN_FAULTS` of the namespace `NAMESPACE`, with no dimension
at all: no student dimension can appear (section 6.6). A record whose status is `null`, and a line
that is not JSON (the collector's), never match.

The other half is a 5xx that the public load balancer answers itself because no gateway task
answered, its `HTTPCode_ELB_5XX_Count`: without it, a gateway that is down would log nothing and
never alarm.

**The share.** The alarm divides the two counts, summed over one period of
gateway_error_alarm_minutes, by the load balancer's `RequestCount` of the same period, and fires
when the share is above gateway_error_alarm_percent (5% over 5 minutes by default). AWS counts in
`RequestCount` only the requests for which the load balancer chose a target: a 502 or 504 it gives
after choosing one is among them, but a 503 it gives while the gateway's service has no task is
not. When no request reached a target, the period's requests are then the load balancer's own 5xx
answers, and a gateway at zero tasks fails all of them: the share is 100% rather than a division by
nothing. A 503 given while a target still answered other requests of the period is in the
numerator and not in `RequestCount`, which can only raise the share a little.

A missing count is 0. A period with no request at all has no share and does not breach, so the
alarm clears once the failures stop.

The construct creates a metric filter and an alarm only, which hold no role and no IAM action.

**What it reads, and how.** The gateway's log group is the gateway stack's, by its fixed name
(`rhi.gateway.service.LOG_GROUP_NAME`), and the stage deploys the gateway stack first
(`rhi.stage`): a name read through an output would add one to the gateway stack, whose outputs the
pipeline's verification reads. The public load balancer is the network stack's, read through that
stack's outputs (`rhi.guardrails.stack`). A parked stage holds neither the alarm nor the filter
(sections 5.6 and 11.2): there is no load balancer and no gateway to watch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_logs as logs
from constructs import Construct

if TYPE_CHECKING:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from aws_cdk import aws_sns as sns

    from rhi.config import Config

NAMESPACE = "rhi/gateway"
OWN_FAULTS = "OwnFaults"
# The fields and values of a record of the gateway's own fault (`rhi_gateway.telemetry`).
FAULT_PATTERN = '{ ($.status >= 500) && ($.cause = "none") }'

LOAD_BALANCER_NAMESPACE = "AWS/ApplicationELB"
LOAD_BALANCER_DIMENSION = "LoadBalancer"


class AlarmsGateway(Construct):
    """The metric filter of the gateway's own faults, as `own_faults`, and the error-rate alarm, as
    `error_rate`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        log_group_name: str,
        load_balancer: elbv2.ApplicationLoadBalancer,
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, construct_id)
        self.own_faults = logs.CfnMetricFilter(
            self,
            "OwnFaults",
            log_group_name=log_group_name,
            filter_pattern=FAULT_PATTERN,
            metric_transformations=[
                logs.CfnMetricFilter.MetricTransformationProperty(
                    metric_namespace=NAMESPACE, metric_name=OWN_FAULTS, metric_value="1"
                )
            ],
        )
        period = config.gateway_error_alarm_minutes * 60
        load_balancer_dimensions = [
            cloudwatch.CfnAlarm.DimensionProperty(
                name=LOAD_BALANCER_DIMENSION, value=load_balancer.load_balancer_full_name
            )
        ]

        def count(
            query_id: str, namespace: str, name: str, dimensions: list[cloudwatch.CfnAlarm.DimensionProperty] | None
        ) -> cloudwatch.CfnAlarm.MetricDataQueryProperty:
            return cloudwatch.CfnAlarm.MetricDataQueryProperty(
                id=query_id,
                return_data=False,
                metric_stat=cloudwatch.CfnAlarm.MetricStatProperty(
                    metric=cloudwatch.CfnAlarm.MetricProperty(namespace=namespace, metric_name=name, dimensions=dimensions),
                    period=period,
                    stat="Sum",
                ),
            )

        percent = config.gateway_error_alarm_percent
        minutes = config.gateway_error_alarm_minutes
        self.error_rate = cloudwatch.CfnAlarm(
            self,
            "ErrorRate",
            alarm_description=(
                f"More than {percent:g}% of the gateway's requests failed for its own fault over {minutes} minutes."
            ),
            metrics=[
                count("own", NAMESPACE, OWN_FAULTS, None),
                count("elb5xx", LOAD_BALANCER_NAMESPACE, "HTTPCode_ELB_5XX_Count", load_balancer_dimensions),
                count("requests", LOAD_BALANCER_NAMESPACE, "RequestCount", load_balancer_dimensions),
                cloudwatch.CfnAlarm.MetricDataQueryProperty(
                    id="share",
                    expression=(
                        "100 * (FILL(own, 0) + FILL(elb5xx, 0))"
                        " / IF(FILL(requests, 0) > 0, FILL(requests, 0), FILL(elb5xx, 0))"
                    ),
                    label="Requests failed for the gateway's own fault, in percent",
                    return_data=True,
                ),
            ],
            comparison_operator="GreaterThanThreshold",
            threshold=percent,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            treat_missing_data="notBreaching",
            alarm_actions=[topic.topic_arn],
        )
