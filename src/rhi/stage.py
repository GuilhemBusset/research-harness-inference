"""The stage the pipeline deploys: everything of the endpoint that a merge to `main` updates.

The pipeline stack deploys this stage as a nested assembly of the cloud assembly (system design,
section 8.2). The stage and its stacks are environment-agnostic: none is given an account or a
region, so both come from the credentials that deploy (section 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.network.stack import NetworkStack

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.config import Config


class EndpointStage(cdk.Stage):
    """The endpoint, as one stage of stacks. Each construct arrives with its own issue and reads
    what it needs from `config`; a stack that needs the VPC takes it from `network.vpc`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        self.config = config
        self.network = NetworkStack(self, "Network", config=config)
