"""The observability stack: the metrics workspace and Grafana, and later the alarms (system
design, section 7).

Other stacks of the stage take the workspace and its policies from `prometheus`. The pools stack
and the gateway stack depend on this one, so it references neither: it takes the VPC, Grafana's
security group and the public load balancer's HTTPS listener, when it has one, from the network
stack, and the admin's sign-in from the access stack, and Grafana's role names the other stacks'
log groups by their fixed names (`rhi.observability.grafana`). The stack and the workspace's
logical ID are recorded in `tests/policy/stateful_logical_ids.json`: moving the workspace to
another stack replaces it.

Self-hosted Grafana is decision 11 of the design (section 15). Were the owner to choose Amazon
Managed Grafana, the swap is this stack's `grafana` and nothing outside it.

The stack declares one output, `WorkspaceUrl`: the workspace's query address, which the pipeline's
verification takes as an input (`rhi.pipeline.verify`). The workspace also carries the tag
`STAGE_TAG`, whose value is the name of the stage that deploys it. The verification's role is
written before the workspace has an ID, so it names the workspace by that tag: it may query the
workspace of the stage it verifies and no other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.observability.grafana import Grafana
from rhi.observability.prometheus import PrometheusWorkspace

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.access.stack import AccessStack
    from rhi.config import Config
    from rhi.network.stack import NetworkStack

# The tag that says which stage a workspace belongs to.
STAGE_TAG = "rhi-stage"


class ObservabilityStack(cdk.Stack):
    """Holds the Managed Prometheus workspace, which the other stacks take from `prometheus`
    (`prometheus.workspace`, `prometheus.write_policy`, `prometheus.query_policy`), the
    workspace's query address, as the output `workspace_url`, and Grafana, as `grafana`, reached
    through the network's HTTPS listener behind the access stack's sign-in, or from nowhere while
    there is no listener."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        network: NetworkStack,
        access: AccessStack,
    ) -> None:
        super().__init__(scope, construct_id)
        # What Grafana reads of the network stack and of the access stack it imports through
        # exports, the CDK's default, stated here. Neither can drop an export while this stack
        # imports it, so Grafana's rule never points at a listener or a user pool that is gone.
        cdk.CrossStackReferences.of(self).consume(cdk.ReferenceStrength.STRONG)
        self.prometheus = PrometheusWorkspace(self, "Prometheus")
        stage = cdk.Stage.of(self)
        # A stack outside a stage belongs to none: the tag then names the stack itself.
        self.stage_name = stage.stage_name if stage is not None and stage.stage_name else self.stack_name
        cdk.Tags.of(self.prometheus.workspace).add(STAGE_TAG, self.stage_name)
        self.workspace_url = cdk.CfnOutput(
            self,
            "WorkspaceUrl",
            description="The metrics workspace's query address.",
            value=self.prometheus.workspace.attr_prometheus_endpoint,
        )
        self.grafana = Grafana(
            self,
            "Grafana",
            config=config,
            vpc=network.vpc,
            security_group=network.security_groups.grafana,
            workspace=self.prometheus,
            sign_in=access.sign_in,
            listener=network.https_listener,
        )
