"""A pool's bearer secret (system design, sections 4.1 and 4.3).

The four inference routes of an engine require its pool's bearer secret. The route allow-list on
the internal load balancer is the control; the secret is a second lock. Secrets Manager generates
the value when the secret is created, so no template, parameter, user data or build environment
holds it: an engine reads it from Secrets Manager when it starts, by the secret's ID, which reaches
the instance in its pool's release parameter.

The secret is named `/rhi/pools/<pool>/bearer` and tagged `rhi-pool` with the pool's name, the
marks by which `tests/policy/test_instance_role.py` tells an instance role's own secret. This
module grants the pool's instance role the right to read it, and nothing to any role it does not
create: the gateway's construct grants itself, through `grant_read`.

A grant names the secret by its fixed name, with the six characters Secrets Manager adds to it,
rather than by a reference to the resource. A reference is a value the invariants cannot read, so
they would take it as any secret, the reporter's among them (`tests/policy/test_pipeline.py`); the
name is this pool's secret and no other. It needs no export, so a stack that grants it need not
import from this one.

Rotation is not part of phase 1: a rotated secret needs every engine and the gateway restarted.
`tests/policy/acknowledged/pools_secret.toml` acknowledges cdk-nag's finding with that reason.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from rhi.pools.group import POOL_TAG

# Letters and digits only, so that the value is a bearer token as it stands.
SECRET_LENGTH = 48
# What reading a secret's value needs, as the CDK's own `grant_read` gives it.
READ_ACTIONS = ("secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret")


def secret_name(pool: str) -> str:
    """The fixed name of a pool's bearer secret."""
    return f"/rhi/pools/{pool}/bearer"


class PoolSecret(Construct):
    """One pool's bearer secret, as `secret`, readable by the pool's instance role. Another
    construct is allowed to read it with `grant_read`."""

    def __init__(self, scope: Construct, construct_id: str, *, pool: str, role: iam.IGrantable) -> None:
        super().__init__(scope, construct_id)
        self.secret = secretsmanager.Secret(
            self,
            "Bearer",
            secret_name=secret_name(pool),
            description=f"The bearer secret of the engines of the pool {pool}.",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=SECRET_LENGTH,
                exclude_punctuation=True,
                include_space=False,
            ),
        )
        cdk.Tags.of(self.secret).add(POOL_TAG, pool)
        # The stacks are environment-agnostic, so the ARN's partition, region and account are
        # pseudo parameters, which read the same in any stack of the stage.
        self.arn_pattern = cdk.Stack.of(self).format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{secret_name(pool)}-??????",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        self.grant_read(role)

    def grant_read(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allows `grantee` to read the secret's value, and nothing else."""
        return iam.Grant.add_to_principal(grantee=grantee, actions=list(READ_ACTIONS), resource_arns=[self.arn_pattern])
