"""The alerts topic: every alarm of the stage, the budget's alerts and Cost Anomaly Detection's
findings publish to it (system design, sections 7.3 and 11.3).

- **Encrypted with a key of the stack.** CloudWatch alarms, AWS Budgets and Cost Anomaly Detection
  cannot use the key AWS manages for SNS, whose policy only lets principals of the account use it.
  The key rotates every year. Its policy keeps the CDK's statement that lets the account's IAM
  policies govern the key, and adds one that lets the three services' principals,
  `cloudwatch.amazonaws.com`, `budgets.amazonaws.com` and `costalerts.amazonaws.com`, call
  `kms:GenerateDataKey*` and `kms:Decrypt` on it, the two calls SNS makes for a publisher of an
  encrypted topic, as each service's documentation gives them, and only for a request made for
  the stack's own account (`aws:SourceAccount`), so that no service acting for another account may
  use the key. A key policy's `*` is the key itself. Other topics of the stack may use the same
  key, from `key`: the condition names the account, not the topic.
- **Refusing unencrypted transport.** The topic's policy denies every request whose
  `aws:SecureTransport` is false. Which service may publish is said where that service's
  construct is made: `rhi.guardrails.anomaly` for Cost Anomaly Detection.
- **One subscription**, by e-mail, to the admin address of the configuration. The address comes
  from the configuration at synthesis, never from the repository. The owner confirms it once, from
  the e-mail it sends (section 8.6, step 5).

The construct creates no role: the grants above are resource policies, on the key and the topic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_sns as sns
from constructs import Construct

if TYPE_CHECKING:
    from rhi.config import Config

# The services that publish to the topic, by their principals.
PUBLISHERS = ("cloudwatch.amazonaws.com", "budgets.amazonaws.com", "costalerts.amazonaws.com")
# What SNS asks of the key for a publisher of an encrypted topic.
KEY_ACTIONS = ("kms:GenerateDataKey*", "kms:Decrypt")


class Alerts(Construct):
    """The alerts topic, as `topic`, and the key it is encrypted with, as `key`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        self.key = kms.Key(
            self,
            "Key",
            description="Encrypts the alerts topic of the stage.",
            enable_key_rotation=True,
            rotation_period=cdk.Duration.days(365),
        )
        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise: jsii passes it by position. So for the arguments below.
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal(service) for service in PUBLISHERS],  # ty: ignore[invalid-argument-type]
                actions=list(KEY_ACTIONS),
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            )
        )
        self.topic = sns.Topic(self, "Topic", master_key=self.key, enforce_ssl=True)  # ty: ignore[invalid-argument-type]
        # A construct of a fixed name: CDK names an e-mail subscription, and its logical ID, after
        # the address, which is private.
        sns.Subscription(
            self,
            "Subscription",
            topic=self.topic,  # ty: ignore[invalid-argument-type]
            protocol=sns.SubscriptionProtocol.EMAIL,
            endpoint=config.admin_email,
        )
