"""The access stack: the students' state, the key issuer, and later the admin's sign-in (system
design, sections 2 and 6.4).

The stack and the logical IDs of its tables are recorded in `tests/policy/stateful_logical_ids.json`:
moving a table to another stack replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.access.issuer import Issuer
from rhi.access.tables import AccessTables

if TYPE_CHECKING:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from constructs import Construct

    from rhi.config import Config


class AccessStack(cdk.Stack):
    """Holds the keys, roster and usage tables, which the other stacks of the stage take from
    `tables` (`tables.keys`, ...), and the key issuer, as `issuer`, reached through `listener`, the
    public load balancer's HTTPS listener, or from nowhere while it is None."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        listener: elbv2.IApplicationListener | None,
    ) -> None:
        super().__init__(scope, construct_id)
        # The listener is the network stack's: this stack imports it through an export, the CDK's
        # default, stated here. Network cannot drop an export while this stack imports it, so the
        # issuer's rule never points at a listener that is gone.
        cdk.CrossStackReferences.of(self).consume(cdk.ReferenceStrength.STRONG)
        self.tables = AccessTables(self, "Tables")
        self.issuer = Issuer(
            self,
            "Issuer",
            config=config,
            roster=self.tables.roster,
            keys=self.tables.keys,
            listener=listener,
        )
