"""The admin's sign-in: a Cognito user pool with one user and MFA required (system design,
section 7.4).

Grafana sits behind the public load balancer's own sign-in, which authenticates against this user
pool: an unauthenticated request never reaches Grafana. This module creates the user pool, its one
user, its hosted sign-in domain and the client the load balancer signs in through. The load
balancer's rule that uses them comes with the Grafana service.

**The user pool.** Nobody signs up: the deployment creates the one user, whose name is the
configuration's `admin_email`, and Cognito sends that address its invitation. A second factor is
required, and the only one is an authenticator app (time-based one-time passwords): text messages
would need a role that sends them and a phone number, and the design asks for neither. A forgotten
password is recovered through the same address.

**State.** The user pool holds the admin's account and MFA enrolment, so replacing it would lock
the admin out. It has deletion protection, it is retained on deletion and on replacement, and its
logical ID is recorded in `tests/policy/stateful_logical_ids.json`: the construct IDs here are part
of it, and renaming one replaces the user pool. The user is a resource of its own, and
CloudFormation deletes a resource it is not told to keep, whatever the user pool's own policy: the
user is retained on deletion and on replacement too, so the account and its enrolment stay in the
retained user pool. Changing `admin_email` therefore adds a user and keeps the previous one, which
the owner removes by hand.

**The domain.** The prefix of the hosted sign-in domain becomes a host name under AWS's Cognito
domain, and the host name of a deployed resource is not in the repository (section 10). So the
prefix is the unique identifier that ends the stack's ID, which CloudFormation gives at
deployment: no template synthesized from the repository holds it. It is made of hexadecimal digits
and hyphens, which a prefix may hold, and it cannot spell a word Cognito reserves.

**The client.** An Application Load Balancer needs a client with a secret. Cognito generates it
and the load balancer reads it from Cognito, so no template holds it. The client has the
authorization code flow, the `openid` scope and one callback, `CALLBACK_PATH` on the configured
domain, which is where an Application Load Balancer receives the answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from constructs import Construct

if TYPE_CHECKING:
    from rhi.config import Config

# Where an Application Load Balancer receives the answer of the sign-in.
CALLBACK_PATH = "/oauth2/idpresponse"

# A stack's ID is an ARN that ends `stack/<name>/<unique identifier>`.
_UNIQUE_PART_OF_STACK_ID = 2


def domain_prefix(stack: cdk.Stack) -> str:
    """The prefix of the hosted sign-in domain: the unique identifier that ends the stack's ID, as
    a token that CloudFormation resolves at deployment."""
    return cdk.Fn.select(_UNIQUE_PART_OF_STACK_ID, cdk.Fn.split("/", stack.stack_id))


class SignIn(Construct):
    """The admin's user pool, as `user_pool`, with its hosted sign-in domain, as `domain`, and the
    client the load balancer signs in through, as `client`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)

        self.user_pool = cognito.UserPool(
            self,
            "Pool",
            self_sign_up_enabled=False,
            # The user's name is an e-mail address, read without regard to case.
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            deletion_protection=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # The one user. The address is the owner's own, so it is created verified: recovering a
        # forgotten password needs a verified address. Cognito e-mails it a temporary password.
        admin = cognito.CfnUserPoolUser(
            self,
            "Admin",
            user_pool_id=self.user_pool.user_pool_id,
            username=config.admin_email,
            desired_delivery_mediums=["EMAIL"],
            user_attributes=[
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email", value=config.admin_email),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email_verified", value="true"),
            ],
        )
        # Kept with its user pool: without this, deleting the stack or replacing the user deletes
        # the account and its MFA enrolment from the retained user pool.
        admin.apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        self.domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix(cdk.Stack.of(self))),
        )

        self.client = self.user_pool.add_client(
            "LoadBalancer",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID],
                callback_urls=[f"https://{config.domain}{CALLBACK_PATH}"],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )
