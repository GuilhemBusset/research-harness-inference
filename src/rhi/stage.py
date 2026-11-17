"""The stage the pipeline deploys: everything of the endpoint that a merge to `main` updates.

The pipeline stack deploys this stage as a nested assembly of the cloud assembly (system design,
section 8.2). The stage and its stacks are environment-agnostic: none is given an account or a
region, so both come from the credentials that deploy (section 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.access.stack import AccessStack
from rhi.network.stack import NetworkStack
from rhi.observability.stack import ObservabilityStack
from rhi.pools.stack import PoolsStack

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.config import Config

# The name the bounded bootstrap gives the permissions boundary (`setup/bootstrap/policies.yaml`).
PERMISSIONS_BOUNDARY = "rhi-permissions-boundary"


class EndpointStage(cdk.Stage):
    """The endpoint, as one stage of stacks. Each construct arrives with its own issue and reads
    what it needs from `config`; a stack that needs the VPC takes it from `network.vpc`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        # Every role of the stage, those the CDK makes for its own functions included, carries the
        # permissions boundary of the bounded bootstrap, by its fixed name, in the deploying account
        # (`setup/bootstrap/README.md`).
        boundary = cdk.PermissionsBoundary.from_name(PERMISSIONS_BOUNDARY)
        super().__init__(scope, construct_id, permissions_boundary=boundary)
        self.config = config
        self.network = NetworkStack(self, "Network", config=config)
        # The students' tables and the pools' tables. Their stacks are fixed with their tables'
        # logical IDs (`tests/policy/stateful_logical_ids.json`): a table that moves is replaced.
        # The key issuer beside the students' tables, behind the public listener when there is one.
        self.access = AccessStack(self, "Access", config=config, listener=self.network.https_listener)
        # The metrics workspace, which the nodes, the gateway and the scaler write to. Its logical
        # ID is recorded too.
        self.observability = ObservabilityStack(self, "Observability")
        # The internal load balancer beside them, in the network's VPC, the groups and the scaler,
        # which writes to the workspace.
        self.pools = PoolsStack(self, "Pools", config=config, network=self.network, observability=self.observability)
