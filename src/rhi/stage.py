"""The stage the pipeline deploys: everything of the endpoint that a merge to `main` updates.

The pipeline stack deploys this stage as a nested assembly of the cloud assembly (system design,
section 8.2). The stage and its stack are environment-agnostic: neither is given an account or a
region, so both come from the credentials that deploy (section 10).

The stack holds no resource yet. CloudFormation refuses a template with no resource, and the CDK
says so at synthesis, as a warning (rule F0001 of its CloudFormation validation) until the first
resource arrives. `cdk.json` leaves out the flag that would make the warning an error,
`@aws-cdk/core:validateAgainstDefaultRules`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.config import Config


class EndpointStage(cdk.Stage):
    """The endpoint, as one stage with one stack. Each construct arrives with its own issue and
    reads what it needs from `config`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        self.config = config
        self.stack = cdk.Stack(self, "Endpoint")
